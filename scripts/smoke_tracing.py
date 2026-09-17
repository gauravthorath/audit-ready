"""Live smoke for Step 37: one correlation id on the http line, the run line and every model call.

Starts the API in-process (TestClient over the real app, real LangGraph runner,
real model), sends one request with `X-Request-ID: smoke-<n>` that audits one
ticket against one control, then reads back the log lines and checks that the
id is on the `http.request` line, the `run.start`/`run.end` lines, the
`batch.pair` line and at least two `llm.call` lines (judge + verify), each with
a positive `elapsed_ms`. Costs one pair (~3,500 tokens).

    uv run python scripts/smoke_tracing.py

Needs OPENROUTER_API_KEY (or a BYO endpoint) and an ingested vector store.
"""

from __future__ import annotations

import io
import json
import logging
import sys
import time
import uuid

from fastapi.testclient import TestClient

from api.main import app
from api.runs import RunRegistry
from core.tracing import configure_logging


def main() -> int:
    buf = io.StringIO()
    configure_logging(fmt="json", level="INFO", stream=buf)
    import tempfile
    app.state.runs = RunRegistry(tempfile.mkdtemp(prefix="smoke-tracing-"))
    rid = f"smoke-{uuid.uuid4().hex[:6]}"

    with TestClient(app) as client:
        r = client.post(
            "/audit",
            json={"pack_id": "iso9001-sample", "ticket_ids": ["TCK-1001"],
                  "control_ids": ["ISO9001-8.6-release-signoff"]},
            headers={"X-Request-ID": rid},
        )
        if r.status_code != 202:
            print(f"FAILED: POST /audit -> {r.status_code} {r.text}")
            return 1
        if r.headers.get("x-request-id") != rid:
            print(f"FAILED: id not echoed: {r.headers.get('x-request-id')!r}")
            return 1
        run_id = r.json()["run_id"]
        deadline = time.time() + 180
        while time.time() < deadline:
            snap = client.get(f"/audit/{run_id}").json()
            if snap["status"] in ("done", "error"):
                break
            time.sleep(0.5)
        if snap["status"] != "done":
            print(f"FAILED: run ended {snap['status']}: {snap.get('error')}")
            return 1

    lines = [json.loads(l) for l in buf.getvalue().splitlines() if l.startswith("{")]
    mine = [l for l in lines if l.get("request_id") == rid]
    events = {}
    for l in mine:
        events.setdefault(l["event"], []).append(l)

    problems = []
    if not any(l.get("path") == "/audit" and l.get("status") == 202 for l in events.get("http.request", [])):
        problems.append("no http.request line for POST /audit with the id")
    if not events.get("run.start") or events["run.start"][0].get("run_id") != run_id:
        problems.append("run.start missing or without run_id")
    if not events.get("run.end") or events["run.end"][0].get("status") != "done":
        problems.append("run.end missing or not done")
    pairs = events.get("batch.pair", [])
    if len(pairs) != 1 or pairs[0].get("pair") != "TCK-1001xISO9001-8.6-release-signoff":
        problems.append(f"batch.pair wrong: {pairs}")
    calls = events.get("llm.call", [])
    if len(calls) < 2:
        problems.append(f"expected >=2 llm.call lines (judge + verify), got {len(calls)}")
    if any(not isinstance(c.get("elapsed_ms"), int) or c["elapsed_ms"] <= 0 for c in calls):
        problems.append("an llm.call has no positive elapsed_ms")
    if any(c.get("run_id") != run_id or c.get("pair") is None for c in calls):
        problems.append("an llm.call lost run_id or pair")

    row = snap["report"]["rows"][0]
    print(f"request {rid} -> run {run_id}: {len(mine)} lines share the id")
    for e, ls in events.items():
        print(f"  {e:<14} x{len(ls)}  " + ", ".join(
            f"{l.get('elapsed_ms')}ms" for l in ls if l.get("elapsed_ms") is not None))
    print(f"  pair elapsed_ms={row.get('elapsed_ms')} report elapsed_ms={snap['report']['elapsed_ms']} "
          f"verdict={row.get('verdict')} status={row.get('status')}")
    if problems:
        print("FAILED: " + "; ".join(problems))
        return 1
    print("PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
