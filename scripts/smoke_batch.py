"""Smoke test for audit/batch.py — a scoped live batch, rendered.

    uv run python scripts/smoke_batch.py

2 tickets × 2 controls = 4 pairs through the LangGraph agent (~10 LLM calls).
Prints the Markdown report and writes it under reports/. Passes if the run
completes, every row has a legal status, and the score is consistent with
the verified rows. Full pack: `uv run python -m audit.batch`.
"""

import sys
import time

from audit.batch import STATUSES, render_markdown, run_batch, write_report
from core.checklists import load_pack
from core.tools import DEFAULT_PACK_PATH, load_tickets

TICKETS = ["TCK-1001", "TCK-1002"]          # one clean, one planted-deficient
CONTROLS = ["ISO9001-8.2.1-client-comm", "ISO9001-8.6-release-signoff"]


def main() -> int:
    pack = load_pack(DEFAULT_PACK_PATH)
    tickets = load_tickets()

    def show(done, total, label):
        print(f"  {done}/{total}  {label}")

    t0 = time.time()
    report = run_batch(pack, tickets, TICKETS, CONTROLS, progress=show)
    print(f"\n{time.time() - t0:.0f}s\n")
    print(render_markdown(report))
    md, js = write_report(report, "reports")
    print(f"written: {md}\n         {js}")

    bad = [r for r in report.rows if r["status"] not in STATUSES]
    if bad or report.total_pairs != 4:
        print("FAILED: malformed rows"); return 1
    if report.verified_pairs and report.readiness_score is None:
        print("FAILED: verified rows but no score"); return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
