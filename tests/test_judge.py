"""Unit tests for core/judge.py — the "parse, don't validate" seam.

judge_ticket itself needs an LLM (covered by scripts/smoke_judge.py); what
unit tests CAN pin down is the contract around it: _parse_verdict must turn
messy model text into a validated Verdict or raise ValueError for the retry
loop — never return something malformed.

Testing a private function (leading underscore) is a deliberate choice here:
_parse_verdict is the pure, deterministic core of the module, and testing it
directly beats testing it through an LLM call that costs money and can
return different text every run.
"""

import pytest

from core.judge import Verdict, _parse_verdict

VALID_REPLY = (
    '{"verdict": "gap", "rationale": "No signoff recorded.", '
    '"citations": ["signoff field"]}'
)


def test_clean_json_parses_to_validated_verdict():
    verdict = _parse_verdict(VALID_REPLY)
    assert isinstance(verdict, Verdict)
    assert verdict.verdict == "gap"
    assert verdict.citations == ["signoff field"]


def test_json_wrapped_in_markdown_fences_still_parses():
    # The most common real-world formatting slip: ```json fences.
    assert _parse_verdict(f"```json\n{VALID_REPLY}\n```").verdict == "gap"


def test_json_with_polite_prose_around_it_still_parses():
    # The second most common slip: a sentence before/after the object.
    text = f"Here is my assessment:\n{VALID_REPLY}\nLet me know if you need more."
    assert _parse_verdict(text).verdict == "gap"


def test_citations_default_to_empty_list_when_omitted():
    verdict = _parse_verdict('{"verdict": "not_applicable", "rationale": "No customer."}')
    assert verdict.citations == []


@pytest.mark.parametrize(
    "bad_reply",
    [
        "I think the ticket looks fine overall.",  # no JSON at all
        '{"verdict": "gap", "rationale": "x",',  # truncated JSON
        # Schema violations — the LLM inventing its own vocabulary:
        '{"verdict": "mostly-satisfied", "rationale": "close enough"}',
        '{"verdict": "gap"}',  # rationale missing entirely
        '{"verdict": "gap", "rationale": ""}',  # rationale empty (min_length=1)
    ],
)
def test_malformed_replies_raise_value_error_for_retry(bad_reply):
    # ValueError is the contract with judge_ticket's corrective-retry loop —
    # any other exception type would crash the agent instead of retrying.
    with pytest.raises(ValueError):
        _parse_verdict(bad_reply)


def test_all_four_verdict_values_accepted():
    for value in ["satisfied", "partial", "gap", "not_applicable"]:
        reply = f'{{"verdict": "{value}", "rationale": "r"}}'
        assert _parse_verdict(reply).verdict == value
