"""AuditReady — Streamlit chat UI (FR-11, 12, 15, 16, 19, 20 + FR-9, FR-17).

THE RULE OF THIS FILE: no business logic. Every capability lives in core/
or assistant/agent.py; this file only renders. V2 swaps this shell
for React+FastAPI (CR-8) — anything written here beyond presentation would
have to be rewritten there, so if a line makes a decision about auditing,
it's in the wrong file.

Run:  uv run streamlit run assistant/app.py

Pages: "Compliance chat" (this file) and, when the audit package is
present, "Audit report" (audit/report_view.py) — same shell, two panels.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from pathlib import Path

import streamlit as st

from core.guardrails import GuardrailViolation
from core.llm import LLMError, get_model_pricing
from core.retrieval import ingest, store_is_empty, store_label
from assistant.agent import DEFAULT_MODEL, AuditAgent

# Fallback price sheet (FR-17): gpt-4o-mini's rate, USD per 1M tokens. Used
# only when the live lookup below fails (offline demo, OpenRouter hiccup) —
# and the sidebar labels which of the two it is showing.
FALLBACK_PRICE_PER_M = (0.15, 0.60)


@st.cache_data(ttl=3600, show_spinner=False)
def _model_pricing(model: str) -> tuple[float, float] | None:
    """Live OpenRouter rates, cached for an hour. st.cache_data (not
    session_state) because pricing is per-model, not per-conversation —
    one fetch serves every session and rerun until the TTL expires."""
    return get_model_pricing(model)

VERDICT_BADGES = {
    "satisfied": "🟢 satisfied",
    "partial": "🟡 partial",
    "gap": "🔴 gap",
    "not_applicable": "⚪ not applicable",
}

_MARK = str(Path(__file__).resolve().parents[1] / "docs" / "brand" / "auditready-mark.png")
st.set_page_config(page_title="AuditReady", page_icon=_MARK, layout="wide")
st.logo(_MARK, size="medium")


# ---------------------------------------------------------------------------
# Session state: the agent (holds conversation memory) + a render log.
# st.session_state survives Streamlit's rerun-on-every-interaction model —
# without it we'd build a fresh agent (and lose memory) on every click.
# ---------------------------------------------------------------------------

if "agent" not in st.session_state:
    st.session_state.agent = None
    st.session_state.turns = []  # [{question, answer, tool_calls, tokens, error}]
    st.session_state.agent_error = None


def _get_agent() -> AuditAgent | None:
    if st.session_state.agent is None:
        try:
            st.session_state.agent = AuditAgent()
            st.session_state.agent_error = None
        except Exception as exc:  # missing key etc. — show, don't crash
            st.session_state.agent_error = str(exc)
    return st.session_state.agent


# ---------------------------------------------------------------------------
# Render helpers (presentation only)
# ---------------------------------------------------------------------------


def _render_check_result(result: dict) -> None:
    """One check_ticket verdict: badge, rationale, citations, RAG trail."""
    badge = VERDICT_BADGES.get(result.get("verdict", ""), result.get("verdict", "?"))
    st.markdown(
        f"**{result.get('control_id', '?')}** — {badge} "
        f"(severity: {result.get('severity', '?')})"
    )
    st.markdown(f"_{result.get('rationale', '')}_")
    if result.get("citations"):
        st.markdown("**Citations:** " + ", ".join(f"`{c}`" for c in result["citations"]))
    check = result.get("citation_check", {})
    if check.get("status") and check["status"] != "ok":
        st.warning(f"Citation check: {check['status']} — {check.get('note', '')}")
    if result.get("security_flags"):
        paths = ", ".join(f["path"] for f in result["security_flags"])
        st.error(f"🛡️ Prompt-injection attempt quarantined at: {paths} — needs human review")
    with st.expander("RAG trail — how evidence was searched", expanded=False):
        st.markdown("**Evidence sub-queries** (decomposed from the rubric):")
        for q in result.get("evidence_queries", []):
            st.markdown(f"- {q}")
        st.markdown(
            "**Evidence chunks retrieved:** "
            + ", ".join(f"`{c}`" for c in result.get("evidence_chunks", []))
        )


def _render_tool_call(call: dict) -> None:
    """One tool invocation inside the tools expander."""
    st.markdown(f"🔧 **`{call['name']}`** `{json.dumps(call['args'])}`")
    result = call.get("result")

    if call["name"] == "check_ticket" and isinstance(result, dict):
        for r in result.get("results", [result]):  # single- or all-controls shape
            if isinstance(r, dict) and "verdict" in r:
                _render_check_result(r)
    elif call["name"] == "find_evidence" and isinstance(result, list):
        for hit in result:
            flagged = "🛡️ " if hit.get("security_flags") else ""
            st.markdown(
                f"- {flagged}`{hit['chunk_id']}` (similarity {hit['similarity']})"
            )
            st.caption(hit["text"][:300])
    elif call["name"] == "coverage_report" and isinstance(result, dict):
        score = result.get("readiness_score")
        if score is not None:
            st.metric("Readiness score", f"{score:.1%}")
        for cid, row in result.get("per_control", {}).items():
            st.markdown(f"- **{cid}** — {row['counts']}")
    else:
        st.code(json.dumps(result, indent=2)[:2000] if not isinstance(result, str) else result[:2000])


def _turn_has_security_flags(turn: dict) -> bool:
    return "QUARANTINED" in json.dumps(turn.get("tool_calls", []))


# ---------------------------------------------------------------------------
# Sidebar: status, cost, maintenance, export (FR-17, FR-9)
# ---------------------------------------------------------------------------

def chat_page() -> None:
    """The v1 chatbot, now one page of a multipage app (see bottom).
    Body unchanged from the v1 deliverable — only indented."""
    with st.sidebar:
        st.title("📋 AuditReady")
        st.caption(
            "Grounded, cited compliance answers over tickets, emails and change "
            "records — ISO 9001 sample pack."
        )
        st.divider()

        st.markdown(f"**Model:** `{DEFAULT_MODEL}`")
        agent = _get_agent()
        if agent is not None:
            p, c = agent.total_prompt_tokens, agent.total_completion_tokens
            pricing = _model_pricing(DEFAULT_MODEL)
            price_in, price_out = pricing or FALLBACK_PRICE_PER_M
            price_note = "live OpenRouter rate" if pricing else "fallback list price"
            cost = p / 1e6 * price_in + c / 1e6 * price_out
            st.markdown(
                f"**Session tokens:** {p + c:,}  \n"
                f"prompt {p:,} / completion {c:,}  \n"
                f"**Est. cost:** ${cost:.4f} _({price_note}, estimate)_"
            )
        st.divider()

        if store_is_empty():
            st.warning(f"Vector index {store_label()} is empty — build it before asking questions.")
        if st.button("Rebuild vector index"):
            with st.spinner("Chunking + embedding corpus…"):
                n = ingest()
            st.success(f"Ingested {n} chunks.")

        if st.session_state.turns:
            st.download_button(  # FR-9: conversation export
                "Export conversation (JSON)",
                data=json.dumps(
                    {
                        "exported_at": datetime.now(timezone.utc).isoformat(),
                        "model": DEFAULT_MODEL,
                        "turns": st.session_state.turns,
                    },
                    indent=2,
                ),
                file_name="auditready_conversation.json",
                mime="application/json",
            )

        if st.button("New conversation"):
            st.session_state.agent = None
            st.session_state.turns = []
            st.rerun()

        st.divider()
        st.caption(
            "Try: *“Is TCK-1002 audit-ready?”* · *“Which of TCK-1001…TCK-1005 "
            "fail release sign-off?”* · *“Do we have client approval for the "
            "dispatch email delay fix?”*"
        )


    # ---------------------------------------------------------------------------
    # Main pane: chat history, then input (FR-11, FR-12, FR-16, FR-20)
    # ---------------------------------------------------------------------------

    st.title("Compliance chat")

    if st.session_state.agent_error:
        st.error(st.session_state.agent_error)

    for turn in st.session_state.turns:
        with st.chat_message("user"):
            st.markdown(turn["question"])
        with st.chat_message("assistant"):
            if turn.get("error"):
                st.error(turn["error"])
                continue
            if _turn_has_security_flags(turn):
                st.error("🛡️ Security notice: suspected prompt-injection content was quarantined this turn.")
            st.markdown(turn["answer"])
            if turn["tool_calls"]:
                with st.expander(
                    f"Tools & sources ({len(turn['tool_calls'])} call(s))", expanded=False
                ):
                    for call in turn["tool_calls"]:
                        _render_tool_call(call)
            st.caption(
                f"{turn['prompt_tokens']:,} prompt + {turn['completion_tokens']:,} "
                f"completion tokens"
            )

    if question := st.chat_input("Ask about a ticket, a control, or overall readiness…"):
        agent = _get_agent()
        if agent is None:
            st.stop()

        with st.chat_message("user"):
            st.markdown(question)

        turn = {
            "question": question,
            "answer": "",
            "tool_calls": [],
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "error": None,
        }
        with st.chat_message("assistant"):
            # FR-16: st.status instead of a bare spinner — a coverage sweep is
            # up to 180 LLM judgments, and a silent spinner for minutes reads
            # as "hung". The agent forwards per-judgment progress from
            # coverage_report's thread-pool loop (called on this thread, so
            # updating widgets here is safe), and the bar appears only when a
            # sweep actually starts — quick questions just see the status line.
            status = st.status("Consulting the audit tools…")  # FR-20
            _bar: list = []  # created lazily on the first progress event

            def _on_progress(done: int, total: int, label: str) -> None:
                if not _bar:
                    status.update(
                        label=f"Coverage sweep: {total} judgments…", expanded=True
                    )
                    _bar.append(status.progress(0.0))
                _bar[0].progress(done / total, text=f"{done}/{total} · {label}")

            try:
                reply = agent.ask(question, progress=_on_progress)
                turn.update(
                    answer=reply.content,
                    tool_calls=reply.tool_calls,
                    prompt_tokens=reply.prompt_tokens,
                    completion_tokens=reply.completion_tokens,
                )
                status.update(label="Audit tools consulted", state="complete", expanded=False)
            except GuardrailViolation as exc:  # FR-10: safe to show verbatim
                turn["error"] = str(exc)
            except LLMError as exc:  # FR-15: user-safe by design
                turn["error"] = f"The language model is unavailable: {exc}"
            except Exception:  # FR-15: never leak stack traces into the UI
                turn["error"] = (
                    "Something went wrong answering that. Please try again or "
                    "rephrase the question."
                )
            if turn["error"]:
                status.update(label="The question could not be answered", state="error", expanded=False)

        st.session_state.turns.append(turn)
        st.rerun()  # re-render so the new turn joins history (and sidebar totals update)


# ---------------------------------------------------------------------------
# Pages. V2 adds the batch audit report as a second page (plan
# item 5 — "the chatbot becomes one panel", CR-8's idea without the React
# rewrite). The import is optional so a chat-only checkout still runs.
# ---------------------------------------------------------------------------


def _pages() -> list:
    pages = [st.Page(chat_page, title="Compliance chat", icon="💬", url_path="chat", default=True)]
    try:
        from audit.report_view import report_page
    except ImportError:
        return pages
    pages.append(st.Page(report_page, title="Audit report", icon="📊", url_path="report"))
    return pages


st.navigation(_pages()).run()
