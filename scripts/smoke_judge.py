"""Smoke test for core/judge.py — the walking skeleton, end to end.

    uv run python scripts/smoke_judge.py

Judges three (ticket, control) pairs with KNOWN ground-truth verdicts,
covering all three verdict values:

    TCK-1001 vs release-signoff  -> satisfied  (clean ticket)
    TCK-1002 vs release-signoff  -> gap        (planted: no sign-off)
    TCK-1002 vs client-comm      -> partial    (informed, never acknowledged)

Costs three or four LLM calls. Exits 1 on any mismatch — though note the
'partial' boundary is the genuinely hard case; if it ever flakes, that is
signal about the rubric or the model, not noise.
"""

import json
import sys
from pathlib import Path

from core.checklists import load_pack
from core.judge import judge_ticket

PACK_PATH = Path("assistant/data/checklists/iso9001.yaml")
TICKETS_PATH = Path("assistant/data/corpus/tickets.json")

CASES = [
    ("TCK-1001", "ISO9001-8.6-release-signoff", "satisfied"),
    ("TCK-1002", "ISO9001-8.6-release-signoff", "gap"),
    ("TCK-1002", "ISO9001-8.2.1-client-comm", "partial"),
]


def main() -> int:
    pack = load_pack(PACK_PATH)
    tickets = {t["ticket_id"]: t for t in json.loads(TICKETS_PATH.read_text())}

    failures = 0
    total_tokens = 0
    for ticket_id, control_id, expected in CASES:
        result = judge_ticket(tickets[ticket_id], pack.get_control(control_id))
        v = result.verdict
        total_tokens += result.prompt_tokens + result.completion_tokens
        ok = v.verdict == expected
        failures += 0 if ok else 1

        print(f"{'PASS' if ok else 'FAIL'}  {ticket_id} vs {control_id}")
        print(f"      expected: {expected}   got: {v.verdict}")
        print(f"      rationale: {v.rationale}")
        print(f"      citations: {v.citations}\n")

    print(f"model: {result.model}   total tokens: {total_tokens}")
    print("smoke_judge: ALL GOOD" if failures == 0 else f"smoke_judge: {failures} MISMATCH(ES)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
