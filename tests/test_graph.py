"""Hermetic tests for audit/graph.py - routing and the verify loop.

No network: judge_ticket, retrieve_evidence and the verifier's chat() are
monkeypatched. What is under test is the CONTROL FLOW the graph adds around
core/ (CR-1), which is exactly what check_ticket does not have:

- clean verdict → finalize on the first pass, one judge call
- verifier objection → exactly one re-judge carrying the feedback, then finalize
- objection that survives the retry → flag as needs_review
- malformed verifier reply → flag, never silently finalize
- unresolved citations alone (verifier agrees) → still triggers the re-judge
"""

from __future__ import annotations

import json

import pytest

import audit.graph as graph
from core.checklists import Control
from core.judge import JudgeResult, Verdict
from core.llm import ChatResult

CONTROL = Control(
    control_id="TEST-1",
    standard="ISO 9001:2015",
    clause="8.6",
    title="Release sign-off",
    description="Releases need an approval record.",
    expected_evidence=["approval comment"],
    sufficiency_rubric="Satisfied only if an approval record with approver exists.",
    citation_required=True,
    severity="major",
)

TICKET = {
    "ticket_id": "TCK-9001",
    "title": "Deploy hotfix",
    "comments": [{"comment_id": "CMT-1", "author": "qa", "body": "Approved by QA lead."}],
}


class _FakeBundle:
    queries = ["approval record", "approver named"]
    hits: list = []


def _judge(verdict: str, citations: list[str]) -> JudgeResult:
    return JudgeResult(
        verdict=Verdict(verdict=verdict, rationale="because", citations=citations),
        model="fake",
        prompt_tokens=10,
        completion_tokens=5,
    )


def _chat_result(content: str) -> ChatResult:
    return ChatResult(content=content, model="fake-verifier", prompt_tokens=7, completion_tokens=3)


@pytest.fixture
def wired(monkeypatch):
    """Patch the three seams; return a dict the test fills with scripted replies."""
    script = {"judge": [], "verify": [], "judge_calls": [], "verify_calls": 0}

    def fake_judge(record, control, feedback=None):
        script["judge_calls"].append(feedback)
        return script["judge"].pop(0)

    def fake_chat(messages, **kwargs):
        script["verify_calls"] += 1
        return _chat_result(script["verify"].pop(0))

    monkeypatch.setattr(graph, "retrieve_evidence", lambda control, ticket_id: _FakeBundle())
    monkeypatch.setattr(graph, "judge_ticket", fake_judge)
    monkeypatch.setattr(graph, "chat", fake_chat)
    monkeypatch.setattr(graph, "_graph", None)  # fresh compile per test
    return script


def _says(level: str, *issues: str) -> str:
    """Fake verifier reply: the verifier states its own level; the graph
    derives agreement by comparing levels."""
    return json.dumps({"verdict": level, "issues": list(issues)})


AGREE = _says("satisfied")                                   # matches a "satisfied" judge
OBJECT = _says("gap", "rubric requires an approver; none named")


def test_clean_verdict_finalizes_on_first_pass(wired):
    wired["judge"] = [_judge("satisfied", ["TCK-9001"])]
    wired["verify"] = [AGREE]

    r = graph.run_control(TICKET, CONTROL)

    assert r["status"] == "verdict"
    assert r["attempts"] == 1
    assert wired["judge_calls"] == [None]
    assert r["citation_check"]["status"] == "ok"
    assert r["verification"]["agrees"] is True
    assert [t.split(":")[0] for t in r["trace"]] == ["retrieve", "judge #1", "verify", "finalize"]


def test_objection_triggers_exactly_one_rejudge_with_feedback(wired):
    wired["judge"] = [_judge("satisfied", ["TCK-9001"]), _judge("partial", ["TCK-9001"])]
    wired["verify"] = [OBJECT, _says("partial")]

    r = graph.run_control(TICKET, CONTROL)

    # The re-judge moved satisfied → partial and the verifier now agrees. Since
    # Step 20 a verdict that CHANGED under review is parked, not finalized: the
    # two opinions stopped being independent the moment the judge read the
    # verifier's claims.
    assert r["status"] == "needs_review"
    assert "changed under review (satisfied → partial)" in r["trace"][-1]
    assert r["verdict"] == "partial"
    assert r["attempts"] == 2
    first, second = wired["judge_calls"]
    assert first is None
    assert "approver" in second  # the verifier's issue reached the re-judge
    assert wired["verify_calls"] == 2
    # token counters accumulate across the loop: 2 judge + 2 verify calls
    assert r["prompt_tokens"] == 2 * 10 + 2 * 7
    assert r["completion_tokens"] == 2 * 5 + 2 * 3


def test_still_contested_after_retry_flags_for_review(wired):
    wired["judge"] = [_judge("satisfied", ["TCK-9001"]), _judge("satisfied", ["TCK-9001"])]
    wired["verify"] = [OBJECT, OBJECT]

    r = graph.run_control(TICKET, CONTROL)

    assert r["status"] == "needs_review"
    assert r["attempts"] == 2
    assert len(wired["judge_calls"]) == 2  # never a third pass
    assert r["trace"][-1].startswith("flag:")
    assert "contested" in r["trace"][-1]


def test_malformed_verifier_reply_flags_not_finalizes(wired):
    wired["judge"] = [_judge("satisfied", ["TCK-9001"])]
    wired["verify"] = ["I think it looks fine, no JSON here"]

    r = graph.run_control(TICKET, CONTROL)

    assert r["status"] == "needs_review"
    assert r["verification"]["agrees"] is None
    assert r["attempts"] == 1  # no retry: nothing concrete to feed back
    assert "could not be parsed" in r["trace"][-1]


def test_unresolved_citation_alone_triggers_rejudge(wired):
    # Verifier agrees both times, but the deterministic check catches a phantom id.
    wired["judge"] = [_judge("satisfied", ["EML-2099"]), _judge("gap", ["TCK-9001"])]
    wired["verify"] = [_says("satisfied"), _says("gap")]

    r = graph.run_control(TICKET, CONTROL)

    assert r["attempts"] == 2
    assert "unresolved" in wired["judge_calls"][1]
    assert r["verdict"] == "gap"
    assert r["status"] == "needs_review"  # satisfied → gap is a flip: parked


def test_holding_firm_through_an_objection_finalizes(wired):
    """Judge keeps its level after reading the objection and the verifier comes
    around → independent-enough agreement → finalize. Only FLIPS are parked."""
    wired["judge"] = [_judge("satisfied", ["TCK-9001"]), _judge("satisfied", ["TCK-9001"])]
    wired["verify"] = [OBJECT, _says("satisfied")]

    r = graph.run_control(TICKET, CONTROL)

    assert r["attempts"] == 2
    assert r["status"] == "verdict"
    assert r["verdict"] == "satisfied"


def test_verifier_checks_are_parsed_and_kept():
    raw = ('{"checks": [{"condition": "approver named", "evidence": "CHG-1 approver=R.W.", "met": true},'
           ' {"condition": "approval before implementation", "evidence": "approved 03-18 < implemented 03-20", "met": true}],'
           ' "verdict": "satisfied", "issues": []}')
    v = graph._parse_verification(raw, "satisfied")
    assert v.agrees is True and len(v.checks) == 2 and v.checks[1].met is True
    # old-shape replies (no checks) still parse - the field defaults to empty
    assert graph._parse_verification('{"verdict": "gap", "issues": []}', "gap").checks == []


def test_parse_verification_slices_json_out_of_prose():
    v = graph._parse_verification('Sure. {"verdict": "gap", "issues": ["x"]} done.', "satisfied")
    assert v.agrees is False and v.verdict == "gap" and v.issues == ["x"]
    assert graph._parse_verification('{"verdict": "gap", "issues": []}', "gap").agrees is True
    with pytest.raises(ValueError):
        graph._parse_verification("no braces at all", "gap")
    with pytest.raises(ValueError):
        graph._parse_verification('{"verdict": "maybe"}', "gap")


def test_route_after_verify_is_pure():
    base = {"verification": {"agrees": True}, "feedback": None, "attempts": 1}
    assert graph.route_after_verify(base) == "finalize"
    assert graph.route_after_verify({**base, "feedback": "- x"}) == "judge"
    assert graph.route_after_verify({**base, "feedback": "- x", "attempts": 2}) == "flag"
    assert graph.route_after_verify({**base, "verification": {"agrees": None}}) == "flag"


def test_feedback_carries_claims_never_the_verifiers_level(wired):
    """Step 20 regression: telling the judge the verifier's level made it fold.
    The re-judge must see the concrete objection and nothing about levels."""
    wired["judge"] = [_judge("satisfied", ["TCK-9001"]), _judge("satisfied", ["TCK-9001"])]
    wired["verify"] = [_says("gap", "rubric requires an approver; none named"), _says("satisfied")]

    r = graph.run_control(TICKET, CONTROL)

    _, second = wired["judge_calls"]
    assert "approver" in second
    for level in ("gap", "satisfied", "partial", "second auditor"):
        assert level not in second.lower()
    assert r["status"] == "verdict" and r["verdict"] == "satisfied"


def test_bare_disagreement_becomes_level_free_recheck(wired):
    """Verifier picks another level but names no problem → the judge gets a
    re-check instruction, still with no level in it, and the loop still runs."""
    wired["judge"] = [_judge("satisfied", ["TCK-9001"]), _judge("satisfied", ["TCK-9001"])]
    wired["verify"] = [_says("partial"), _says("partial")]

    r = graph.run_control(TICKET, CONTROL)

    _, second = wired["judge_calls"]
    assert second == "- " + graph.RECHECK_INSTRUCTION
    assert "partial" not in second
    assert r["status"] == "needs_review" and r["attempts"] == 2


def test_retrieve_joins_linked_records_by_id(wired, monkeypatch):
    """Step 20: a ticket's linked_changes/linked_emails reach the judge even when
    similarity retrieval pulled none of their chunks."""
    monkeypatch.setattr(graph, "_ARTIFACTS", {
        "CHG-42": {"change_id": "CHG-42", "approver": "R. W.", "approved": "2026-01-01T09:00:00Z"},
        "EML-7": {"email_id": "EML-7", "subject": "approval"},
    })
    ticket = dict(TICKET, linked_changes=["CHG-42", "CHG-missing"], linked_emails=["EML-7"])
    seen = {}

    def spy_judge(record, control, feedback=None):
        seen["record"] = record
        return _judge("satisfied", ["TCK-9001"])

    monkeypatch.setattr(graph, "judge_ticket", spy_judge)
    wired["verify"] = [_says("satisfied")]

    r = graph.run_control(ticket, CONTROL)

    linked = seen["record"]["linked_records"]
    assert [x.get("change_id") or x.get("email_id") for x in linked] == ["CHG-42", "EML-7"]
    assert "2 linked record(s) joined by id" in r["trace"][0]
    assert r["status"] == "verdict"
