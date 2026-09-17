"""Offline tests for audit/eval/run_eval.py — scoring is pure, running is faked.

What is pinned: the v1 metric definitions are reproduced exactly (so the
ISO 9001 delta means something), the four agent metrics do what their names
say, errored pairs never inflate a rate, rows keep answer-key order, and both
committed answer keys resolve against their packs.
"""

from __future__ import annotations

import pytest

from audit.eval import run_eval as ev
from core.checklists import load_pack
from core.tools import load_tickets


def _row(expected, predicted, status="verdict", citation="ok", attempts=1):
    return {
        "ticket_id": "T", "control_id": "C", "expected": expected,
        "predicted": predicted, "match": expected == predicted, "status": status,
        "verifier_verdict": None, "verifier_issues": [], "attempts": attempts,
        "citation_status": citation, "rationale": "r",
        "prompt_tokens": 10, "completion_tokens": 1,
    }


def _err():
    r = _row("gap", None, status="error", citation=None, attempts=0)
    r["match"] = False
    r["prompt_tokens"] = r["completion_tokens"] = 0
    return r


ROWS = [
    _row("satisfied", "satisfied"),                       # clean, finalized
    _row("satisfied", "satisfied"),
    _row("satisfied", "gap", status="needs_review"),      # FP, but verifier caught it
    _row("satisfied", "partial"),                         # FP finalized (verifier missed)
    _row("gap", "gap", attempts=2),                       # re-judged, ended right
    _row("gap", "gap"),
    _row("gap", "satisfied", status="needs_review"),      # missed gap, caught
    _row("partial", "gap"),                               # flagged but wrong level
    _row("gap", "gap", status="needs_review"),            # verifier false alarm
    _row("satisfied", "satisfied", citation="unresolved"),
    _err(),
]


def test_assistant_definitions_reproduced():
    m = ev.score(ROWS)
    assert m["pairs"] == 11 and m["scored"] == 10 and m["errors"] == 1
    # 10 scored: matches = 6 (rows 0,1,4,5,8,9)
    assert m["exact_agreement"] == 0.6
    # gt gap = rows 4,5,6,8 -> predicted gap in 4,5,8
    assert m["gap_recall"] == 0.75
    # gt deficient = 4,5,6,7,8 -> flagged (gap/partial) in 4,5,7,8
    assert m["flagged_recall"] == 0.8
    # gt satisfied = 0,1,2,3,9 -> flagged in 2,3
    assert m["false_positive_rate"] == 0.4
    # cited = all 10 (none N/A) -> ok in 9
    assert m["citation_validity"] == 0.9
    assert m["confusion"]["satisfied->gap"] == 1
    assert m["confusion"]["gap->satisfied"] == 1


def test_agent_metrics():
    m = ev.score(ROWS)
    # parked = rows 2,6,8 of 10 scored
    assert m["review_rate"] == 0.3
    # of the 3 parked, rows 2 and 6 were wrong -> precision 2/3
    assert m["review_precision"] == 0.667
    # wrong = rows 2,3,6,7 -> parked among them: 2,6 -> 0.5
    assert m["error_catch_rate"] == 0.5
    # finalized = 7 rows (0,1,3,4,5,7,9); matches 0,1,4,5,9 -> 5/7
    assert m["finalized_pairs"] == 7
    assert m["finalized_exact_agreement"] == 0.714
    # finalized satisfied = 0,1,3,9 -> flagged: 3 -> 0.25
    assert m["finalized_false_positive_rate"] == 0.25
    assert m["rejudged_pairs"] == 1
    assert m["prompt_tokens"] == 100 and m["completion_tokens"] == 10


def test_errored_rows_are_counted_but_never_in_a_denominator():
    only_errors = ev.score([_err(), _err()])
    assert only_errors["errors"] == 2 and only_errors["scored"] == 0
    for k in ("exact_agreement", "gap_recall", "false_positive_rate",
              "review_rate", "error_catch_rate", "finalized_exact_agreement"):
        assert only_errors[k] is None


def test_compare_to_baseline_deltas():
    base = {"exact_agreement": 0.957, "gap_recall": 1.0, "flagged_recall": 1.0,
            "false_positive_rate": 0.032, "citation_validity": 0.935}
    m = {"exact_agreement": 0.978, "gap_recall": 1.0, "flagged_recall": 1.0,
         "false_positive_rate": 0.0, "citation_validity": None}
    d = ev.compare_to_baseline(m, base)
    assert d["exact_agreement"]["delta"] == 0.021
    assert d["false_positive_rate"]["delta"] == -0.032
    assert d["citation_validity"]["delta"] is None


@pytest.mark.parametrize("name", sorted(ev.PACKS))
def test_committed_answer_keys_resolve_against_their_packs(name):
    pack_path, gt_path = ev.PACKS[name]
    pack = load_pack(pack_path)
    ids = {c.control_id for c in pack.controls}
    tickets = load_tickets()
    pairs = ev.load_pairs(gt_path)
    assert pairs, "answer key is empty"
    for t, c, e in pairs:
        assert t in tickets, (name, t)
        assert c in ids, (name, c)
        assert e in ("satisfied", "partial", "gap"), (name, t, c, e)


def test_run_pairs_keeps_order_and_isolates_exceptions():
    pack = load_pack(ev.PACKS["iso9001"][0])
    tickets = load_tickets()
    pairs = ev.load_pairs(ev.PACKS["iso9001"][1])[:4]
    boom = pairs[1]

    def runner(ticket, control):
        if (ticket["ticket_id"], control.control_id) == boom[:2]:
            raise RuntimeError("provider down")
        return {
            "verdict": "gap", "rationale": "r", "status": "verdict",
            "verification": {"verdict": "gap", "issues": []},
            "citation_check": {"status": "ok"}, "attempts": 1,
            "prompt_tokens": 3, "completion_tokens": 1,
        }

    seen = []
    rows = ev.run_pairs(pack, tickets, pairs, runner=runner, max_workers=3,
                        progress=lambda d, t, label: seen.append((d, t)))
    assert [(r["ticket_id"], r["control_id"]) for r in rows] == [p[:2] for p in pairs]
    assert rows[1]["status"] == "error" and "provider down" in rows[1]["error"]
    assert rows[0]["status"] == "verdict" and rows[0]["verifier_verdict"] == "gap"
    assert seen[-1] == (4, 4)


def test_run_pairs_rejects_unknown_ids():
    pack = load_pack(ev.PACKS["iso9001"][0])
    tickets = load_tickets()
    with pytest.raises(ValueError, match="unknown control"):
        ev.run_pairs(pack, tickets, [("TCK-1001", "NOPE", "gap")], runner=lambda *_: {})
    with pytest.raises(ValueError, match="unknown ticket"):
        ev.run_pairs(pack, tickets, [("TCK-0", pack.controls[0].control_id, "gap")],
                     runner=lambda *_: {})
