"""Hermetic tests for audit/batch.py - aggregation, isolation, rendering.

No network: the LangGraph runner is swapped for a fake via the `runner`
argument. Under test is what the BATCH layer adds around per-pair results
(CR-2): contested/errored pairs stay out of the score, one exception does
not sink the run, order is deterministic, and the Markdown says the right
things in the right places.
"""

import pytest

from audit import batch
from core.checklists import load_pack
from core.tools import DEFAULT_PACK_PATH

PACK = load_pack(DEFAULT_PACK_PATH)
MAJOR = next(c.control_id for c in PACK.controls if c.severity == "major")
MINOR = next(c.control_id for c in PACK.controls if c.severity == "minor")

TICKETS = {
    "T1": {"ticket_id": "T1"},
    "T2": {"ticket_id": "T2"},
    "T3": {"ticket_id": "T3"},
}


def _row(ticket, control, verdict, status="verdict", **extra):
    base = {
        "ticket_id": ticket["ticket_id"],
        "control_id": control.control_id,
        "control_title": control.title,
        "severity": control.severity,
        "verdict": verdict,
        "rationale": f"{verdict} because",
        "citations": [ticket["ticket_id"]],
        "status": status,
        "verification": {"agrees": status == "verdict", "issues": [], "model": "m"},
        "attempts": 1,
        "prompt_tokens": 10,
        "completion_tokens": 5,
    }
    base.update(extra)
    return base


def make_runner(table):
    """table: {(ticket_id, control_id): verdict | ("needs_review", verdict) | Exception}"""

    def runner(ticket, control):
        spec = table[(ticket["ticket_id"], control.control_id)]
        if isinstance(spec, Exception):
            raise spec
        if isinstance(spec, tuple):
            status, verdict = spec
            return _row(ticket, control, verdict, status=status,
                        verification={"agrees": False, "issues": ["rubric not met"], "model": "m"},
                        attempts=2)
        return _row(ticket, control, spec)

    return runner


def run(table, **kw):
    # preserve the table's insertion order so tests can assert on it
    ids = list(dict.fromkeys(t for t, _ in table)), list(dict.fromkeys(c for _, c in table))
    return batch.run_batch(PACK, TICKETS, ids[0], ids[1], runner=make_runner(table),
                           max_workers=2, **kw)


# ---------------------------------------------------------------- scoring


def test_score_uses_core_weights_and_skips_not_applicable():
    r = run({
        ("T1", MAJOR): "satisfied",      # 3 * 1.0
        ("T2", MAJOR): "gap",            # 3 * 0.0
        ("T1", MINOR): "partial",        # 1 * 0.5
        ("T2", MINOR): "not_applicable", # excluded
    })
    assert r.readiness_score == round(3.5 / 7, 3)
    assert r.verified_pairs == r.total_pairs == 4


def test_contested_and_errored_pairs_are_outside_the_score():
    r = run({
        ("T1", MAJOR): "satisfied",
        ("T2", MAJOR): ("needs_review", "gap"),   # would tank the score if counted
        ("T3", MAJOR): RuntimeError("provider down"),
    })
    assert r.readiness_score == 1.0
    assert r.verified_pairs == 1 and r.total_pairs == 3
    assert [q["ticket_id"] for q in r.review_queue] == ["T2"]
    assert r.errors[0]["error"].startswith("RuntimeError: provider down")
    counts = r.per_control[MAJOR]["counts"]
    assert counts == {"satisfied": 1, "partial": 0, "gap": 0, "not_applicable": 0,
                      "needs_review": 1, "error": 1}


def test_all_pairs_unverified_gives_no_score_not_zero():
    r = run({("T1", MAJOR): ("needs_review", "satisfied")})
    assert r.readiness_score is None


# ---------------------------------------------------------------- isolation & order


def test_one_exception_does_not_stop_the_other_pairs():
    r = run({
        ("T1", MAJOR): ValueError("boom"),
        ("T2", MAJOR): "satisfied",
        ("T3", MAJOR): "gap",
    })
    assert len(r.rows) == 3
    assert [x["status"] for x in r.rows] == ["error", "verdict", "verdict"]


def test_rows_follow_requested_order_not_completion_order():
    seen = []
    r = run({("T1", MAJOR): "satisfied", ("T2", MAJOR): "gap",
             ("T1", MINOR): "gap", ("T2", MINOR): "satisfied"},
            progress=lambda d, t, label: seen.append((d, t)))
    assert [(x["control_id"], x["ticket_id"]) for x in r.rows] == [
        (MAJOR, "T1"), (MAJOR, "T2"), (MINOR, "T1"), (MINOR, "T2")]
    assert seen[-1] == (4, 4) and [d for d, _ in seen] == [1, 2, 3, 4]


def test_unknown_ids_fail_before_any_work():
    calls = []
    with pytest.raises(KeyError):
        batch.run_batch(PACK, TICKETS, ["NOPE"], [MAJOR],
                        runner=lambda t, c: calls.append(1))
    with pytest.raises(KeyError):
        batch.run_batch(PACK, TICKETS, ["T1"], ["ISO9001-nope"],
                        runner=lambda t, c: calls.append(1))
    assert calls == []


def test_zero_pairs_refuses_to_run():
    with pytest.raises(ValueError):
        batch.run_batch(PACK, TICKETS, [], [MAJOR], runner=lambda t, c: None)


# ---------------------------------------------------------------- rendering


def test_markdown_has_every_section_with_the_right_items():
    r = run({
        ("T1", MAJOR): "gap",
        ("T2", MAJOR): ("needs_review", "satisfied"),
        ("T3", MAJOR): RuntimeError("timeout"),
    })
    md = batch.render_markdown(r)
    assert md.startswith("# Audit-readiness report - ISO 9001")
    assert "## Readiness score: 0%" in md
    assert "**1 of 3** record-control pairs" in md
    assert "## Open gaps (1)" in md and "**T1 × " in md and "GAP (major)" in md
    assert "## Needs human review (1)" in md and "rubric not met" in md
    assert "## Errors (1)" in md and "RuntimeError: timeout" in md
    assert "| Satisfied rate |" in md
    assert "20 prompt + 10 completion tokens" in md  # errored row contributes 0


def test_write_report_emits_md_and_json(tmp_path):
    r = run({("T1", MAJOR): "satisfied"})
    md, js = batch.write_report(r, tmp_path)
    assert md.exists() and js.exists() and md.stem == js.stem
    import json
    data = json.loads(js.read_text())
    assert data["readiness_score"] == 1.0 and data["rows"][0]["ticket_id"] == "T1"
    assert "open_gaps" in data and "review_queue" in data


def test_every_row_and_the_report_carry_elapsed_ms():
    """Timing wraps the runner call, so a fake runner is timed like the real one (Step 37)."""
    pack = load_pack(DEFAULT_PACK_PATH)
    tickets = {"TCK-1": {"ticket_id": "TCK-1"}, "TCK-2": {"ticket_id": "TCK-2"}}
    cid = pack.controls[0].control_id

    def runner(ticket, control):
        return _row(ticket, control, "satisfied")

    report = batch.run_batch(pack, tickets, ["TCK-1", "TCK-2"], [cid], runner=runner)
    assert all(isinstance(r["elapsed_ms"], int) and r["elapsed_ms"] >= 0 for r in report.rows)
    assert isinstance(report.elapsed_ms, int) and report.elapsed_ms >= 0
    assert "Wall time" in batch.render_markdown(report) or report.elapsed_ms == 0
