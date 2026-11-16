"""Unit tests for core/pgvector_store.py - the pure seams.

The database itself is exercised by scripts/smoke_pgvector.py (needs a
Postgres with the vector extension; `docker compose up -d db`). What can be
pinned offline is the contract the callers rely on:

1. Chroma's `where` dialect translates to SQL with every key and value as a
   bound parameter - a filter can never become a string spliced into SQL.
2. The store switch in core/retrieval.py rejects unknown backends loudly and
   keeps the namespace convention (`AUDITREADY_PERSIST_DIR` name) intact.
"""

import pytest

from core import retrieval
from core.pgvector_store import describe, where_to_sql


def test_no_filter_matches_everything():
    assert where_to_sql(None) == ("TRUE", [])
    assert where_to_sql({}) == ("TRUE", [])


def test_bare_equality_binds_key_and_value():
    sql, params = where_to_sql({"ticket_id": "TCK-1002"})
    assert sql == "metadata->>%s = %s"
    assert params == ["ticket_id", "TCK-1002"]


def test_and_of_two_filters_is_what_core_tools_sends():
    # core/tools.py: one bare dict for one condition, explicit $and for two.
    sql, params = where_to_sql({"$and": [{"ticket_id": "TCK-1002"}, {"doc_type": "email"}]})
    assert sql == "(metadata->>%s = %s) AND (metadata->>%s = %s)"
    assert params == ["ticket_id", "TCK-1002", "doc_type", "email"]


def test_in_and_nin_and_ne_and_or():
    sql, params = where_to_sql({"doc_type": {"$in": ["email", "change"]}})
    assert sql == "metadata->>%s = ANY(%s)" and params == ["doc_type", ["email", "change"]]
    sql, params = where_to_sql({"doc_type": {"$nin": ["ticket"]}})
    assert sql.startswith("NOT (") and params == ["doc_type", ["ticket"]]
    sql, _ = where_to_sql({"status": {"$ne": "open"}})
    assert sql == "metadata->>%s <> %s"
    sql, _ = where_to_sql({"$or": [{"a": "1"}, {"b": "2"}]})
    assert " OR " in sql


def test_non_string_scalars_compare_in_their_json_spelling():
    # `->>` yields text; jsonb true is the text "true", 3 is "3".
    assert where_to_sql({"flag": True})[1] == ["flag", "true"]
    assert where_to_sql({"n": 3})[1] == ["n", "3"]
    assert where_to_sql({"x": 1.5})[1] == ["x", "1.5"]


@pytest.mark.parametrize(
    "bad",
    [
        {"a": "1", "b": "2"},  # two top-level keys: Chroma rejects this too
        {"$and": []},
        {"$not": {"a": "1"}},
        {"a": {"$gt": 3}},  # not supported on purpose: no caller needs it
        {"a": {"$eq": 1, "$ne": 2}},
    ],
)
def test_unsupported_shapes_raise_instead_of_matching_everything(bad):
    with pytest.raises(ValueError):
        where_to_sql(bad)


def test_describe_never_leaks_the_password():
    label = describe("postgresql://auditready:s3cret@db.example:6543/evidence")
    assert label == "db.example:6543/evidence"
    assert "s3cret" not in label


def test_store_kind_defaults_to_chroma_and_rejects_unknown(monkeypatch):
    monkeypatch.delenv("AUDITREADY_VECTOR_STORE", raising=False)
    assert retrieval.store_kind() == "chroma"
    monkeypatch.setenv("AUDITREADY_VECTOR_STORE", "PgVector ")
    assert retrieval.store_kind() == "pgvector"
    monkeypatch.setenv("AUDITREADY_VECTOR_STORE", "pinecone")
    with pytest.raises(ValueError, match="pinecone"):
        retrieval.store_kind()


def test_namespace_is_the_persist_dir_name():
    assert retrieval.store_namespace(".chroma") == ".chroma"
    assert retrieval.store_namespace("/app/.chroma-live") == ".chroma-live"


def test_store_is_empty_for_chroma_looks_at_the_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("AUDITREADY_VECTOR_STORE", raising=False)
    assert retrieval.store_is_empty(tmp_path / "missing")
    assert retrieval.store_is_empty(tmp_path)  # exists, nothing in it
    (tmp_path / "chroma.sqlite3").write_text("")
    assert not retrieval.store_is_empty(tmp_path)


def test_pgvector_without_dsn_names_the_variable(monkeypatch):
    monkeypatch.setenv("AUDITREADY_VECTOR_STORE", "pgvector")
    monkeypatch.delenv("AUDITREADY_PG_DSN", raising=False)
    with pytest.raises(RuntimeError, match="AUDITREADY_PG_DSN"):
        retrieval.store_label()
