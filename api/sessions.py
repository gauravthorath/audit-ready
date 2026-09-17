"""Chat sessions: whose conversation it is, and where it lives between turns (FR-8, NFR-2).

`AuditAgent` holds a conversation — the sliding-window memory is its message
list. The API has to find the same conversation on the next turn, and *where*
that lives is the question this module isolates.

What changed at Step 41, and why
--------------------------------
The first version kept live `AuditAgent` objects in a process dict. That is the
smallest thing that preserves the memory, and it has three properties that stop
being acceptable the moment a second person can sign in:

* **It belonged to nobody.** Anyone holding a session id could continue anyone
  else's conversation. Sessions are now keyed by `(actor, session_id)`, so two
  people cannot reach each other's, and "your conversations" is a query rather
  than a hope.
* **It died with the process.** A deploy, a crash or an idle restart silently
  emptied every conversation. A session is now a *record* — the serialised
  message list plus the token counters — so the agent is rebuilt from it per
  turn and the API keeps no conversation in memory between requests.
* **It was one worker's private truth.** Two uvicorn workers meant a coin flip
  over whether your last turn existed. A serialised record in Postgres is the
  same record for every worker.

Rebuilding the agent per turn costs a dictionary round-trip on a few messages,
which is nothing next to a model call, and it buys statelessness — the property
that makes the API safe to run more than one of.

**The system prompt is deliberately not stored.** It is regenerated from
`assistant/prompts.py` and the live control catalogue when the agent is rebuilt,
so a conversation resumed after a pack changed is judged against the packs that
exist now, not against a snapshot of the ones that existed on Tuesday. Only the
turns are stored; the instructions are always current.

Two implementations, one protocol: `MemorySessionStore` (the default, a dict,
right for one process and a laptop) and `PostgresSessionStore` (the `db`
service compose already runs, selected with `AUDITREADY_SESSION_STORE=postgres`).
Chroma-versus-pgvector is the same shape of choice, made the same way.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

TABLE = "auditready_sessions"

SCHEMA = (
    f"""CREATE TABLE IF NOT EXISTS {TABLE} (
        actor      text        NOT NULL,
        session_id text        NOT NULL,
        state      jsonb       NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (actor, session_id)
    )""",
    # "my conversations, newest first" is the only listing the UI asks for.
    f"CREATE INDEX IF NOT EXISTS {TABLE}_actor_updated ON {TABLE} (actor, updated_at DESC)",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SessionRecord:
    """One conversation, owned by one actor. `state` is whatever the agent serialised."""

    session_id: str
    actor: str
    state: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @property
    def turns(self) -> int:
        """Human turns so far — what a conversation list shows beside the title."""
        return sum(1 for m in self.state.get("messages", []) if m.get("type") == "human")

    @property
    def title(self) -> str:
        """The first thing the person asked, trimmed. A conversation names itself."""
        for message in self.state.get("messages", []):
            if message.get("type") == "human":
                text = str(message.get("data", {}).get("content", "")).strip().replace("\n", " ")
                return (text[:77] + "…") if len(text) > 78 else text or "(empty)"
        return "(new conversation)"


class SessionStore(Protocol):
    """What the chat route needs from wherever conversations are kept."""

    kind: str

    def get(self, actor: str, session_id: str) -> SessionRecord | None: ...
    def put(self, record: SessionRecord) -> None: ...
    def delete(self, actor: str, session_id: str) -> bool: ...
    def list(self, actor: str, limit: int = 50) -> list[SessionRecord]: ...


# ---------------------------------------------------------------------------
# In process
# ---------------------------------------------------------------------------


class MemorySessionStore:
    """Conversations in a process-local dict. Lock-guarded: sync routes run on a thread pool.

    Right for one process. It does not survive a restart and it is not shared
    between workers — both are written down in `GET /health` via the store's
    `kind`, so nobody has to guess which posture a deployment is in.
    """

    kind = "memory"

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], SessionRecord] = {}
        self._lock = threading.Lock()

    def get(self, actor: str, session_id: str) -> SessionRecord | None:
        with self._lock:
            return self._records.get((actor, session_id))

    def put(self, record: SessionRecord) -> None:
        record.updated_at = _now()
        with self._lock:
            existing = self._records.get((record.actor, record.session_id))
            if existing is not None:
                record.created_at = existing.created_at
            self._records[(record.actor, record.session_id)] = record

    def delete(self, actor: str, session_id: str) -> bool:
        with self._lock:
            return self._records.pop((actor, session_id), None) is not None

    def list(self, actor: str, limit: int = 50) -> list[SessionRecord]:
        with self._lock:
            mine = [r for (a, _), r in self._records.items() if a == actor]
        return sorted(mine, key=lambda r: r.updated_at, reverse=True)[:limit]

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------


class PostgresSessionStore:
    """The same four methods against the `db` service. One table, one jsonb column.

    The DSN is the one the vector store already uses, so a deployment that
    chose `AUDITREADY_VECTOR_STORE=pgvector` adds no second database to run.
    The schema is created on first use, the way `core/pgvector_store.py` does
    it: a two-statement migration is not worth a migration tool, and it keeps
    "clone, compose up, works" true.
    """

    kind = "postgres"

    def __init__(self, conninfo: str | None = None) -> None:
        from core.pgvector_store import dsn  # local: psycopg only loads for this backend

        self.conninfo = conninfo or dsn()
        self._ready = False
        self._lock = threading.Lock()

    # -- plumbing ---------------------------------------------------------------

    def _connection(self):
        from core.pgvector_store import _pool

        pool = _pool(self.conninfo)
        with self._lock:
            if not self._ready:
                with pool.connection() as conn, conn.transaction():
                    for statement in SCHEMA:
                        conn.execute(statement)
                self._ready = True
        return pool.connection()

    @staticmethod
    def _row(row: tuple) -> SessionRecord:
        actor, session_id, state, created_at, updated_at = row
        return SessionRecord(
            session_id=session_id,
            actor=actor,
            state=state if isinstance(state, dict) else json.loads(state),
            created_at=created_at.isoformat(timespec="seconds"),
            updated_at=updated_at.isoformat(timespec="seconds"),
        )

    # -- the four methods -------------------------------------------------------

    def get(self, actor: str, session_id: str) -> SessionRecord | None:
        with self._connection() as conn:
            row = conn.execute(
                f"SELECT actor, session_id, state, created_at, updated_at FROM {TABLE} "
                "WHERE actor = %s AND session_id = %s",
                (actor, session_id),
            ).fetchone()
        return self._row(row) if row else None

    def put(self, record: SessionRecord) -> None:
        from psycopg.types.json import Jsonb

        with self._connection() as conn, conn.transaction():
            conn.execute(
                f"INSERT INTO {TABLE} (actor, session_id, state) VALUES (%s, %s, %s) "
                "ON CONFLICT (actor, session_id) DO UPDATE "
                "SET state = EXCLUDED.state, updated_at = now()",
                (record.actor, record.session_id, Jsonb(record.state)),
            )

    def delete(self, actor: str, session_id: str) -> bool:
        with self._connection() as conn, conn.transaction():
            cursor = conn.execute(
                f"DELETE FROM {TABLE} WHERE actor = %s AND session_id = %s", (actor, session_id)
            )
            return cursor.rowcount > 0

    def list(self, actor: str, limit: int = 50) -> list[SessionRecord]:
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT actor, session_id, state, created_at, updated_at FROM {TABLE} "
                "WHERE actor = %s ORDER BY updated_at DESC LIMIT %s",
                (actor, int(limit)),
            ).fetchall()
        return [self._row(row) for row in rows]


def build_session_store(kind: str, dsn: str | None = None) -> SessionStore:
    """`memory` (default) or `postgres`. An unknown name fails at startup, not on a turn."""
    kind = (kind or "memory").strip().lower()
    if kind == "memory":
        return MemorySessionStore()
    if kind == "postgres":
        return PostgresSessionStore(dsn or None)
    raise ValueError(f"AUDITREADY_SESSION_STORE must be memory or postgres; got {kind!r}")
