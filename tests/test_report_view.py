"""Offline tests for audit/report_view.py's pure helpers.

The Streamlit page itself is exercised by streamlit.testing.AppTest in
test_app_pages (below) — no browser, no LLM. These tests pin the data
shaping the page relies on: pack discovery (including a broken pack), the
per-control and per-pair tables, and the call/cost estimates.
"""

from __future__ import annotations

import pytest

from audit import batch, report_view
from core.checklists import load_pack
from core.tools import DEFAULT_PACK_PATH

PACK = load_pack(DEFAULT_PACK_PATH)


def test_discover_packs_lists_valid_and_invalid(tmp_path):
    (tmp_path / "good.yaml").write_text(DEFAULT_PACK_PATH.read_text())
    (tmp_path / "bad.yaml").write_text("pack: {id: x}\ncontrols: []\n")
    (tmp_path / "notes.txt").write_text("ignored")
    found = report_view.discover_packs(tmp_path)
    assert [f["path"].name for f in found] == ["bad.yaml", "good.yaml"]
    bad, good = found
    assert bad["pack"] is None and "invalid" in bad["label"] and bad["error"]
    assert good["pack"].pack.id == "iso9001-sample"
    assert good["label"].startswith("ISO 9001:2015 · iso9001-sample v")


def test_discover_packs_empty_dir(tmp_path):
    assert report_view.discover_packs(tmp_path) == []


def test_estimate_calls_brackets_judge_verify_rejudge():
    assert report_view.estimate_calls(0) == (0, 0)
    assert report_view.estimate_calls(180) == (360, 540)


@pytest.mark.parametrize("pricing, expected_note", [
    ((1.0, 2.0), "live OpenRouter rate"),
    (None, "fallback list price"),
])
def test_estimate_cost(pricing, expected_note):
    cost, note = report_view.estimate_cost_usd(1_000_000, 500_000, pricing)
    assert note == expected_note
    if pricing:
        assert cost == pytest.approx(1.0 + 1.0)
    else:
        assert cost == pytest.approx(0.15 + 0.30)


def _fake_report():
    major = next(c for c in PACK.controls if c.severity == "major")

    def row(tid, verdict, status="verdict", **extra):
        r = {"ticket_id": tid, "control_id": major.control_id, "control_title": major.title,
             "severity": major.severity, "verdict": verdict, "rationale": "r", "citations": [tid],
             "status": status, "verification": {"agrees": True, "issues": [], "model": "m"},
             "attempts": 1, "prompt_tokens": 1, "completion_tokens": 1}
        r.update(extra)
        return r

    results = {
        ("T1", major.control_id): row("T1", "satisfied"),
        ("T2", major.control_id): row("T2", "gap", status="needs_review",
                                      verification={"agrees": False, "issues": ["x"], "model": "m"},
                                      attempts=2),
        ("T3", major.control_id): batch._error_row("T3", major, RuntimeError("boom")),
    }
    return batch._aggregate(PACK, ["T1", "T2", "T3"], [major.control_id],
                            {major.control_id: major}, results)


def test_per_control_rows_shape():
    rows = report_view.per_control_rows(_fake_report())
    assert len(rows) == 1
    r = rows[0]
    assert r["Satisfied"] == 1 and r["Review"] == 1 and r["Error"] == 1
    assert r["Satisfied rate"] == 100  # 1 satisfied of 1 applicable verified
    assert set(r) >= {"Control", "Clause", "Severity", "Gap", "N/A"}


def test_pair_rows_flatten_status_and_verifier():
    rows = report_view.pair_rows(_fake_report().rows)
    by_ticket = {r["Ticket"]: r for r in rows}
    assert by_ticket["T1"]["Verifier"] == "agrees" and by_ticket["T1"]["Status"] == "verdict"
    assert by_ticket["T2"]["Verifier"] == "objects" and by_ticket["T2"]["Attempts"] == 2
    assert by_ticket["T3"]["Verdict"] == "—" and by_ticket["T3"]["Status"] == "error"
    assert by_ticket["T3"]["Verifier"] == "—"


# ---------------------------------------------------------------------------
# The page renders without a browser or an LLM (streamlit.testing)
# ---------------------------------------------------------------------------


def test_chat_entrypoint_mounts_headless():
    """assistant/app.py still runs as the entrypoint after the navigation
    refactor; the default page is the chat."""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file("assistant/app.py", default_timeout=60).run()
    assert not at.exception, at.exception
    assert any("Compliance chat" in t.value for t in at.title)


def test_report_page_renders_empty_state_headless(monkeypatch):
    """The report page mounts without a browser or an LLM: the pack picker
    lists the ISO 9001 pack, the empty-state hint shows, and run_batch is
    patched so nothing could ever spend tokens here."""
    from streamlit.testing.v1 import AppTest

    monkeypatch.setattr(report_view, "run_batch", lambda *a, **k: pytest.fail("batch ran"))
    monkeypatch.setattr(report_view, "_model_pricing", lambda model: None)

    def _script():  # from_function copies the source into a bare script,
        from audit.report_view import report_page  # so imports go inside
        report_page()

    at = AppTest.from_function(_script, default_timeout=60).run()
    assert not at.exception, at.exception
    picker = at.sidebar.selectbox[0]
    assert picker.label == "Checklist pack"
    assert any("iso9001-sample" in o for o in picker.options)
    assert at.sidebar.button[0].label == "Run audit"
    assert any("Run audit" in i.value for i in at.info)
    assert any("Audit-readiness report" in t.value for t in at.title)


def test_report_page_renders_a_report_after_run(monkeypatch):
    """Click 'Run audit' with run_batch faked → headline metrics, per-control
    table, the four tabs, and both download buttons appear. Pins the page's
    structure without spending a token."""
    from streamlit.testing.v1 import AppTest

    fake = _fake_report()
    monkeypatch.setattr(report_view, "run_batch", lambda *a, **k: fake)
    monkeypatch.setattr(report_view, "write_report", lambda r, d: ("x.md", "x.json"))
    monkeypatch.setattr(report_view, "_model_pricing", lambda model: None)

    def _script():
        from audit.report_view import report_page
        report_page()

    at = AppTest.from_function(_script, default_timeout=120).run()
    at.sidebar.button[0].click().run()
    assert not at.exception, at.exception

    labels = [m.label for m in at.metric]
    assert labels == ["Readiness", "Verified pairs", "Open gaps", "Needs review", "Est. cost"]
    values = dict(zip(labels, (m.value for m in at.metric)))
    assert values["Readiness"] == "100%" and values["Verified pairs"] == "1 / 3"
    assert values["Needs review"] == "1"
    assert any("1 pair(s) failed to run" in w.value for w in at.warning)
    assert [t.label for t in at.tabs] == ["Open gaps (0)", "Needs review (1)", "All pairs (3)", "Errors (1)"]
    assert len(at.dataframe) >= 2  # per-control table + all-pairs table
    assert any("Compliance chat" not in t.value for t in at.title)
