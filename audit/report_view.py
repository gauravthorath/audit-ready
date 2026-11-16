"""Streamlit page: batch audit report + pack picker + agent-trace panel.

Plan item 5 (UI), serving CR-2 (the report as a product surface) and FR-6
(export of findings). Same rule as assistant/app.py: NO business logic here.
Everything this page shows comes out of audit.batch.run_batch(); this
file decides layout, not verdicts.

What the page is for, in the order a compliance manager reads it:
  1. pick a checklist pack (the "new standard = new YAML file" demo moment);
  2. choose scope and run - with a progress bar, because a full pack is
     hundreds of LLM calls and a silent spinner reads as "hung" (FR-16);
  3. the score, with its denominator right beside it;
  4. the per-control table, then the work lists: open gaps, review queue;
  5. the agent trace for any pair - judge, verifier, re-judge, flag - so
     "human in the loop" is something you can click, not a README claim;
  6. download the Markdown/JSON the batch runner already wrote.

Design notes:
- The report lives in st.session_state so switching pages or clicking an
  expander does not re-run a $0.10 batch. "Run audit" is the only trigger.
- Pure helpers (discover_packs, per_control_rows, pair_rows, estimate_calls)
  are module-level and free of Streamlit so tests can pin them offline.
- Pricing reuses core.llm.get_model_pricing behind st.cache_data, the same
  policy as the chat page: one fetch per model per hour.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import streamlit as st

from audit.batch import BatchReport, render_markdown, run_batch, write_report
from core.checklists import ChecklistPack, load_pack
from core.llm import LLMError, get_model_pricing
from core.tools import DEFAULT_CORPUS_DIR, DEFAULT_PACK_PATH, load_tickets
from assistant.agent import DEFAULT_MODEL

REPORT_DIR = Path("reports")

VERDICT_BADGES = {
    "satisfied": "🟢 satisfied",
    "partial": "🟡 partial",
    "gap": "🔴 gap",
    "not_applicable": "⚪ not applicable",
    None: "⚫ no verdict",
}
STATUS_BADGES = {"verdict": "✅ verified", "needs_review": "🧑‍⚖️ needs review", "error": "⚠️ error"}


# ---------------------------------------------------------------------------
# Pure helpers live in audit/reporting.py so api/ can use them without
# importing Streamlit. Re-exported here: tests and callers address them as
# report_view.<name>, and that contract did not change when the file did.
# ---------------------------------------------------------------------------

from audit.reporting import (  # noqa: E402  (re-export, kept after the badges on purpose)
    CHECKLIST_DIR,
    FALLBACK_PRICE_PER_M,
    discover_packs,
    estimate_calls,
    estimate_cost_usd,
    pair_rows,
    per_control_rows,
)

__all__ = [
    "CHECKLIST_DIR", "FALLBACK_PRICE_PER_M", "discover_packs", "estimate_calls",
    "estimate_cost_usd", "pair_rows", "per_control_rows", "report_page",
]


# ---------------------------------------------------------------------------
# Streamlit-facing pieces
# ---------------------------------------------------------------------------


@st.cache_data(ttl=3600, show_spinner=False)
def _model_pricing(model: str) -> tuple[float, float] | None:
    return get_model_pricing(model)


def _render_pair(r: dict[str, Any]) -> None:
    """One pair, fully: verdict, rationale, citations, verifier, trace."""
    st.markdown(
        f"**{VERDICT_BADGES.get(r.get('verdict'))}** · {STATUS_BADGES[r['status']]} · "
        f"severity {r.get('severity', '?')} · {r.get('attempts', '?')} judge pass(es)"
    )
    if r["status"] == "error":
        st.error(r.get("error", "unknown error"))
        return
    st.markdown(f"_{r.get('rationale', '')}_")
    if r.get("citations"):
        st.markdown("**Citations:** " + ", ".join(f"`{c}`" for c in r["citations"]))
    check = r.get("citation_check") or {}
    if check.get("status") and check["status"] != "ok":
        st.warning(f"Citation check: {check['status']} - {check.get('note', '')}")
    ver = r.get("verification") or {}
    if ver.get("agrees") is False:
        st.warning(f"**Verifier would say `{ver.get('verdict')}`:** "
                   + (" · ".join(ver.get("issues")) or "no further detail"))
    elif ver.get("agrees") is None:
        st.warning("Verifier reply could not be parsed - parked for a human.")
    else:
        st.caption(f"Verifier agrees ({ver.get('model', '?')})")
    if r.get("security_flags"):
        paths = ", ".join(f["path"] for f in r["security_flags"])
        st.error(f"🛡️ Prompt-injection content quarantined at: {paths}")
    st.markdown("**Agent trace**")
    for i, line in enumerate(r.get("trace") or [], start=1):
        st.markdown(f"{i}. `{line}`")
    with st.expander("RAG trail", expanded=False):
        for q in r.get("evidence_queries") or []:
            st.markdown(f"- {q}")
        st.markdown("**Chunks:** " + ", ".join(f"`{c}`" for c in r.get("evidence_chunks") or []) or "-")


def _pair_list(rows: list[dict[str, Any]], empty: str) -> None:
    if not rows:
        st.info(empty)
        return
    for r in rows:
        title = (f"{r['ticket_id']} × {r['control_id']} - "
                 f"{VERDICT_BADGES.get(r.get('verdict'))} · {STATUS_BADGES[r['status']]}")
        with st.expander(title, expanded=False):
            _render_pair(r)


def _run(pack: ChecklistPack, tickets: dict, ticket_ids: list[str],
         control_ids: list[str]) -> BatchReport | None:
    """Run the batch under st.status with a progress bar; None on failure."""
    total = len(ticket_ids) * len(control_ids)
    status = st.status(f"Auditing {total} record-control pairs…", expanded=True)
    bar = status.progress(0.0)

    def on_progress(done: int, total_: int, label: str) -> None:
        bar.progress(done / total_, text=f"{done}/{total_} · {label}")

    try:
        report = run_batch(pack, tickets, ticket_ids, control_ids, progress=on_progress)
    except LLMError as exc:  # FR-15: user-safe by design
        status.update(label="The language model is unavailable", state="error")
        st.error(str(exc))
        return None
    except Exception:  # FR-15: never leak a stack trace into the UI
        status.update(label="The audit could not be completed", state="error")
        st.error("Something went wrong running the audit. Check the terminal log and try again.")
        return None
    status.update(label=f"Audited {total} pairs", state="complete", expanded=False)
    return report


def report_page() -> None:
    state = st.session_state.setdefault("report_view", {"report": None, "paths": None, "pack_label": None})

    # ---- sidebar: pack + scope -------------------------------------------
    with st.sidebar:
        st.title("📊 Audit report")
        packs = discover_packs()
        if not packs:
            st.error(f"No checklist packs found in {CHECKLIST_DIR}/")
            return
        labels = [p["label"] for p in packs]
        chosen = st.selectbox("Checklist pack", labels, index=0,
                              help="A standard is a YAML file. Add one, it appears here.")
        entry = packs[labels.index(chosen)]
        if entry["error"]:
            st.error(entry["error"])
            return
        pack: ChecklistPack = entry["pack"]
        st.caption(pack.pack.description)

        tickets = load_tickets(DEFAULT_CORPUS_DIR)
        all_controls = [c.control_id for c in pack.controls]
        control_ids = st.multiselect("Controls", all_controls, default=all_controls,
                                     format_func=lambda cid: pack.get_control(cid).title)
        ticket_ids = st.multiselect("Records", list(tickets), default=list(tickets))
        n = len(control_ids) * len(ticket_ids)
        lo, hi = estimate_calls(n)
        st.caption(f"{n} pairs → about {lo}-{hi} model calls (judge + verify, maybe one re-judge).")
        run_clicked = st.button("Run audit", type="primary", disabled=n == 0,
                                use_container_width=True)
        st.divider()
        st.markdown(f"**Model:** `{DEFAULT_MODEL}`")
        st.caption("Verifier uses the same model unless `AUDITREADY_VERIFY_MODEL` is set.")

    # ---- main --------------------------------------------------------------
    st.title("Audit-readiness report")

    if run_clicked:
        report = _run(pack, tickets, ticket_ids, control_ids)
        if report is not None:
            REPORT_DIR.mkdir(exist_ok=True)
            state["report"] = report
            state["paths"] = write_report(report, REPORT_DIR)
            state["pack_label"] = chosen
            st.rerun()

    report: BatchReport | None = state["report"]
    if report is None:
        st.info("Pick a pack and scope in the sidebar, then **Run audit**. "
                "The report appears here with the score, open gaps, the review queue "
                "and the agent trace for every pair.")
        return

    st.caption(f"Pack **{report.standard}** (`{report.pack_id}`) · generated {report.generated_at} · "
               f"{len(report.ticket_ids)} records × {len(report.control_ids)} controls")

    # -- headline numbers ----------------------------------------------------
    pricing = _model_pricing(DEFAULT_MODEL)
    cost, note = estimate_cost_usd(report.prompt_tokens, report.completion_tokens, pricing)
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Readiness", "-" if report.readiness_score is None else f"{report.readiness_score:.0%}",
              help="Severity-weighted (major ×3, minor ×1); partial counts half; N/A excluded. "
                   "Verified verdicts only.")
    c2.metric("Verified pairs", f"{report.verified_pairs} / {report.total_pairs}")
    c3.metric("Open gaps", len(report.open_gaps))
    c4.metric("Needs review", len(report.review_queue),
              help="Verifier still objected after one re-judge, or could not be parsed. Not in the score.")
    c5.metric("Est. cost", f"${cost:.4f}", help=f"{report.prompt_tokens:,} prompt + "
              f"{report.completion_tokens:,} completion tokens · {note}")
    if report.errors:
        st.warning(f"{len(report.errors)} pair(s) failed to run and are excluded from the score.")

    # -- per control ---------------------------------------------------------
    st.subheader("Per control")
    st.dataframe(per_control_rows(report), use_container_width=True, hide_index=True,
                 column_config={"Satisfied rate": st.column_config.ProgressColumn(
                     "Satisfied rate", format="%d%%", min_value=0, max_value=100)})

    # -- work lists + trace --------------------------------------------------
    tabs = st.tabs([f"Open gaps ({len(report.open_gaps)})",
                    f"Needs review ({len(report.review_queue)})",
                    f"All pairs ({report.total_pairs})",
                    f"Errors ({len(report.errors)})"])
    with tabs[0]:
        _pair_list(report.open_gaps, "No open gaps - every verified pair is satisfied or not applicable.")
    with tabs[1]:
        _pair_list(report.review_queue, "Nothing parked - the verifier agreed with every verdict.")
    with tabs[2]:
        st.dataframe(pair_rows(report.rows), use_container_width=True, hide_index=True)
        _pair_list(report.rows, "No pairs.")
    with tabs[3]:
        _pair_list(report.errors, "No errors.")

    # -- export (FR-6) -------------------------------------------------------
    st.subheader("Export")
    d1, d2 = st.columns(2)
    stem = f"auditready-{report.pack_id}-{report.generated_at[:10]}"
    d1.download_button("Download report (Markdown)", render_markdown(report),
                       file_name=f"{stem}.md", mime="text/markdown", use_container_width=True)
    d2.download_button("Download report (JSON)", json.dumps(report.to_dict(), indent=2, default=str),
                       file_name=f"{stem}.json", mime="application/json", use_container_width=True)
    if state["paths"]:
        md, js = state["paths"]
        st.caption(f"Also written to `{md}` and `{js}`.")
