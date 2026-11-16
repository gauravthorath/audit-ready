"""LangChain tool-calling agent over the core tools (FR-8, NFR-2).

What the framework actually adds - and what it doesn't:
- core/tools.py already contains ALL the capability. LangChain contributes
  three things here: (1) `@tool` turns a Python function's signature +
  docstring into the JSON schema the model sees, (2) `bind_tools` attaches
  those schemas to every request, (3) typed message objects (AIMessage,
  ToolMessage) that keep the conversation structured. That's it. The
  framework is plumbing, not intelligence.
- The tool-calling LOOP is written out by hand below (~20 lines) instead of
  using a prebuilt agent wrapper, deliberately: this is the thing worth
  understanding, and prebuilt wrappers (AgentExecutor, create_agent) are
  just this loop with more configuration surface. V2's LangGraph
  agent (CR-1) replaces the loop with an explicit graph - same idea, more
  control.

The loop demystified:
    1. send messages (with tool schemas attached)
    2. model replies EITHER with text (done) OR tool_calls - a structured
       "please run find_evidence(query=...)" request; the model cannot run
       anything itself
    3. we execute the named core function, append the result as a
       ToolMessage, go to 1
    A round cap prevents infinite tool ping-pong.

Memory (NFR-2) is nothing magical either: the message list persists on the
AuditAgent instance across ask() calls, so "what about its client comms?"
resolves against the previous turn. A sliding window caps token growth.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable

from dotenv import load_dotenv
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    messages_from_dict,
    messages_to_dict,
)
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from core import tools as core_tools
from core.guardrails import validate_user_input
from core.llm import llm_api_key, llm_base_url
from core.tracing import elapsed_ms, log_event
from assistant.prompts import AGENT_SYSTEM_PROMPT

load_dotenv()

# The chat agent talks to the model through LangChain, not core.llm.chat(), so
# it logs its own `llm.call` lines - same event name, same fields, plus
# `transport=langchain` so the two paths can be told apart in one grep.
log = logging.getLogger("auditready.agent")

DEFAULT_MODEL = os.getenv("AUDITREADY_MODEL", "openai/gpt-4o-mini")

MAX_TOOL_ROUNDS = 6  # a sane audit question resolves in 1-3; 6 = runaway brake
MEMORY_WINDOW = 40  # keep system prompt + the most recent N messages


# ---------------------------------------------------------------------------
# Tool wrappers - thin adapters, zero logic (FR-7 kept intact)
#
# The docstrings below are NOT documentation for humans - they are the tool
# descriptions the MODEL reads when deciding which tool to call. Treat them
# as UI copy for an audience of one LLM: say when to use the tool, what the
# arguments mean, and what comes back. The `@tool` decorator lifts the
# signature + docstring into a JSON schema automatically.
# ---------------------------------------------------------------------------


@tool
def find_evidence(
    query: str, ticket_id: str | None = None, doc_type: str | None = None, k: int = 5
) -> str:
    """Semantic search over the audit corpus (tickets, ticket comments,
    emails, change records). Use for open questions like "is there any
    approval email for X?" or to locate artifacts before judging.

    Args:
        query: natural-language description of the evidence sought.
        ticket_id: optional - restrict to one ticket's cluster, e.g. "TCK-1002".
        doc_type: optional - one of "ticket", "ticket_comment", "email", "change".
        k: number of results (default 5).

    Returns JSON list of {chunk_id, text, similarity, doc_type, doc_id};
    chunk_id is the citation handle.
    """
    return json.dumps(core_tools.find_evidence(query, ticket_id, doc_type, k))


@tool
def check_ticket(ticket_id: str, control_id: str | None = None) -> str:
    """Judge one ticket against a compliance control (or ALL controls if
    control_id is omitted - slower, several LLM calls). Use whenever asked
    whether a ticket is compliant / audit-ready / passing a control.

    Args:
        ticket_id: e.g. "TCK-1002".
        control_id: e.g. "ISO9001-8.6-release-signoff". Omit for a full check.

    Returns JSON with verdict (satisfied|partial|gap|not_applicable),
    rationale, citations, citation_check, security_flags, and the evidence
    trail (sub-queries + chunk ids).
    """
    return json.dumps(core_tools.check_ticket(ticket_id, control_id))


# Progress plumbing (FR-16): coverage_report can run for minutes, and the
# LangChain tool schema must NOT know about progress callbacks (the model
# would try to fill the argument). So the callback travels out-of-band: ask()
# parks it in this module global for the duration of one turn, and the tool
# wrapper picks it up. Safe because an AuditAgent turn is single-threaded -
# only coverage_report's INTERNALS fan out, and it calls back on our thread.
_progress_callback: Callable[[int, int, str], None] | None = None


@tool
def coverage_report(
    ticket_ids: list[str] | None = None, control_ids: list[str] | None = None
) -> str:
    """Aggregate audit-readiness report: verdicts for every (ticket, control)
    pair, per-control satisfaction rates, failing tickets, and a weighted
    readiness score. EXPENSIVE - one LLM judgment per pair; unscoped it
    checks the whole corpus (~180 calls). Prefer passing explicit
    ticket_ids and/or control_ids whenever the question allows it.

    Args:
        ticket_ids: optional list like ["TCK-1001", "TCK-1002"].
        control_ids: optional list of control ids.
    """
    return json.dumps(
        core_tools.coverage_report(ticket_ids, control_ids, progress=_progress_callback)
    )


_TOOLS = [find_evidence, check_ticket, coverage_report]
_TOOLS_BY_NAME = {t.name: t for t in _TOOLS}


# ---------------------------------------------------------------------------
# System prompt - hardened per Step 8's threat model. The wording lives in
# assistant/prompts.py (companion to core/prompts.py) so prompt changes diff
# as prompt changes; this module fills in the {control_catalog} placeholder.
# ---------------------------------------------------------------------------


def _control_catalog() -> str:
    """One line per control from the loaded pack. Injected into the system
    prompt so the agent maps user phrasing ("sign-off", "customer comms")
    to exact control_ids by TITLE instead of guessing ids from vibes -
    the smoke test caught it inventing a mapping to the wrong control.
    ~9 lines ≈ trivial tokens, paid once per conversation."""
    return "\n".join(
        f"- {c.control_id} ({c.severity}): {c.title}"
        for c in core_tools._pack().controls
    )


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------


def _count(value: Any) -> int:
    """A token counter off a stored record: a number, or zero. Never an exception."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


@dataclass
class AgentReply:
    """One turn's outcome, with the accounting the UI shows (FR-17).

    tool_calls entries are {"name", "args", "result"} in execution order -
    result is the parsed JSON (dict/list) when the tool returned JSON, else
    the raw string. Carrying results here is what feeds the UI's sources /
    RAG-visualisation panel without the UI re-running anything.
    """

    content: str
    tool_calls: list[dict] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0


class AuditAgent:
    """A conversation: one system prompt, persistent message history,
    and the hand-rolled tool-calling loop."""

    def __init__(self, model: str | None = None):
        # LangChain gets its own client handle (as designed in Step 2 -
        # core/llm.py serves the plain tools; the framework layer lives out
        # here in assistant/, never inside core/). What it must NOT get is its
        # own idea of where the endpoint is: a self-hosted deployment (CR-4)
        # where the chat page still calls OpenRouter while the report engine
        # calls the local model would leak exactly the data the deployment
        # exists to keep in. Hence the shared resolvers.
        self._model = model or DEFAULT_MODEL
        self._llm = ChatOpenAI(
            model=self._model,
            api_key=llm_api_key(),
            base_url=llm_base_url(),
            temperature=0,
        ).bind_tools(_TOOLS)
        self._messages: list[BaseMessage] = [
            SystemMessage(
                content=AGENT_SYSTEM_PROMPT.format(control_catalog=_control_catalog())
            )
        ]
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    # -- memory -------------------------------------------------------------

    def _trim_history(self) -> None:
        """Sliding window (NFR-2): keep the system prompt + the most recent
        MEMORY_WINDOW messages. Care at the seam: history must never START
        with a ToolMessage whose AIMessage (the tool_calls request) was
        trimmed away - providers reject orphaned tool results."""
        if len(self._messages) <= 1 + MEMORY_WINDOW:
            return
        tail = self._messages[-MEMORY_WINDOW:]
        while tail and isinstance(tail[0], ToolMessage):
            tail = tail[1:]
        self._messages = [self._messages[0], *tail]

    # -- serialisation (CR-8, Step 41) -----------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """The conversation as plain data, so it can outlive this process.

        The turns are stored; the *system prompt is not*. It is rebuilt from
        `assistant/prompts.py` and the live control catalogue whenever an agent is
        constructed, so a conversation resumed after a checklist pack changed is
        answered against the packs that exist now rather than a snapshot of the
        ones that existed when it started. Storing it would freeze an
        instruction that is meant to track the deployment.
        """
        return {
            "messages": messages_to_dict(self._messages[1:]),
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "model": self._model,
        }

    def restore(self, state: Mapping[str, Any]) -> None:
        """Rebuild a conversation from `snapshot()`. Tolerant of an older record shape.

        A stored session that cannot be read is a *new* conversation, not a
        failed request: the alternative is a 500 on every turn until someone
        clears a table, and the cost of the fallback is one lost history.
        """
        try:
            restored = messages_from_dict(list(state.get("messages") or []))
        except Exception:  # noqa: BLE001 - an unreadable record must not sink the turn
            restored = []
        self._messages = [self._messages[0], *restored]
        # The counters are read the same defensive way, and for the same reason:
        # they come from a database row, and a row is data, not a promise.
        self.total_prompt_tokens = _count(state.get("prompt_tokens"))
        self.total_completion_tokens = _count(state.get("completion_tokens"))
        self._trim_history()  # the window applies to a restored history too

    # -- the loop -------------------------------------------------------------

    def ask(
        self,
        user_text: str,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> AgentReply:
        """One user turn: validate -> loop(model, tools) -> final text.

        progress, if given, receives (done, total, label) updates from
        long-running tools (currently coverage_report) so the UI can show
        a live progress bar instead of a silent spinner (FR-16).
        """
        global _progress_callback
        user_text = validate_user_input(user_text)  # guardrail layer 0 (FR-10)
        self._messages.append(HumanMessage(content=user_text))

        reply = AgentReply(content="")
        _progress_callback = progress
        try:
            reply = self._run_tool_loop(reply)
        finally:
            _progress_callback = None  # never leak one turn's UI into the next

        self.total_prompt_tokens += reply.prompt_tokens
        self.total_completion_tokens += reply.completion_tokens
        self._trim_history()
        return reply

    def _run_tool_loop(self, reply: AgentReply) -> AgentReply:
        for _round in range(MAX_TOOL_ROUNDS):
            started = time.perf_counter()
            response: AIMessage = self._llm.invoke(self._messages)
            self._messages.append(response)

            usage = response.usage_metadata or {}
            reply.prompt_tokens += usage.get("input_tokens", 0)
            reply.completion_tokens += usage.get("output_tokens", 0)
            log_event(log, "llm.call", transport="langchain", model=self._model,
                      round=_round + 1, elapsed_ms=elapsed_ms(started),
                      prompt_tokens=usage.get("input_tokens", 0),
                      completion_tokens=usage.get("output_tokens", 0),
                      tool_calls=len(response.tool_calls or []), outcome="ok")

            if not response.tool_calls:  # plain text -> the turn is done
                # `.text` is a property in langchain 1.x and was a method
                # before. The 1.x value is a str subclass that stays callable
                # for back-compat and warns when called, so the str check has
                # to come first or the deprecated path is the one taken.
                text = getattr(response, "text", None)
                if isinstance(text, str):
                    reply.content = text
                elif callable(text):
                    reply.content = text()
                else:
                    reply.content = str(response.content)
                break

            for call in response.tool_calls:
                requested = _TOOLS_BY_NAME.get(call["name"])
                tool_started = time.perf_counter()
                try:
                    if requested is None:
                        result = f"error: unknown tool {call['name']!r}"
                    else:
                        result = requested.invoke(call["args"])
                except Exception as exc:  # tool failure -> the model decides
                    # what to do next (retry, different tool, apologise) -
                    # crashing the turn would waste everything done so far
                    result = f"error: {type(exc).__name__}: {exc}"
                log_event(log, "agent.tool", tool=call["name"],
                          elapsed_ms=elapsed_ms(tool_started),
                          outcome="error" if str(result).startswith("error:") else "ok")
                try:
                    parsed = json.loads(result)
                except (TypeError, json.JSONDecodeError):
                    parsed = str(result)
                reply.tool_calls.append(
                    {"name": call["name"], "args": call["args"], "result": parsed}
                )
                self._messages.append(
                    ToolMessage(content=str(result), tool_call_id=call["id"])
                )
        else:
            reply.content = (
                "I hit the tool-call limit for a single question. Please "
                "narrow the question (fewer tickets or a specific control)."
            )

        return reply
