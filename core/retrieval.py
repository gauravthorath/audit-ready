"""Ingestion + vector store + similarity search (FR-1, FR-2).

Turns the corpus (tickets, emails, change records) into searchable chunks in
a persistent Chroma vector store, embedded with a LOCAL sentence-transformers
model. This is the "long-term memory" every later step queries: Step 6 builds
multi-query RAG on top of `search()`, and Step 7's tools call both.

The mental model (see LEARNING.md Step 5 for the full walkthrough):
- An embedding model maps text -> a fixed-length vector (here 384 floats)
  such that texts with similar MEANING land near each other. "Client agreed
  to close the ticket" and "customer approved closure" share almost no words
  but sit close in vector space — that's the whole trick, and why this beats
  keyword search for evidence-hunting.
- A vector store persists (id, vector, text, metadata) rows and answers
  "nearest vectors to this query vector" fast.
- Chunking decides WHAT gets a vector. We chunk by NATURAL UNITS (a comment,
  an email, a change record), not fixed character windows: our records are
  small and structured, and a hit like "comment #2 of TCK-1002" is exactly
  the citation granularity the auditor needs (FR-16).

Why local embeddings: OpenRouter exposes no embeddings endpoint, and local
embedding means corpus text never leaves the machine except inside judging
prompts we control — a property the self-hosted story (CR-4)
keeps. all-MiniLM-L6-v2 is the classic small/fast/good-enough choice: 384
dims, ~90MB, ~fractions of a second for our whole corpus on CPU.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import chromadb

# Gitignored: derived data, rebuild anytime. Overridable so a live-evidence
# snapshot (CR-3) can be indexed into its own store without dropping the
# synthetic collection the frozen eval depends on.
DEFAULT_PERSIST_DIR = Path(os.getenv("AUDITREADY_PERSIST_DIR", ".chroma"))
COLLECTION_NAME = "auditready_corpus"

# Which store answers. "chroma" (default): the embedded, file-backed index —
# zero setup, the eval was frozen on it. "pgvector": Postgres + the vector
# extension (core/pgvector_store.py) — the scale path, one database to
# self-host. Read at call time so a test or a CLI can flip it per process.
STORE_KINDS = ("chroma", "pgvector")


def store_kind() -> str:
    kind = os.getenv("AUDITREADY_VECTOR_STORE", "chroma").strip().lower()
    if kind not in STORE_KINDS:
        raise ValueError(
            f"AUDITREADY_VECTOR_STORE={kind!r}: expected one of {', '.join(STORE_KINDS)}"
        )
    return kind


def store_namespace(persist_dir: str | Path) -> str:
    """The pgvector namespace for a persist dir: its NAME (".chroma",
    ".chroma-live"), so the one knob that separates a live snapshot from the
    eval corpus keeps separating them on both backends."""
    return Path(persist_dir).name


def store_label(persist_dir: str | Path = DEFAULT_PERSIST_DIR) -> str:
    """For /health and logs: which backend, and where — never a password."""
    if store_kind() == "pgvector":
        from core import pgvector_store

        return f"pgvector:{pgvector_store.describe(pgvector_store.dsn())}/{store_namespace(persist_dir)}"
    return f"chroma:{persist_dir}"


def store_is_empty(persist_dir: str | Path = DEFAULT_PERSIST_DIR) -> bool:
    """Should the operator (or the container entrypoint) ingest first?"""
    if store_kind() == "pgvector":
        from core import pgvector_store

        return pgvector_store.count(store_namespace(persist_dir)) == 0
    path = Path(persist_dir)
    return not path.exists() or not any(path.iterdir())
EMBEDDING_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


# ---------------------------------------------------------------------------
# Embedding — lazy model singleton, same pattern as core/llm.py's client
# ---------------------------------------------------------------------------

_model = None

# coverage_report judges (ticket, control) pairs on a thread pool, and each
# judgment embeds its sub-queries through here. SentenceTransformer.encode
# isn't documented as thread-safe, so one lock serializes embedding: it's a
# fast local op (milliseconds) between slow network calls (seconds), so the
# serialization costs nothing measurable and removes the race entirely.
_model_lock = threading.Lock()


def _embed(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts into 384-dim vectors.

    The import and model load live INSIDE the function: sentence-transformers
    drags in PyTorch (~seconds of import time), and the first call downloads
    the model weights (~90MB, cached in ~/.cache thereafter). Modules that
    import core.retrieval without searching should pay none of that.

    normalize_embeddings=True scales every vector to length 1, which makes
    cosine similarity equivalent to a plain dot product — cheaper, and the
    convention Chroma's cosine space expects.
    """
    global _model
    with _model_lock:
        if _model is None:
            from sentence_transformers import SentenceTransformer

            _model = SentenceTransformer(EMBEDDING_MODEL_NAME)
        return _model.encode(texts, normalize_embeddings=True).tolist()


# ---------------------------------------------------------------------------
# Chunking — corpus JSON -> (id, text, metadata) rows
# ---------------------------------------------------------------------------


@dataclass
class Chunk:
    """One unit of searchable text with its provenance.

    chunk_id is DETERMINISTIC (e.g. "TCK-1002#c1", "EML-2001") so re-running
    ingestion produces the same ids — which is what makes citations stable
    across rebuilds and lets upserts replace rather than duplicate.
    """

    chunk_id: str
    text: str
    metadata: dict[str, Any]


def _ticket_chunks(t: dict) -> list[Chunk]:
    """A ticket becomes 1 header chunk + 1 chunk per comment.

    Why split comments out: a ticket with 3 comments discusses 3 different
    moments (scope confirmation, root cause, client approval). Averaged into
    one vector those meanings blur together; as separate chunks, the query
    "client approved closure" lands precisely on the approval comment.
    The header keeps ticket-level facts (status, dates, resolution, signoff)
    that no single comment carries.
    """
    header_lines = [
        f"Ticket {t['ticket_id']}: {t['title']}",
        f"type: {t['type']}  priority: {t['priority']}  status: {t['status']}",
        f"created: {t['created']}  closed: {t.get('closed') or 'not closed'}",
        f"reporter: {t['reporter']}  assignee: {t['assignee']}",
        f"description: {t['description']}",
        f"resolution: {t.get('resolution') or 'none recorded'}",
    ]
    signoff = t.get("signoff")
    header_lines.append(
        f"signoff: approved by {signoff['approver']} ({signoff['role']}) at {signoff['timestamp']}"
        if signoff
        else "signoff: none recorded"
    )
    if t.get("linked_changes"):
        header_lines.append("linked changes: " + ", ".join(t["linked_changes"]))
    if t.get("linked_emails"):
        header_lines.append("linked emails: " + ", ".join(t["linked_emails"]))

    chunks = [
        Chunk(
            chunk_id=f"{t['ticket_id']}#header",
            text="\n".join(header_lines),
            # Chroma metadata values must be scalars (str/int/float/bool) —
            # no lists. Anything list-shaped gets comma-joined; filters can
            # still match with $contains-style logic or exact doc ids.
            metadata={
                "doc_type": "ticket",
                "doc_id": t["ticket_id"],
                "ticket_id": t["ticket_id"],
                "status": t["status"],
            },
        )
    ]
    for i, c in enumerate(t.get("comments", [])):
        chunks.append(
            Chunk(
                chunk_id=f"{t['ticket_id']}#c{i}",
                text=(
                    f"Comment on {t['ticket_id']} by {c['author']} "
                    f"at {c['timestamp']}:\n{c['body']}"
                ),
                metadata={
                    "doc_type": "ticket_comment",
                    "doc_id": t["ticket_id"],
                    "ticket_id": t["ticket_id"],
                    "author": c["author"],
                    "timestamp": c["timestamp"],
                },
            )
        )
    return chunks


def _email_chunk(e: dict) -> Chunk:
    """An email is one natural unit; related_tickets joins it to tickets."""
    text = (
        f"Email {e['email_id']} from {e['from']} to {', '.join(e['to'])} "
        f"on {e['date']}\nsubject: {e['subject']}\n{e['body']}"
    )
    return Chunk(
        chunk_id=e["email_id"],
        text=text,
        metadata={
            "doc_type": "email",
            "doc_id": e["email_id"],
            "ticket_id": ",".join(e.get("related_tickets", [])) or "none",
            "date": e["date"],
        },
    )


def _change_chunk(c: dict) -> Chunk:
    approved = (
        f"approved by {c['approver']} at {c['approved']}"
        if c.get("approver")
        else "no approval recorded"
    )
    text = (
        f"Change {c['change_id']}: {c['title']}\nstatus: {c['status']}  {approved}\n"
        f"implemented: {c.get('implemented') or 'not implemented'}\n{c['summary']}"
    )
    return Chunk(
        chunk_id=c["change_id"],
        text=text,
        metadata={
            "doc_type": "change",
            "doc_id": c["change_id"],
            "ticket_id": ",".join(c.get("related_tickets", [])) or "none",
            "status": c["status"],
        },
    )


def build_chunks(corpus_dir: str | Path) -> list[Chunk]:
    """Read the three corpus files and produce all chunks."""
    corpus_dir = Path(corpus_dir)
    chunks: list[Chunk] = []
    for t in json.loads((corpus_dir / "tickets.json").read_text()):
        chunks.extend(_ticket_chunks(t))
    for e in json.loads((corpus_dir / "emails.json").read_text()):
        chunks.append(_email_chunk(e))
    for c in json.loads((corpus_dir / "changes.json").read_text()):
        chunks.append(_change_chunk(c))
    return chunks


# ---------------------------------------------------------------------------
# Vector store — ingest + search
# ---------------------------------------------------------------------------


# One client per persist path, built under a lock. Two reasons this can't
# be "construct a fresh PersistentClient per call" anymore:
# 1. Thread-safety: concurrent construction races inside chromadb's shared-
#    system registry (found live: parallel coverage_report died with
#    "Could not connect to tenant default_tenant" / RustBindingsAPI errors).
# 2. It was always wasteful — the client is designed to be long-lived.
# The CLIENT is cached, not the collection: ingest() drops and recreates
# the collection, and a cached collection handle would go stale.
_clients: dict[str, "chromadb.ClientAPI"] = {}
_client_lock = threading.Lock()


def _get_collection(persist_dir: str | Path):
    """Open (or create) the persistent collection.

    hnsw:space=cosine: Chroma defaults to L2 (euclidean) distance; cosine is
    the standard for normalized text embeddings, where direction carries the
    meaning and length is irrelevant (we normalized to length 1 anyway).
    """
    key = str(persist_dir)
    with _client_lock:
        client = _clients.get(key)
        if client is None:
            client = chromadb.PersistentClient(path=key)
            _clients[key] = client
    return client.get_or_create_collection(
        COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
    )


def ingest(
    corpus_dir: str | Path = "assistant/data/corpus",
    persist_dir: str | Path = DEFAULT_PERSIST_DIR,
) -> int:
    """(Re)build the vector store from the corpus. Returns the chunk count.

    Strategy: drop and rebuild the whole collection. With ~60 chunks this
    takes seconds, and full rebuild is the only approach that can't drift
    out of sync with the source files. Incremental upserts + tombstones are
    a production-scale problem (CR-3 live connectors); don't buy that
    complexity before the data size demands it.

    The pgvector path does the same rebuild as one transaction (see
    core/pgvector_store.py); chunking and embedding are shared, so both
    stores hold identical ids, text and vectors.
    """
    chunks = build_chunks(corpus_dir)
    embeddings = _embed([c.text for c in chunks])

    if store_kind() == "pgvector":
        from core import pgvector_store

        return pgvector_store.ingest_chunks(
            [(c.chunk_id, c.text, c.metadata, e) for c, e in zip(chunks, embeddings)],
            store=store_namespace(persist_dir),
        )

    client = chromadb.PersistentClient(path=str(persist_dir))
    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass  # first run: nothing to delete
    collection = _get_collection(persist_dir)
    collection.add(
        ids=[c.chunk_id for c in chunks],
        documents=[c.text for c in chunks],
        metadatas=[c.metadata for c in chunks],
        embeddings=embeddings,
    )
    return len(chunks)


@dataclass
class SearchHit:
    """One search result, ready to cite: text + provenance + similarity."""

    chunk_id: str
    text: str
    metadata: dict[str, Any]
    similarity: float  # 1.0 = identical direction, 0.0 = unrelated


def search(
    query: str,
    k: int = 5,
    where: dict | None = None,
    persist_dir: str | Path = DEFAULT_PERSIST_DIR,
) -> list[SearchHit]:
    """Semantic search: embed the query, return the k nearest chunks.

    `where` is Chroma's metadata filter, e.g. {"doc_type": "email"} or
    {"ticket_id": "TCK-1002"} — this is what makes "find evidence FOR THIS
    TICKET" possible instead of hoping similarity alone stays on-topic.
    Chroma returns cosine DISTANCE (1 - similarity); we flip it back so
    bigger = better in every caller. pgvector's `<=>` is the same cosine
    distance, so the number means the same thing whichever store answered.
    """
    query_embedding = _embed([query])

    if store_kind() == "pgvector":
        from core import pgvector_store

        return [
            SearchHit(chunk_id=cid, text=text, metadata=meta, similarity=sim)
            for cid, text, meta, sim in pgvector_store.search_chunks(
                query_embedding[0], k=k, where=where, store=store_namespace(persist_dir)
            )
        ]

    collection = _get_collection(persist_dir)
    result = collection.query(
        query_embeddings=query_embedding,
        n_results=k,
        where=where,
        include=["documents", "metadatas", "distances"],
    )
    hits = []
    for chunk_id, text, meta, dist in zip(
        result["ids"][0],
        result["documents"][0],
        result["metadatas"][0],
        result["distances"][0],
    ):
        hits.append(
            SearchHit(chunk_id=chunk_id, text=text, metadata=meta, similarity=1.0 - dist)
        )
    return hits
