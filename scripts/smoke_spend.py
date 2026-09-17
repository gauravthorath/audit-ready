"""Live smoke for Step 38: a real chat turn lands on the ledger and moves GET /spend.

Runs the real app in-process with a temp ledger: `/spend` starts at zero, one
chat turn is answered by the model and charged, `/spend` shows the charge, then
the per-caller budget is set to exactly what was spent and a second turn is
refused with a 429 before it reaches the model. Costs one short chat turn.

    uv run python scripts/smoke_spend.py

Needs OPENROUTER_API_KEY (or a BYO endpoint) and an ingested vector store.
"""

from __future__ import annotations

import dataclasses
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from api.main import app
from api.ratelimit import RateLimits
from api.sessions import MemorySessionStore
from core.spend import SpendLedger


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="smoke-spend-"))
    app.state.ledger = SpendLedger(tmp / "spend.jsonl")
    app.state.sessions = MemorySessionStore()
    app.state.limits = RateLimits.build(30, 6, 400)
    app.state.settings = dataclasses.replace(app.state.settings, actor_daily_budget_usd=None)

    with TestClient(app) as client:
        before = client.get("/spend").json()
        if before["today_usd"] != 0 or before["remaining_usd"] is not None:
            print(f"FAILED: unexpected starting spend {before}")
            return 1

        r = client.post("/chat", json={"question": "Is TCK-1001 compliant with release sign-off?"})
        if r.status_code != 200:
            print(f"FAILED: first turn -> {r.status_code} {r.text[:200]}")
            return 1
        turn = r.json()
        after = client.get("/spend").json()
        if not after["today_usd"] > 0:
            print(f"FAILED: ledger did not move: {after}")
            return 1

        # The budget becomes exactly what was spent: the next turn must be refused.
        app.state.settings = dataclasses.replace(
            app.state.settings, actor_daily_budget_usd=after["today_usd"])
        capped = client.get("/spend").json()
        r2 = client.post("/chat", json={"question": "and TCK-1002?", "session_id": turn["session_id"]})

    lines = (tmp / "spend.jsonl").read_text().splitlines()
    print(f"turn: {turn['prompt_tokens']} + {turn['completion_tokens']} tokens, "
          f"{len(turn['tool_calls'])} tool call(s)")
    print(f"spend after one turn: ${after['today_usd']:.6f}; then capped at ${capped['actor_budget_usd']} "
          f"(remaining ${capped['remaining_usd']:.6f}); ledger lines: {len(lines)}")
    print(f"second turn -> {r2.status_code}: {r2.json().get('detail', '')[:90]}")
    if len(lines) != 1:
        print("FAILED: expected exactly one ledger line (the refused turn must not be charged)")
        return 1
    if r2.status_code != 429 or capped["remaining_usd"] != 0.0:
        print(f"FAILED: expected a 429 once the budget was used up, got {r2.status_code}")
        return 1
    print("PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
