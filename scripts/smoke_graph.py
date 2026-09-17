"""Smoke test for audit/graph.py — one control through the LangGraph agent.

    uv run python scripts/smoke_graph.py

Needs OPENROUTER_API_KEY in .env and an ingested vector store (.chroma/ is
built on first use by core.retrieval). Costs ~6-9 LLM calls: per pair one
judge + one verifier call, plus a re-judge when the verifier objects.

Same three cases as scripts/smoke_tools.py so the graph can be compared with
check_ticket like-for-like, plus one deliberately hard case:
- TCK-1001 vs client-comm: clean, should finalize on the first pass.
- TCK-1014 vs traceability: planted dangling references (CHG-3099, EML-2099
  do not exist) — a judge that cites them trips check_citations, and the
  graph must send that back as feedback rather than just report it.
- TCK-1003 vs client-comm: internal ticket → not_applicable, not gap.

PASS = verdict in the accepted set AND the graph ended with a status. A
`needs_review` status is not a failure — it is the graph doing its job —
but it is printed loudly so a surprising flag is noticed.
"""

import json
import sys
from pathlib import Path

from audit.graph import run_control
from core.checklists import load_pack
from core.tools import DEFAULT_CORPUS_DIR, DEFAULT_PACK_PATH

CASES = [
    ("TCK-1001", "ISO9001-8.2.1-client-comm", {"satisfied"}),
    ("TCK-1014", "ISO9001-8.5.2-traceability", {"gap"}),
    ("TCK-1003", "ISO9001-8.2.1-client-comm", {"not_applicable"}),
]


def main() -> int:
    pack = load_pack(DEFAULT_PACK_PATH)
    tickets = {
        t["ticket_id"]: t
        for t in json.loads((Path(DEFAULT_CORPUS_DIR) / "tickets.json").read_text())
    }

    failures = 0
    total_p = total_c = 0
    for ticket_id, control_id, accepted in CASES:
        r = run_control(tickets[ticket_id], pack.get_control(control_id))
        # The graph's promise is "never FINALIZE a wrong verdict", not "always be
        # right": a wrong verdict that ended in needs_review was caught, so it passes.
        ok = r["verdict"] in accepted or r["status"] == "needs_review"
        failures += 0 if ok else 1
        total_p += r["prompt_tokens"]
        total_c += r["completion_tokens"]
        flag = ""
        if r["status"] == "needs_review":
            flag = "  ⚠ NEEDS REVIEW" + ("" if r["verdict"] in accepted else " (wrong verdict caught)")
        print(f"{'PASS' if ok else 'FAIL'}  {ticket_id} vs {control_id}{flag}")
        print(f"      expected {sorted(accepted)}, got: {r['verdict']} "
              f"(status={r['status']}, attempts={r['attempts']})")
        print(f"      rationale: {r['rationale'][:160]}")
        print(f"      citations: {r['citations']}  → {r['citation_check']['status']}")
        print(f"      verifier: agrees={r['verification']['agrees']} "
              f"issues={r['verification']['issues']}")
        print("      trace:")
        for line in r["trace"]:
            print(f"        · {line}")
        print()

    print(f"tokens: {total_p}p + {total_c}c across {len(CASES)} pairs")
    if failures:
        print(f"FAILED: {failures} FINALIZED verdict(s) outside the accepted set")
        return 1
    print("PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
