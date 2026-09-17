"""Unit tests for core/rag.py — the query-decomposition output contract.

decompose_control and multi_query_search need an LLM and a vector store
(covered by scripts/smoke_rag.py). The unit-testable seam is _parse_queries:
the schema gate that keeps a rambling model from turning one control into 15
searches (cost) or collapsing it into 1 (defeats multi-query RAG, FR-3).
"""

import pytest

from core.rag import _parse_queries


def test_valid_query_list_parses():
    text = '{"queries": ["client approval email", "signoff with named approver"]}'
    assert _parse_queries(text) == [
        "client approval email",
        "signoff with named approver",
    ]


def test_prose_around_the_json_is_tolerated():
    text = 'Sure! {"queries": ["a b c", "d e f"]} Hope that helps.'
    assert _parse_queries(text) == ["a b c", "d e f"]


def test_single_query_rejected():
    # min_length=2: one query means decomposition didn't decompose —
    # accepting it would silently degrade advanced RAG to naive RAG.
    with pytest.raises(ValueError):
        _parse_queries('{"queries": ["only one"]}')


def test_more_than_six_queries_rejected():
    # max_length=6: the cost ceiling. 7+ queries per control multiplied
    # across a 20-ticket sweep is a token bill, not better retrieval.
    seven = ", ".join(f'"query {i}"' for i in range(7))
    with pytest.raises(ValueError):
        _parse_queries(f'{{"queries": [{seven}]}}')


@pytest.mark.parametrize(
    "bad_reply",
    [
        "no json here",
        '{"queries": "not a list"}',
        '{"wrong_key": ["a", "b"]}',
    ],
)
def test_malformed_replies_raise_value_error(bad_reply):
    with pytest.raises(ValueError):
        _parse_queries(bad_reply)
