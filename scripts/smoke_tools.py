"""Smoke test for core/tools.py - the three domain tools end to end.

    uv run python scripts/smoke_tools.py

Costs ~10 LLM calls (3 decompositions, cached after first use + 7 judgments).

The check_ticket cases are chosen to prove RETRIEVAL is now load-bearing:
- TCK-1001 vs client-comm: the acknowledgement lives in the ticket, but the
  scope-confirmation email EML-2001 arrives via retrieval.
- TCK-1014 vs traceability: the planted dangling references (CHG-3099,
  EML-2099 don't exist) - retrieval finds nothing for them, judge must gap.
- TCK-1003 vs client-comm: internal ticket, no customer - must come back
  not_applicable, NOT gap (the new fourth verdict earns its keep here).
"""

import sys

from core.tools import check_ticket, coverage_report, find_evidence

CHECK_CASES = [
    ("TCK-1001", "ISO9001-8.2.1-client-comm", {"satisfied"}),
    ("TCK-1014", "ISO9001-8.5.2-traceability", {"gap"}),
    ("TCK-1003", "ISO9001-8.2.1-client-comm", {"not_applicable"}),
]


def main() -> int:
    failures = 0

    # --- Tool 1: find_evidence ---------------------------------------------
    hits = find_evidence(
        "client approval to close the dispatch notification email delay issue", k=3
    )
    ok = any(h["chunk_id"] == "EML-2001" for h in hits)
    failures += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  find_evidence: EML-2001 in top-3")

    hits = find_evidence("approval", ticket_id="TCK-1002", doc_type="email", k=2)
    ok = bool(hits) and all(h["doc_type"] == "email" for h in hits)
    failures += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  find_evidence: combined filters honoured\n")

    # --- Tool 2: check_ticket ----------------------------------------------
    for ticket_id, control_id, accepted in CHECK_CASES:
        r = check_ticket(ticket_id, control_id)
        ok = r["verdict"] in accepted
        failures += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  check_ticket {ticket_id} vs {control_id}")
        print(f"      expected {sorted(accepted)}, got: {r['verdict']}")
        print(f"      rationale: {r['rationale'][:160]}")
        print(f"      citations: {r['citations']}")
        print(f"      evidence chunks: {r['evidence_chunks']}\n")

    # --- Tool 3: coverage_report (scoped 2x2) --------------------------------
    report = coverage_report(
        ticket_ids=["TCK-1001", "TCK-1002"],
        control_ids=["ISO9001-8.2.1-client-comm", "ISO9001-8.6-release-signoff"],
    )
    signoff = report["per_control"]["ISO9001-8.6-release-signoff"]
    comm = report["per_control"]["ISO9001-8.2.1-client-comm"]
    print("coverage_report (2 tickets x 2 controls):")
    for cid, row in report["per_control"].items():
        print(f"  {cid:<32} {row['counts']}  failing={len(row['failing_tickets'])}")
    print(f"  readiness_score: {report['readiness_score']}")
    print(f"  tokens: {report['prompt_tokens']}p + {report['completion_tokens']}c")

    # TCK-1002 is the planted signoff gap; TCK-1001 is clean on both.
    ok = (
        signoff["counts"]["gap"] == 1
        and signoff["counts"]["satisfied"] == 1
        and comm["counts"]["satisfied"] >= 1
        and report["readiness_score"] is not None
        and 0.0 < report["readiness_score"] < 1.0
    )
    failures += 0 if ok else 1
    print(f"\n{'PASS' if ok else 'FAIL'}  coverage_report aggregates match ground truth")

    print(f"\nsmoke_tools: {'ALL GOOD' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
