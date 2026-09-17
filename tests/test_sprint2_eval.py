"""Offline tests for the eval harness's grader seam (FR-18, CR-7).

What is pinned: the Part B grader model reaches `chat()` unchanged, a grader
is never chosen implicitly (that is how self-grading bias returns unnoticed),
the CLI flag beats the configured default, the run stamps who graded it, and
re-running one part carries the other part's evidence forward instead of
overwriting the file with half a run. No API key, no network — `chat` is
faked at the seam the harness imports it by.
"""

from __future__ import annotations

import json
import sys

import pytest

from core.llm import DEFAULT_JUDGE_MODEL, DEFAULT_MODEL, ChatResult
from assistant.eval import run_eval as ev


@pytest.fixture
def fake_chat(monkeypatch):
    """Record every chat() call the harness makes; reply with valid judge JSON."""
    calls: list[dict] = []

    def _chat(messages, model=None, **kwargs):
        calls.append({"messages": messages, "model": model, **kwargs})
        return ChatResult(content='{"claims": 4, "unsupported": 1, "score": 0.5}',
                          model=model or "unset")

    monkeypatch.setattr(ev, "chat", _chat)
    return calls


# --- the grader model reaches the provider -------------------------------

def test_judge_call_uses_the_grader_model_not_the_system_default(fake_chat):
    ev._judge_json("system", "user", "google/gemini-2.5-pro")
    assert fake_chat[0]["model"] == "google/gemini-2.5-pro"


def test_any_grader_model_is_honoured(fake_chat):
    ev._judge_json("system", "user", "openai/gpt-4o-mini")
    assert fake_chat[0]["model"] == "openai/gpt-4o-mini"


def test_grader_model_is_required_never_implicit(fake_chat):
    # A defaulted model here would let the system quietly grade itself.
    with pytest.raises(TypeError):
        ev._judge_json("system", "user")


def test_judge_json_slices_json_out_of_chatty_replies(monkeypatch):
    monkeypatch.setattr(ev, "chat", lambda *a, **k: ChatResult(
        content='Sure!\n{"score": 0.9}\nHope that helps.', model="m"))
    assert ev._judge_json("s", "u", "m") == {"score": 0.9}


# --- the flag wins, and the run records who graded ------------------------

def test_cli_flag_overrides_the_configured_default(monkeypatch, tmp_path):
    seen: list[str] = []
    monkeypatch.setattr(ev, "run_rag_eval", lambda n, model: seen.append(model) or {})
    monkeypatch.setattr(sys, "argv", ["run_eval", "--skip-compliance",
                                      "--judge-model", "deepseek/deepseek-chat-v3.1",
                                      "--out", str(tmp_path / "r.json")])
    assert ev.main() == 0
    assert seen == ["deepseek/deepseek-chat-v3.1"]


def test_without_the_flag_the_configured_default_is_used(monkeypatch, tmp_path):
    seen: list[str] = []
    monkeypatch.setattr(ev, "run_rag_eval", lambda n, model: seen.append(model) or {})
    monkeypatch.setattr(sys, "argv", ["run_eval", "--skip-compliance",
                                      "--out", str(tmp_path / "r.json")])
    assert ev.main() == 0
    assert seen == [DEFAULT_JUDGE_MODEL]


@pytest.fixture
def fake_agent(monkeypatch):
    """Stand in for the LangChain agent so Part B runs offline."""
    import assistant.agent

    class _Reply:
        content = "ISO 9001 clause 7.5 is satisfied [TCK-1001]."
        tool_calls = [{"name": "check_ticket",
                       "result": {"ticket_id": "TCK-1001"}}]

    class _Agent:
        def ask(self, question):
            return _Reply()

    monkeypatch.setattr(assistant.agent, "AuditAgent", _Agent)


def test_metrics_stamp_who_graded_and_who_answered(fake_agent, fake_chat):
    # A faithfulness number is unreadable without knowing who produced it.
    metrics = ev.run_rag_eval(1, "google/gemini-2.5-pro")["metrics"]
    assert metrics["judge_model"] == "google/gemini-2.5-pro"
    assert metrics["system_model"] == DEFAULT_MODEL


def test_a_dead_grader_is_recorded_not_swallowed(fake_agent, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("404 no such model")

    monkeypatch.setattr(ev, "chat", _boom)
    result = ev.run_rag_eval(1, "typo/not-a-model")
    assert result["metrics"]["faithfulness"] is None
    errors = result["rows"][0]["judge_errors"]
    assert len(errors) == 2 and all("404 no such model" in e for e in errors)


# --- re-running one part keeps the other part's evidence ------------------

def test_skipped_part_is_carried_forward_with_its_provenance():
    previous = {"ran_at": "2026-07-29T00:00:00+00:00",
                "compliance": {"metrics": {"gap_recall": 1.0}}}
    merged = ev.carry_forward({"ran_at": "2026-09-02T00:00:00+00:00",
                               "rag": {"metrics": {}}}, previous)
    assert merged["compliance"]["metrics"] == {"gap_recall": 1.0}
    assert merged["compliance"]["carried_from_run"] == "2026-07-29T00:00:00+00:00"


def test_a_freshly_run_part_is_never_replaced_by_the_old_one():
    previous = {"ran_at": "old", "rag": {"metrics": {"faithfulness": 0.98}}}
    merged = ev.carry_forward({"rag": {"metrics": {"faithfulness": 0.5}}}, previous)
    assert merged["rag"]["metrics"]["faithfulness"] == 0.5
    assert "carried_from_run" not in merged["rag"]


def test_no_previous_file_carries_nothing():
    assert ev.carry_forward({"rag": {}}, {}) == {"rag": {}}


def test_main_writes_the_carried_file(monkeypatch, tmp_path):
    out = tmp_path / "results.json"
    out.write_text(json.dumps({"ran_at": "old", "compliance": {"metrics": {"x": 1}}}))
    monkeypatch.setattr(ev, "run_rag_eval", lambda n, model: {"metrics": {"judge_model": model}})
    monkeypatch.setattr(sys, "argv", ["run_eval", "--skip-compliance", "--out", str(out)])
    assert ev.main() == 0
    written = json.loads(out.read_text())
    assert written["compliance"]["carried_from_run"] == "old"
    assert written["rag"]["metrics"]["judge_model"] == DEFAULT_JUDGE_MODEL
