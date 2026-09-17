"""Smoke test for core/rag.py — decomposition + multi-query retrieval.

    uv run python scripts/smoke_rag.py

Costs 1-2 LLM calls (the decomposition; searches are local).

The assertion: for TCK-1001 vs the client-comm control, the merged evidence
must contain all three evidence moments the rubric demands —
    TCK-1001#header  (resolution + signoff summary)
    EML-2001 or TCK-1001#c0  (logged client communication)
    TCK-1001#c2      (explicit client acknowledgement/approval)
— and for comparison we print what NAIVE RAG (whole rubric as one query,
k=3, same scope) retrieves, so the difference is visible, not theoretical.
"""

import sys

from core.checklists import load_pack
from core.rag import retrieve_evidence
from core.retrieval import search

CONTROL_ID = "ISO9001-8.2.1-client-comm"
TICKET_ID = "TCK-1001"

# Each requirement is satisfied if ANY of its chunk-ids shows up
REQUIREMENTS = {
    "resolution/header": {"TCK-1001#header"},
    "client communication": {"EML-2001", "TCK-1001#c0"},
    "explicit acknowledgement": {"TCK-1001#c2"},
}


def main() -> int:
    control = load_pack("assistant/data/checklists/iso9001.yaml").get_control(CONTROL_ID)

    bundle = retrieve_evidence(control, ticket_id=TICKET_ID)
    print(f"decomposed {CONTROL_ID} into {len(bundle.queries)} sub-queries:")
    for q in bundle.queries:
        print(f"  - {q}")
    print(f"\nmerged evidence for {TICKET_ID} ({len(bundle.hits)} chunks):")
    for h in bundle.hits:
        print(f"  {h.similarity:.3f}  {h.chunk_id:<16} {h.text.splitlines()[-1][:70]}")

    got_ids = {h.chunk_id for h in bundle.hits}
    failures = 0
    print()
    for name, accepted in REQUIREMENTS.items():
        ok = bool(got_ids & accepted)
        failures += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  evidence requirement covered: {name}")

    # The comparison: naive single-query RAG, same scope, k=3
    naive = search(control.sufficiency_rubric, k=3, where={"ticket_id": TICKET_ID})
    naive_ids = {h.chunk_id for h in naive}
    print(f"\nnaive RAG (rubric as one query, k=3) retrieved: {sorted(naive_ids)}")
    missed = {
        name for name, accepted in REQUIREMENTS.items() if not (naive_ids & accepted)
    }
    print(
        f"naive missed: {sorted(missed)}" if missed else
        "naive covered everything too (small scope — the gap grows with corpus size)"
    )

    print(f"\nsmoke_rag: {'ALL GOOD' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
