"""Unit tests for core/prompts.py and assistant/prompts.py.

Prompts are load-bearing spec (the project's recurring lesson: the words are
the code), but unit tests can't judge wording quality - that's what the eval
harness measures. What unit tests CAN pin down is the prompts' *contracts*:
the placeholders templates promise, the delimiters the guardrails scan for,
and the output formats the parsers expect. If a prompt edit breaks one of
those, the failure should happen here in milliseconds, not mid-conversation.
"""

from core.checklists import Control
from core.prompts import (
    DECOMPOSE_SYSTEM,
    JUDGE_SYSTEM,
    build_decompose_user_prompt,
    build_judge_user_prompt,
)
from assistant.prompts import AGENT_SYSTEM_PROMPT

CONTROL = Control(
    control_id="T-1",
    standard="TEST 0001",
    clause="1.1",
    title="Test control",
    description="A control used only in tests.",
    expected_evidence=["signoff", "client email"],
    sufficiency_rubric="satisfied if a named approver signed off",
    citation_required=True,
    severity="major",
)


# ---------------------------------------------------------------------------
# Judge prompt - contract with judge._parse_verdict and the guardrails
# ---------------------------------------------------------------------------


def test_judge_system_demands_the_json_verdict_schema():
    # The parser accepts exactly these four verdicts; the prompt must ask
    # for exactly these four - drift between the two is a retry storm.
    for verdict in ["satisfied", "partial", "gap", "not_applicable"]:
        assert verdict in JUDGE_SYSTEM
    assert "rationale" in JUDGE_SYSTEM
    assert "citations" in JUDGE_SYSTEM


def test_judge_user_prompt_fences_the_record():
    prompt = build_judge_user_prompt({"ticket_id": "TCK-9001"}, CONTROL)
    # The <<<RECORD fence is shared vocabulary with the injection defense:
    # guardrails' delimiter-breakout pattern quarantines corpus text that
    # tries to fake these exact markers, so the markers must stay stable.
    assert "<<<RECORD" in prompt
    assert "RECORD>>>" in prompt
    # And the record must sit INSIDE the fence.
    assert prompt.index("<<<RECORD") < prompt.index("TCK-9001") < prompt.index("RECORD>>>")


def test_judge_user_prompt_carries_the_full_control():
    prompt = build_judge_user_prompt({}, CONTROL)
    assert "T-1" in prompt
    assert "satisfied if a named approver signed off" in prompt  # the rubric
    assert "signoff, client email" in prompt  # expected evidence types


# ---------------------------------------------------------------------------
# Decomposition prompt - contract with rag._parse_queries
# ---------------------------------------------------------------------------


def test_decompose_system_asks_for_the_queries_json_shape():
    assert '{"queries"' in DECOMPOSE_SYSTEM
    # The 2-6 bounds are enforced by the _QueryList schema; the prompt must
    # state them too, or every decomposition risks burning a retry.
    assert "2 to 6" in DECOMPOSE_SYSTEM


def test_decompose_user_prompt_includes_rubric_and_evidence_types():
    prompt = build_decompose_user_prompt(CONTROL)
    assert "satisfied if a named approver signed off" in prompt
    assert "signoff, client email" in prompt


# ---------------------------------------------------------------------------
# Agent prompt - contract with assistant/agent.py
# ---------------------------------------------------------------------------


def test_agent_prompt_has_the_control_catalog_placeholder():
    # agent.py fills this via .format(); a typo'd placeholder would raise
    # KeyError at agent construction - catch it here instead.
    assert "{control_catalog}" in AGENT_SYSTEM_PROMPT
    formatted = AGENT_SYSTEM_PROMPT.format(control_catalog="- T-1 (major): Test")
    assert "- T-1 (major): Test" in formatted
    # No other placeholders may lurk in the template: .format() with one
    # keyword must succeed, which the line above already proves.


def test_agent_prompt_names_all_three_tools():
    # The working-style rules route question types to specific tools; if a
    # tool is renamed, the prompt must be updated in the same change.
    for tool_name in ["check_ticket", "find_evidence", "coverage_report"]:
        assert tool_name in AGENT_SYSTEM_PROMPT
