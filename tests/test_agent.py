"""Hermetic tests for assistant/agent.py — the hand-rolled tool-calling loop.

No network, no key. `ChatOpenAI` is replaced by a scripted fake, so what is
under test is the ~40 lines the framework does NOT write for us (FR-8, NFR-2):

- a plain-text reply ends the turn; a tool_calls reply does not
- the round cap actually caps, and says so instead of returning empty
- a tool that raises, or a name the model invented, becomes text the model
  can react to — never a crashed turn
- token accounting survives multi-round turns and lands on the instance
- the sliding window keeps the system prompt and never leaves a ToolMessage
  orphaned at the head of history (providers reject that)
- the out-of-band progress callback is scoped to one turn
- the LLM is wired from the shared resolvers in core/llm.py, so a self-hosted
  deployment cannot end up with the chat page still calling OpenRouter (CR-4)

The LLM-shaped seams (does the model choose a sensible tool?) stay in
scripts/smoke_agent.py, where a real model answers.
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

import assistant.agent as agent
from core.guardrails import GuardrailViolation


# ---------------------------------------------------------------------------
# Scripted stand-ins
# ---------------------------------------------------------------------------


class _FakeLLM:
    """What `ChatOpenAI(...).bind_tools(...)` returns: a scripted invoke()."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.seen: list[list] = []  # the message list handed over each round
        self.bound_tools: list = []

    def invoke(self, messages):
        self.seen.append(list(messages))
        if not self._replies:
            raise AssertionError("the loop ran more rounds than were scripted")
        return self._replies.pop(0)


def _install(monkeypatch, *replies):
    """Build an AuditAgent whose LLM is scripted. Returns (agent, fake, kwargs)."""
    fake = _FakeLLM(replies)
    captured: dict = {}

    class _FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def bind_tools(self, tools):
            fake.bound_tools = tools
            return fake

    monkeypatch.setattr(agent, "ChatOpenAI", _FakeChatOpenAI)
    monkeypatch.setattr(agent, "llm_api_key", lambda: "key-from-the-shared-resolver")
    monkeypatch.setattr(agent, "llm_base_url", lambda: "http://endpoint.invalid/v1")
    return agent.AuditAgent(), fake, captured


def _says(text: str, *, prompt: int = 0, completion: int = 0) -> AIMessage:
    return AIMessage(
        content=text,
        usage_metadata={
            "input_tokens": prompt,
            "output_tokens": completion,
            "total_tokens": prompt + completion,
        },
    )


def _wants(name: str, args: dict, call_id: str = "call_1", **usage) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id}],
        usage_metadata={
            "input_tokens": usage.get("prompt", 0),
            "output_tokens": usage.get("completion", 0),
            "total_tokens": usage.get("prompt", 0) + usage.get("completion", 0),
        },
    )


# ---------------------------------------------------------------------------
# Ending the turn
# ---------------------------------------------------------------------------


def test_a_plain_text_reply_ends_the_turn_in_one_round(monkeypatch):
    a, fake, _ = _install(monkeypatch, _says("TCK-1001 is satisfied.", prompt=11, completion=4))

    reply = a.ask("Is TCK-1001 audit-ready?")

    assert reply.content == "TCK-1001 is satisfied."
    assert reply.tool_calls == []
    assert len(fake.seen) == 1
    assert (reply.prompt_tokens, reply.completion_tokens) == (11, 4)


def test_text_is_read_as_a_property_not_called(monkeypatch):
    # langchain 1.x returns a str subclass that stays callable and warns when
    # called. The str branch has to win, or every turn takes the deprecated
    # path — the regression this file exists partly to hold.
    a, _, _ = _install(monkeypatch, _says("plain"))

    with _no_deprecation():
        reply = a.ask("hello")

    assert reply.content == "plain"


class _no_deprecation:
    """`.text()` warns; assert the loop never triggers it."""

    def __enter__(self):
        import warnings

        self._ctx = warnings.catch_warnings(record=True)
        self._log = self._ctx.__enter__()
        warnings.simplefilter("always")
        return self

    def __exit__(self, *exc):
        wording = [str(w.message) for w in self._log]
        self._ctx.__exit__(*exc)
        assert not any("deprecated" in w.lower() for w in wording), wording
        return False


def test_a_response_without_text_falls_back_to_its_content(monkeypatch):
    class _Bare:
        tool_calls: list = []
        usage_metadata = None
        content = "content-only"

    a, _, _ = _install(monkeypatch, _Bare())

    assert a.ask("hello").content == "content-only"


# ---------------------------------------------------------------------------
# Dispatching tools
# ---------------------------------------------------------------------------


def test_a_requested_tool_runs_and_its_result_goes_back_as_a_tool_message(monkeypatch):
    monkeypatch.setattr(
        agent.core_tools, "check_ticket", lambda t, c=None: {"ticket_id": t, "verdict": "gap"}
    )
    a, fake, _ = _install(
        monkeypatch,
        _wants("check_ticket", {"ticket_id": "TCK-1009"}, "call_a"),
        _says("It is a gap."),
    )

    reply = a.ask("Check TCK-1009")

    assert reply.content == "It is a gap."
    assert len(fake.seen) == 2  # the tool result bought a second round
    # the parsed result is what the UI's sources panel reads
    assert reply.tool_calls == [
        {
            "name": "check_ticket",
            "args": {"ticket_id": "TCK-1009"},
            "result": {"ticket_id": "TCK-1009", "verdict": "gap"},
        }
    ]
    # ... and the wire form is a ToolMessage carrying the model's own call id
    handed_back = fake.seen[1][-1]
    assert isinstance(handed_back, ToolMessage)
    assert handed_back.tool_call_id == "call_a"
    assert json.loads(handed_back.content)["verdict"] == "gap"


def test_a_non_json_tool_result_is_kept_as_a_string(monkeypatch):
    monkeypatch.setattr(agent, "_TOOLS_BY_NAME", {**agent._TOOLS_BY_NAME})
    monkeypatch.setitem(
        agent._TOOLS_BY_NAME, "check_ticket", _stub_tool("not json at all")
    )
    a, _, _ = _install(
        monkeypatch, _wants("check_ticket", {"ticket_id": "TCK-1"}), _says("ok")
    )

    reply = a.ask("check")

    assert reply.tool_calls[0]["result"] == "not json at all"


def _stub_tool(returns):
    class _Stub:
        def invoke(self, args):
            if isinstance(returns, Exception):
                raise returns
            return returns

    return _Stub()


def test_the_find_evidence_wrapper_hands_the_model_json(monkeypatch):
    monkeypatch.setattr(
        agent.core_tools,
        "find_evidence",
        lambda q, t, d, k: [{"chunk_id": "c1", "text": "approved by QA"}],
    )
    a, _, _ = _install(
        monkeypatch,
        _wants("find_evidence", {"query": "sign-off", "ticket_id": "TCK-1001"}),
        _says("Cited c1."),
    )

    reply = a.ask("who signed TCK-1001 off?")

    assert reply.tool_calls[0]["result"] == [{"chunk_id": "c1", "text": "approved by QA"}]


def test_a_provider_that_returns_a_plain_callable_text_still_works(monkeypatch):
    # Belt and braces for the other side of the langchain 1.x seam: if some
    # provider hands back a callable that is not a str, call it.
    class _Callable:
        tool_calls: list = []
        usage_metadata = None
        content = "unused"

        def text(self):
            return "called, not read"

    a, _, _ = _install(monkeypatch, _Callable())

    assert a.ask("hello").content == "called, not read"


def test_an_invented_tool_name_becomes_text_the_model_can_react_to(monkeypatch):
    a, fake, _ = _install(
        monkeypatch, _wants("summarise_everything", {}), _says("Sorry, retrying.")
    )

    reply = a.ask("do the thing")

    assert reply.tool_calls[0]["result"].startswith("error: unknown tool")
    assert reply.content == "Sorry, retrying."  # the turn survived


def test_a_tool_that_raises_does_not_crash_the_turn(monkeypatch):
    monkeypatch.setattr(agent, "_TOOLS_BY_NAME", {**agent._TOOLS_BY_NAME})
    monkeypatch.setitem(
        agent._TOOLS_BY_NAME, "check_ticket", _stub_tool(ValueError("no such ticket"))
    )
    a, _, _ = _install(
        monkeypatch, _wants("check_ticket", {"ticket_id": "TCK-999"}), _says("It does not exist.")
    )

    reply = a.ask("check TCK-999")

    assert reply.tool_calls[0]["result"] == "error: ValueError: no such ticket"
    assert reply.content == "It does not exist."


# ---------------------------------------------------------------------------
# The brakes
# ---------------------------------------------------------------------------


def test_the_round_cap_stops_the_loop_and_says_why(monkeypatch):
    monkeypatch.setattr(agent.core_tools, "check_ticket", lambda t, c=None: {"verdict": "gap"})
    forever = [
        _wants("check_ticket", {"ticket_id": "TCK-1"}, f"call_{i}")
        for i in range(agent.MAX_TOOL_ROUNDS)
    ]
    a, fake, _ = _install(monkeypatch, *forever)

    reply = a.ask("check everything")

    assert len(fake.seen) == agent.MAX_TOOL_ROUNDS  # capped, not runaway
    assert "tool-call limit" in reply.content
    assert "narrow the question" in reply.content


def test_tokens_accumulate_across_rounds_and_across_turns(monkeypatch):
    monkeypatch.setattr(agent.core_tools, "check_ticket", lambda t, c=None: {"verdict": "gap"})
    a, _, _ = _install(
        monkeypatch,
        _wants("check_ticket", {"ticket_id": "TCK-1"}, prompt=100, completion=10),
        _says("done", prompt=200, completion=20),
        _says("again", prompt=7, completion=1),
    )

    first = a.ask("check TCK-1")
    assert (first.prompt_tokens, first.completion_tokens) == (300, 30)

    a.ask("and again")
    assert (a.total_prompt_tokens, a.total_completion_tokens) == (307, 31)


def test_an_empty_question_is_refused_before_any_model_call(monkeypatch):
    a, fake, _ = _install(monkeypatch)  # nothing scripted: a call would fail loudly

    with pytest.raises(GuardrailViolation):
        a.ask("   ")

    assert fake.seen == []
    assert len(a._messages) == 1  # the system prompt, and nothing appended


# ---------------------------------------------------------------------------
# Memory (NFR-2)
# ---------------------------------------------------------------------------


def test_short_conversations_are_left_alone(monkeypatch):
    a, _, _ = _install(monkeypatch)
    a._messages += [_says(f"turn {i}") for i in range(agent.MEMORY_WINDOW)]
    before = list(a._messages)

    a._trim_history()

    assert a._messages == before


def test_trimming_keeps_the_system_prompt_and_never_orphans_a_tool_result(monkeypatch):
    a, _, _ = _install(monkeypatch)
    a._messages += [_says(f"old {i}") for i in range(10)]
    a._messages += [
        _wants("check_ticket", {"ticket_id": "TCK-1"}, "call_x"),
        ToolMessage(content="{}", tool_call_id="call_x"),
        ToolMessage(content="{}", tool_call_id="call_x"),
    ]
    a._messages += [_says(f"recent {i}") for i in range(agent.MEMORY_WINDOW - 2)]
    # the hazard this guard exists for: the naive window starts on a tool result
    assert isinstance(a._messages[-agent.MEMORY_WINDOW], ToolMessage)

    a._trim_history()

    assert isinstance(a._messages[0], SystemMessage)
    assert not isinstance(a._messages[1], ToolMessage)
    assert len(a._messages) <= 1 + agent.MEMORY_WINDOW


# ---------------------------------------------------------------------------
# Progress plumbing (FR-16) and endpoint wiring (CR-4)
# ---------------------------------------------------------------------------


def test_the_progress_callback_reaches_the_tool_and_does_not_outlive_the_turn(monkeypatch):
    seen: dict = {}

    def _coverage(ticket_ids=None, control_ids=None, progress=None):
        seen["progress"] = progress
        return {"readiness": 0.5}

    monkeypatch.setattr(agent.core_tools, "coverage_report", _coverage)
    a, _, _ = _install(monkeypatch, _wants("coverage_report", {}), _says("Half ready."))

    def bar(done, total, label): ...

    a.ask("how ready are we?", progress=bar)

    assert seen["progress"] is bar
    assert agent._progress_callback is None  # not leaked into the next turn


def test_the_callback_is_cleared_even_when_the_turn_blows_up(monkeypatch):
    class _Explodes:
        def invoke(self, messages):
            raise RuntimeError("provider down")

    a, _, _ = _install(monkeypatch)
    a._llm = _Explodes()

    with pytest.raises(RuntimeError):
        a.ask("anything", progress=lambda *_: None)

    assert agent._progress_callback is None


def test_the_llm_is_wired_from_the_shared_resolvers_not_its_own_env_read(monkeypatch):
    # A self-hosted deployment (CR-4) where the chat page still calls
    # OpenRouter would leak exactly the data the deployment exists to keep in.
    _, fake, captured = _install(monkeypatch, _says("hi"))

    assert captured["base_url"] == "http://endpoint.invalid/v1"
    assert captured["api_key"] == "key-from-the-shared-resolver"
    assert captured["temperature"] == 0
    assert {t.name for t in fake.bound_tools} == {
        "find_evidence",
        "check_ticket",
        "coverage_report",
    }
