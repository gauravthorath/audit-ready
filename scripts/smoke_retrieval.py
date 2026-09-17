"""Smoke test for core/retrieval.py — ingest the corpus, prove semantic search.

    uv run python scripts/smoke_retrieval.py

First run downloads the embedding model (~90MB, then cached). Each case
checks that the expected chunk appears in the top-3 — semantic search is
ranked, not exact, so "top-3 contains it" is the honest assertion. Note the
queries share almost no words with their targets: that's the point.
"""

import sys

from core.retrieval import ingest, search

# (query, expected chunk-id prefix, metadata filter or None)
#
# Lesson baked into these cases (found the hard way — see LEARNING.md Step 5):
# a query made ONLY of generic audit vocabulary ("client approved closure")
# ranks poorly corpus-wide — every ticket contains that vocabulary. Queries
# need either a topic anchor (case 1) or a metadata scope (cases 4 & 5).
CASES = [
    # Topic-anchored paraphrase: "dispatch notification email delay" pins the
    # right neighbourhood even though the wording matches no chunk verbatim
    ("client approval to close the dispatch notification email delay issue", "EML-2001", None),
    # Approval facts live in the change record
    ("who authorised the certificate expiry monitoring rollout", "CHG-3001", None),
    # Same query twice: unfiltered vs doc_type filter
    ("manager approval for new VPN user profiles", "EML-2002", {"doc_type": "email"}),
    # Metadata filter as scoping: only chunks of this ticket
    ("was the client informed the accounts were ready", "TCK-1002", {"ticket_id": "TCK-1002"}),
    # The generic query that FAILS corpus-wide works scoped — this is how
    # check_ticket (Step 7) will search: per-ticket, rubric-driven
    ("client acknowledgement approving closure", "TCK-1001#c2", {"ticket_id": "TCK-1001"}),
]


def main() -> int:
    n = ingest()
    print(f"ingested {n} chunks into .chroma/\n")

    failures = 0
    for query, expected_prefix, where in CASES:
        hits = search(query, k=3, where=where)
        top_ids = [h.chunk_id for h in hits]
        ok = any(cid.startswith(expected_prefix) for cid in top_ids)
        failures += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  {query!r}" + (f"  where={where}" if where else ""))
        for h in hits:
            print(f"      {h.similarity:.3f}  {h.chunk_id:<16} {h.text.splitlines()[0][:70]}")
        print()

    print("smoke_retrieval: ALL GOOD" if failures == 0 else f"smoke_retrieval: {failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
