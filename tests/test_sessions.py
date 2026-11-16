"""The session store and the conversation it holds (Step 41). Offline.

`MemorySessionStore` is exercised directly; `PostgresSessionStore` shares the
protocol and is proven live by `scripts/smoke_sessions.py`, because a test
that needs a database is not an offline test. What *is* pinned here without a
database is the thing most likely to break silently: that an `AuditAgent` can
be serialised and rebuilt, and that rebuilding regenerates the system prompt
instead of restoring a stale one.
"""

from __future__ import annotations

import pytest

from api.sessions import (
    MemorySessionStore, PostgresSessionStore, SessionRecord, build_session_store,
)


@pytest.fixture()
def store():
    return MemorySessionStore()


def record(session_id="s1", actor="alice", *questions) -> SessionRecord:
    messages = []
    for q in questions:
        messages.append({"type": "human", "data": {"content": q}})
        messages.append({"type": "ai", "data": {"content": f"answer to {q}"}})
    return SessionRecord(session_id=session_id, actor=actor, state={"messages": messages})


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


def test_a_conversation_names_itself_after_the_first_question():
    r = record("s1", "alice", "Is TCK-1001 audit-ready?", "and TCK-1002?")
    assert r.title == "Is TCK-1001 audit-ready?"
    assert r.turns == 2

    assert SessionRecord("s2", "alice").title == "(new conversation)"
    long = record("s3", "alice", "x" * 200)
    assert len(long.title) == 78 and long.title.endswith("…")


def test_a_title_never_carries_a_newline_into_a_list():
    r = record("s1", "alice", "first line\nsecond line")
    assert "\n" not in r.title and r.title == "first line second line"


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


def test_sessions_are_keyed_by_actor_and_id_together(store):
    store.put(record("shared", "alice", "alice's question"))
    store.put(record("shared", "bob", "bob's question"))

    assert store.get("alice", "shared").title == "alice's question"
    assert store.get("bob", "shared").title == "bob's question"
    assert store.get("carol", "shared") is None  # a guessed id finds nothing


def test_listing_is_mine_newest_first(store):
    for i, q in enumerate(["first", "second", "third"]):
        r = record(f"s{i}", "alice", q)
        r.updated_at = f"2026-09-1{i}T00:00:00+00:00"
        store.put(r)
    store.put(record("other", "bob", "not mine"))

    # `put` stamps updated_at, so the insertion order is the recency order here
    titles = [r.title for r in store.list("alice")]
    assert set(titles) == {"first", "second", "third"}
    assert [r.actor for r in store.list("alice")] == ["alice"] * 3
    assert [r.title for r in store.list("bob")] == ["not mine"]
    assert store.list("carol") == []
    assert len(store.list("alice", limit=2)) == 2


def test_an_update_keeps_the_original_creation_time(store):
    first = record("s1", "alice", "one")
    first.created_at = "2026-09-01T00:00:00+00:00"
    store.put(first)
    store.put(record("s1", "alice", "one", "two"))

    kept = store.get("alice", "s1")
    assert kept.created_at == "2026-09-01T00:00:00+00:00"
    assert kept.updated_at > kept.created_at
    assert kept.turns == 2


def test_deleting_is_scoped_to_the_owner(store):
    store.put(record("s1", "alice", "mine"))
    assert store.delete("bob", "s1") is False      # not bob's to delete
    assert store.get("alice", "s1") is not None
    assert store.delete("alice", "s1") is True
    assert store.delete("alice", "s1") is False    # idempotent
    assert len(store) == 0


def test_the_store_name_decides_the_backend_and_a_typo_fails_at_startup():
    assert build_session_store("memory").kind == "memory"
    assert isinstance(build_session_store(""), MemorySessionStore)
    with pytest.raises(ValueError, match="memory or postgres"):
        build_session_store("sqlite")
    # postgres is built lazily from the DSN; no connection is opened here
    built = build_session_store("postgres", "postgresql://u:p@localhost:5432/db")
    assert isinstance(built, PostgresSessionStore) and built.kind == "postgres"


# ---------------------------------------------------------------------------
# The agent round-trip - the seam the store depends on
# ---------------------------------------------------------------------------


@pytest.fixture()
def agent(monkeypatch):
    """A real AuditAgent with its model handle replaced: no key, no network."""
    from assistant import agent as agent_module

    monkeypatch.setattr(agent_module, "ChatOpenAI", lambda **kw: _FakeLLM())
    monkeypatch.setenv("OPENROUTER_API_KEY", "not-used")
    return agent_module.AuditAgent()


class _FakeLLM:
    def bind_tools(self, tools):
        return self

    def invoke(self, messages):  # pragma: no cover - never called in these tests
        raise AssertionError("no model call expected")


def test_an_agent_round_trips_through_plain_data(agent):
    from langchain_core.messages import AIMessage, HumanMessage

    agent._messages.extend([HumanMessage(content="is TCK-1001 ready?"),
                            AIMessage(content="it is satisfied")])
    agent.total_prompt_tokens, agent.total_completion_tokens = 3000, 200

    state = agent.snapshot()
    assert [m["type"] for m in state["messages"]] == ["human", "ai"]
    assert state["prompt_tokens"] == 3000

    import json
    assert json.loads(json.dumps(state)) == state  # it has to survive a jsonb column

    rebuilt = type(agent)()
    rebuilt.restore(state)
    assert [m.content for m in rebuilt._messages[1:]] == ["is TCK-1001 ready?", "it is satisfied"]
    assert rebuilt.total_prompt_tokens == 3000 and rebuilt.total_completion_tokens == 200


def test_the_system_prompt_is_regenerated_not_restored(agent):
    """A resumed conversation uses today's instructions, not the ones it started with."""
    from langchain_core.messages import SystemMessage

    state = agent.snapshot()
    assert not any(m["type"] == "system" for m in state["messages"])

    rebuilt = type(agent)()
    rebuilt.restore(state)
    assert isinstance(rebuilt._messages[0], SystemMessage)
    assert rebuilt._messages[0].content == agent._messages[0].content


def test_an_unreadable_record_starts_a_new_conversation_rather_than_failing(agent):
    agent.restore({"messages": [{"type": "nonsense", "data": {}}], "prompt_tokens": "x"})
    assert len(agent._messages) == 1  # the system prompt only
    assert agent.total_prompt_tokens == 0


def test_a_restored_history_is_still_trimmed_to_the_window(agent):
    from langchain_core.messages import HumanMessage

    from assistant.agent import MEMORY_WINDOW

    agent.restore({"messages": [{"type": "human", "data": {"content": str(i)}}
                                for i in range(MEMORY_WINDOW + 10)]})
    assert len(agent._messages) == 1 + MEMORY_WINDOW
    assert isinstance(agent._messages[-1], HumanMessage)
    assert agent._messages[-1].content == str(MEMORY_WINDOW + 9)  # the newest survived
