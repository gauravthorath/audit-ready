"""Unit tests for core/llm.py's pure seam: pricing-payload parsing.

chat() needs a live provider (scripts/smoke_llm.py); _pricing_from_payload
is the unit-testable part - it parses an UNTRUSTED external API response,
so the tests are mostly about malformed shapes returning None instead of
raising. Pricing is a display nicety: it must never be the reason the app
crashes.
"""

from core.llm import _pricing_from_payload

# The shape OpenRouter's GET /api/v1/models actually returns: prices are
# USD-per-single-token STRINGS, which the parser scales to per-million.
PAYLOAD = {
    "data": [
        {"id": "openai/gpt-4o-mini", "pricing": {"prompt": "0.00000015", "completion": "0.0000006"}},
        {"id": "anthropic/claude-sonnet-4.5", "pricing": {"prompt": "0.000003", "completion": "0.000015"}},
    ]
}


def test_known_model_scales_to_per_million():
    assert _pricing_from_payload(PAYLOAD, "openai/gpt-4o-mini") == (0.15, 0.6)


def test_other_models_resolve_independently():
    assert _pricing_from_payload(PAYLOAD, "anthropic/claude-sonnet-4.5") == (3.0, 15.0)


def test_unknown_model_returns_none():
    assert _pricing_from_payload(PAYLOAD, "no-such/model") is None


def test_malformed_payloads_return_none_never_raise():
    # External API responses are untrusted input - every broken shape must
    # degrade to None (caller falls back to its price sheet), not crash.
    cases = [
        {},  # no data key
        {"data": "not a list"},  # wrong type - .get on a str would raise if unguarded
        {"data": [{"id": "openai/gpt-4o-mini"}]},  # pricing key missing
        {"data": [{"id": "openai/gpt-4o-mini", "pricing": "free!"}]},  # pricing not a dict
        {"data": [{"id": "openai/gpt-4o-mini", "pricing": {"prompt": "abc", "completion": "1"}}]},
        {"data": [{"id": "openai/gpt-4o-mini", "pricing": {"prompt": None, "completion": None}}]},
        {"data": [{"id": "openai/gpt-4o-mini", "pricing": {"prompt": "0.001"}}]},  # completion missing
    ]
    for payload in cases:
        assert _pricing_from_payload(payload, "openai/gpt-4o-mini") is None, payload
