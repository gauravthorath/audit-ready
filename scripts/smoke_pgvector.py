"""Live smoke for the pgvector store — and the parity check that makes the
switch safe to flip.

    docker compose up -d db
    uv run python scripts/smoke_pgvector.py

Ingests the corpus into BOTH stores, runs the same queries (with the same
metadata filters core/tools.py and core/rag.py send) against each, and
reports whether the ranked chunk ids and similarities agree. If they do,
every eval number measured on Chroma holds on pgvector, because retrieval is
the only layer that changed and it returned the same evidence.
"""

from __future__ import annotations

import os
import sys
import time

from dotenv import load_dotenv

load_dotenv()

DSN = os.getenv("AUDITREADY_PG_DSN", "postgresql://auditready:auditready@localhost:5432/auditready")
os.environ["AUDITREADY_PG_DSN"] = DSN

from core import retrieval  # noqa: E402  (after env is set)

QUERIES = [
    ("client acknowledged the fix before closure", None),
    ("who approved the change and when", {"doc_type": "change"}),
    ("root cause and corrective action", {"ticket_id": "TCK-1003"}),
    ("email confirming resolution", {"$and": [{"ticket_id": "TCK-1002"}, {"doc_type": "email"}]}),
    ("release sign-off recorded", {"doc_type": {"$in": ["ticket", "ticket_comment"]}}),
]


def run(kind: str):
    os.environ["AUDITREADY_VECTOR_STORE"] = kind
    t = time.perf_counter()
    n = retrieval.ingest()
    print(f"[{kind}] ingested {n} chunks in {time.perf_counter() - t:.1f}s  ({retrieval.store_label()})")
    out = {}
    t = time.perf_counter()
    for q, where in QUERIES:
        out[q] = [(h.chunk_id, round(h.similarity, 4)) for h in retrieval.search(q, k=5, where=where)]
    print(f"[{kind}] {len(QUERIES)} searches in {(time.perf_counter() - t) * 1000:.0f} ms")
    return out


def main() -> int:
    chroma = run("chroma")
    pg = run("pgvector")
    mismatches = 0
    for q, _ in QUERIES:
        a, b = chroma[q], pg[q]
        same_ids = [x[0] for x in a] == [x[0] for x in b]
        close = same_ids and all(abs(x[1] - y[1]) < 1e-3 for x, y in zip(a, b))
        flag = "OK " if close else "DIFF"
        mismatches += not close
        print(f"  {flag} {q!r}")
        if not close:
            print(f"       chroma:   {a}")
            print(f"       pgvector: {b}")
    print("parity:", "identical rankings" if mismatches == 0 else f"{mismatches} query(ies) differ")
    return 0 if mismatches == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
