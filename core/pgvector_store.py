"""pgvector backend for core/retrieval — the same two verbs, on Postgres.

v1 named pgvector as the audit engine's store; Chroma stayed because the
corpus is ~90 chunks and the store was never the bottleneck. This module is
the scale path made real without touching the callers: `core/retrieval.py`
keeps its `ingest()` / `search()` signatures and dispatches here when
`AUDITREADY_VECTOR_STORE=pgvector`. Nothing above retrieval knows which
backend answered — that is the whole point of the seam.

Why pgvector and not a second vector database: Postgres is already the
system of record wherever this would be deployed (Jira, ServiceNow and the
customer's own data warehouse all speak it), one database is one backup, one
access-control model, one thing to self-host (CR-4). The `vector` extension
adds an HNSW index and a cosine-distance operator; the rest is SQL.

Design notes
- One table, namespaced by `store` (the persist-dir *name*, so the
  `AUDITREADY_PERSIST_DIR=.chroma-live` convention that keeps a live snapshot
  apart from the eval corpus keeps working unchanged).
- Rebuild is one transaction: DELETE the namespace, INSERT every chunk. The
  Chroma path drops and recreates the collection, which has a window where a
  concurrent search sees nothing; here a reader sees the old index until the
  commit. Small, but it is the kind of thing a database buys you.
- `where` is Chroma's filter dialect ({"k": v}, {"$and": [...]}, {"k": {"$in":
  [...]}}) translated to SQL over a jsonb column, keys and values always as
  bound parameters. Callers wrote their filters for Chroma; they keep them.
- Cosine distance `<=>` on unit vectors is exactly what Chroma's cosine
  space computes, so `similarity = 1 - distance` means the same number on
  both backends and the eval's thresholds transfer. scripts/smoke_pgvector.py
  checks the two rankings agree.
- No LangChain here, same as the rest of core/.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

DSN_ENV = "AUDITREADY_PG_DSN"
TABLE = "auditready_chunks"
DIMS = 384  # all-MiniLM-L6-v2; the column type pins it, so a model swap is a migration, on purpose

SCHEMA = (
    "CREATE EXTENSION IF NOT EXISTS vector",
    f"""CREATE TABLE IF NOT EXISTS {TABLE} (
        store     text  NOT NULL,
        chunk_id  text  NOT NULL,
        text      text  NOT NULL,
        metadata  jsonb NOT NULL,
        embedding vector({DIMS}) NOT NULL,
        PRIMARY KEY (store, chunk_id)
    )""",
    # HNSW over cosine distance: approximate nearest neighbour, sub-linear at
    # the sizes where Chroma's single sqlite file stops being comfortable.
    f"CREATE INDEX IF NOT EXISTS {TABLE}_embedding_hnsw ON {TABLE} USING hnsw (embedding vector_cosine_ops)",
    # The metadata filters (`ticket_id`, `doc_type`) hit this.
    f"CREATE INDEX IF NOT EXISTS {TABLE}_metadata_gin ON {TABLE} USING gin (metadata)",
)


def dsn() -> str:
    value = os.getenv(DSN_ENV, "").strip()
    if not value:
        raise RuntimeError(
            f"{DSN_ENV} is not set. AUDITREADY_VECTOR_STORE=pgvector needs a Postgres with the "
            "vector extension, e.g. postgresql://auditready:auditready@localhost:5432/auditready "
            "(`docker compose up -d db` starts one)."
        )
    return value


def describe(conninfo: str) -> str:
    """`host:port/dbname` for logs and /health — never the password."""
    d = conninfo_to_dict(conninfo)
    return f"{d.get('host', 'localhost')}:{d.get('port', 5432)}/{d.get('dbname', '')}"


# One pool per DSN. coverage_report searches from a thread pool; psycopg
# connections are not shareable across threads, pools are.
_pools: dict[str, ConnectionPool] = {}
_pool_lock = threading.Lock()


def _pool(conninfo: str) -> ConnectionPool:
    with _pool_lock:
        pool = _pools.get(conninfo)
        if pool is None:
            pool = ConnectionPool(
                conninfo, min_size=1, max_size=8, configure=register_vector, open=True
            )
            _pools[conninfo] = pool
    return pool


def ensure_schema(conn: psycopg.Connection) -> None:
    for stmt in SCHEMA:
        conn.execute(stmt)


# ---------------------------------------------------------------------------
# Chroma `where` -> SQL predicate (pure; pinned by tests/test_pgvector_store.py)
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    """Compare against `metadata->>key`, which yields jsonb scalars as text:
    strings as themselves, everything else in its JSON spelling."""
    return value if isinstance(value, str) else json.dumps(value)


def where_to_sql(where: dict | None) -> tuple[str, list[Any]]:
    """Translate a Chroma-style filter into `(predicate, params)`.

    Supported: {"k": v}, {"k": {"$eq"|"$ne"|"$in"|"$nin": ...}}, and
    {"$and"|"$or": [filters]} nested to any depth — the subset core/tools.py
    and core/rag.py actually use, plus the two obvious neighbours. Anything
    else raises rather than silently matching everything.
    """
    if not where:
        return "TRUE", []
    if len(where) != 1:
        raise ValueError("a where filter has exactly one top-level key (wrap several in $and)")
    ((key, value),) = where.items()

    if key in ("$and", "$or"):
        if not isinstance(value, list) or not value:
            raise ValueError(f"{key} takes a non-empty list of filters")
        parts, params = [], []
        for sub in value:
            sql, p = where_to_sql(sub)
            parts.append(f"({sql})")
            params.extend(p)
        return (" AND " if key == "$and" else " OR ").join(parts), params
    if key.startswith("$"):
        raise ValueError(f"unsupported top-level operator {key!r}")

    if not isinstance(value, dict):
        value = {"$eq": value}
    if len(value) != 1:
        raise ValueError(f"field {key!r}: exactly one operator expected")
    ((op, operand),) = value.items()
    col = "metadata->>%s"
    if op == "$eq":
        return f"{col} = %s", [key, _text(operand)]
    if op == "$ne":
        return f"{col} <> %s", [key, _text(operand)]
    if op == "$in":
        return f"{col} = ANY(%s)", [key, [_text(v) for v in operand]]
    if op == "$nin":
        return f"NOT ({col} = ANY(%s))", [key, [_text(v) for v in operand]]
    raise ValueError(f"unsupported operator {op!r} for field {key!r}")


# ---------------------------------------------------------------------------
# The two verbs
# ---------------------------------------------------------------------------


def ingest_chunks(
    rows: list[tuple[str, str, dict[str, Any], list[float]]],
    *,
    store: str,
    conninfo: str | None = None,
) -> int:
    """Replace the namespace `store` with `rows` = (chunk_id, text, metadata, embedding).
    One transaction: readers see the old index until the commit."""
    conninfo = conninfo or dsn()
    with _pool(conninfo).connection() as conn, conn.transaction():
        ensure_schema(conn)
        conn.execute(f"DELETE FROM {TABLE} WHERE store = %s", (store,))
        with conn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO {TABLE} (store, chunk_id, text, metadata, embedding) "
                "VALUES (%s, %s, %s, %s, %s)",
                [(store, cid, text, Jsonb(meta), Vector(emb)) for cid, text, meta, emb in rows],
            )
    return len(rows)


def search_chunks(
    query_embedding: list[float],
    *,
    k: int,
    where: dict | None,
    store: str,
    conninfo: str | None = None,
) -> list[tuple[str, str, dict[str, Any], float]]:
    """k nearest chunks in `store` matching `where`, as (chunk_id, text, metadata, similarity)."""
    conninfo = conninfo or dsn()
    predicate, params = where_to_sql(where)
    q = Vector(query_embedding)
    with _pool(conninfo).connection() as conn, conn.transaction():
        # pgvector >= 0.8: keep scanning the HNSW graph until k rows pass the
        # filter, instead of filtering the first ef_search candidates and
        # returning fewer than k. Older servers lack the GUC; the savepoint
        # confines that error so the query still runs (exactly, at our size).
        try:
            with conn.transaction():
                conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        except psycopg.Error:
            pass
        cur = conn.execute(
            f"SELECT chunk_id, text, metadata, 1 - (embedding <=> %s) AS similarity "
            f"FROM {TABLE} WHERE store = %s AND ({predicate}) "
            f"ORDER BY embedding <=> %s LIMIT %s",
            [q, store, *params, q, k],
        )
        return [(cid, text, meta, float(sim)) for cid, text, meta, sim in cur.fetchall()]


def count(store: str, conninfo: str | None = None) -> int:
    conninfo = conninfo or dsn()
    with _pool(conninfo).connection() as conn, conn.transaction():
        ensure_schema(conn)
        return conn.execute(f"SELECT count(*) FROM {TABLE} WHERE store = %s", (store,)).fetchone()[0]
