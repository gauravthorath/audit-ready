"""Unit tests for core/tools.py - coverage_report's aggregation + progress.

check_ticket itself needs the LLM and the vector store (scripts/
smoke_tools.py); what's unit-testable is everything coverage_report does
AROUND it: pair fan-out, thread-pool execution, progress reporting, the
severity-weighted readiness math, and per-control aggregation. So these
tests replace check_ticket with a deterministic fake (monkeypatch) and
replace the pack/ticket caches with tiny fixtures - hermetic, offline,
and they pin the exact scoring policy (3:1 severity, partial=0.5,
not_applicable out of the denominator).

Testing concept: monkeypatch swaps a module attribute for the duration of
one test and restores it automatically - here it cuts the LLM out of the
dependency chain the same way a mock server cuts out a network.
"""

import threading

import pytest

import core.tools as tools
from core.checklists import ChecklistPack


def _control(control_id: str, severity: str) -> dict:
    return {
        "control_id": control_id,
        "standard": "TEST 0001",
        "clause": "1.1",
        "title": f"Control {control_id}",
        "description": "test control",
        "expected_evidence": ["signoff"],
        "sufficiency_rubric": "satisfied if signoff present",
        "citation_required": False,
        "severity": severity,
    }


PACK = ChecklistPack.model_validate(
    {
        "pack": {"id": "t", "standard": "TEST", "version": "1", "description": "d"},
        "controls": [_control("C-MAJ", "major"), _control("C-MIN", "minor")],
    }
)

# The verdict matrix the fake judge returns - chosen so every aggregation
# rule is exercised: full credit, none, half, and an excluded pair.
VERDICTS = {
    ("T-1", "C-MAJ"): "satisfied",
    ("T-2", "C-MAJ"): "gap",
    ("T-1", "C-MIN"): "partial",
    ("T-2", "C-MIN"): "not_applicable",
}


@pytest.fixture
def hermetic(monkeypatch):
    """Swap the LLM-dependent judge and the file-backed caches for fixtures."""

    calls: list[tuple[str, str]] = []

    def fake_check_ticket(ticket_id: str, control_id: str | None = None) -> dict:
        calls.append((ticket_id, control_id))
        return {
            "verdict": VERDICTS[(ticket_id, control_id)],
            "rationale": f"because {ticket_id}/{control_id}",
            "prompt_tokens": 10,
            "completion_tokens": 5,
        }

    monkeypatch.setattr(tools, "check_ticket", fake_check_ticket)
    monkeypatch.setattr(tools, "_pack_cache", PACK)
    monkeypatch.setattr(tools, "_tickets_cache", {"T-1": {}, "T-2": {}})
    return calls


def test_readiness_score_is_severity_weighted(hermetic):
    report = tools.coverage_report()
    # Hand-computed: T-1×C-MAJ satisfied -> 1.0×3, T-2×C-MAJ gap -> 0.0×3,
    # T-1×C-MIN partial -> 0.5×1; T-2×C-MIN not_applicable -> excluded.
    # (3.0 + 0.0 + 0.5) / (3 + 3 + 1) = 0.5
    assert report["readiness_score"] == 0.5
    # Token accounting sums every judged pair: 4 × (10 + 5).
    assert report["prompt_tokens"] == 40
    assert report["completion_tokens"] == 20


def test_per_control_counts_rates_and_failing_tickets(hermetic):
    report = tools.coverage_report()

    maj = report["per_control"]["C-MAJ"]
    assert maj["counts"] == {"satisfied": 1, "partial": 0, "gap": 1, "not_applicable": 0}
    assert maj["satisfied_rate"] == 0.5
    assert [f["ticket_id"] for f in maj["failing_tickets"]] == ["T-2"]
    assert maj["failing_tickets"][0]["rationale"] == "because T-2/C-MAJ"

    mnr = report["per_control"]["C-MIN"]
    # not_applicable leaves ONE applicable pair, and it's partial: rate 0.
    assert mnr["satisfied_rate"] == 0.0
    assert [f["ticket_id"] for f in mnr["failing_tickets"]] == ["T-1"]


def test_unscoped_call_defaults_to_whole_corpus_and_pack(hermetic):
    report = tools.coverage_report()
    assert report["tickets_checked"] == ["T-1", "T-2"]
    assert report["controls_checked"] == ["C-MAJ", "C-MIN"]
    assert sorted(hermetic) == sorted(VERDICTS)  # every pair judged exactly once


def test_scoping_limits_the_pairs_judged(hermetic):
    tools.coverage_report(ticket_ids=["T-1"], control_ids=["C-MAJ"])
    assert hermetic == [("T-1", "C-MAJ")]


def test_progress_reports_every_pair_with_running_count(hermetic):
    events: list[tuple[int, int, str]] = []
    tools.coverage_report(progress=lambda d, t, label: events.append((d, t, label)))

    # One event per judged pair, done counting 1..total, total constant.
    assert [e[0] for e in events] == [1, 2, 3, 4]
    assert all(e[1] == 4 for e in events)
    # Labels name the pair (completion order varies under the thread pool,
    # so assert set membership, not sequence).
    assert {e[2] for e in events} == {f"{t} × {c}" for t, c in VERDICTS}


def test_progress_callback_runs_on_the_callers_thread(hermetic):
    # The contract the Streamlit layer depends on: st.progress may only be
    # touched from the script's own thread, so the callback must fire from
    # the caller's thread even though judgments run on a pool.
    caller = threading.current_thread().name
    seen: set[str] = set()
    tools.coverage_report(progress=lambda *_: seen.add(threading.current_thread().name))
    assert seen == {caller}


def test_unknown_control_fails_before_any_judgment_is_spent(hermetic):
    with pytest.raises(KeyError):
        tools.coverage_report(control_ids=["NO-SUCH-CONTROL"])
    assert hermetic == []  # zero LLM spend on a doomed request


def test_parallel_and_sequential_reports_are_identical(hermetic):
    # Determinism guarantee: aggregation follows the REQUESTED order, not
    # completion order, so worker count must never change the report.
    assert tools.coverage_report(max_workers=4) == tools.coverage_report(max_workers=1)


def test_load_artifacts_indexes_changes_and_emails_by_id():
    from core.tools import load_artifacts

    a = load_artifacts()
    assert a["CHG-3001"]["approver"] and a["CHG-3001"]["approved"] < a["CHG-3001"]["implemented"]
    assert a["EML-2001"]["subject"]
    assert all(k.startswith(("CHG-", "EML-")) for k in a)
