"""Live smoke for Step 41: a conversation survives the process that held it.

Runs against a real Postgres (the `db` service in compose.yaml). It proves the
property the in-memory store cannot have:

  1. `/health` reports the postgres session store.
  2. A chat turn is answered by the real model and the conversation is stored.
  3. **The store is thrown away and rebuilt from the DSN** — the same thing a
     restart, a deploy or a second uvicorn worker does — and the next turn in
     that conversation still has the first one in its memory.
  4. The conversation is listed under its owner and not under anyone else.
  5. Ending it deletes the row.

    docker compose up -d db
    AUDITREADY_PG_DSN=postgresql://auditready:auditready@localhost:5432/auditready \
      uv run python scripts/smoke_sessions.py

Needs OPENROUTER_API_KEY (or a BYO endpoint) and an ingested vector store.
Costs two short chat turns.
"""

from __future__ import annotations

import dataclasses
import os
import sys
import uuid

from fastapi.testclient import TestClient

from api.main import app
from api.sessions import PostgresSessionStore, build_session_store


def main() -> int:
    dsn = os.getenv("AUDITREADY_PG_DSN", "").strip()
    if not dsn:
        print("FAILED: set AUDITREADY_PG_DSN (docker compose up -d db)")
        return 1

    app.state.sessions = build_session_store("postgres", dsn)
    app.state.settings = dataclasses.replace(app.state.settings, session_store="postgres")
    marker = uuid.uuid4().hex[:8]

    with TestClient(app) as client:
        health = client.get("/health").json()
        if health["sessions"] != "postgres":
            print(f"FAILED: /health says sessions={health['sessions']!r}")
            return 1

        first = client.post(
            "/chat",
            json={"question": f"Does ticket TCK-1001 satisfy ISO9001-8.6? Call it check {marker}."},
        )
        if first.status_code != 200:
            print(f"FAILED: first turn -> {first.status_code} {first.text[:200]}")
            return 1
        session_id = first.json()["session_id"]
        first_tokens = first.json()["session"]["prompt_tokens"]

        # The restart: nothing of this conversation is left in the process.
        app.state.sessions = PostgresSessionStore(dsn)
        print(f"  session {session_id} — store rebuilt from the DSN (the 'restart')")

        second = client.post(
            "/chat",
            json={"question": "What was the ticket id I just asked about?", "session_id": session_id},
        )
        if second.status_code != 200:
            print(f"FAILED: second turn -> {second.status_code} {second.text[:200]}")
            return 1
        answer = second.json()["answer"]
        totals = second.json()["session"]

        listed = client.get("/chat").json()
        mine = [c for c in listed if c["session_id"] == session_id]
        stranger = app.state.sessions.get("someone-else", session_id)
        ended = client.delete(f"/chat/{session_id}").json()["ended"]
        gone = app.state.sessions.get("testclient", session_id)

    print(f"  answer after the restart: {answer[:120]}")
    print(f"  tokens accumulated across the restart: {first_tokens} -> {totals['prompt_tokens']}")
    print(f"  listed as: {mine[0]['title'][:60]!r} ({mine[0]['turns']} turns)" if mine else "  NOT LISTED")

    problems = []
    if "TCK-1001" not in answer:
        problems.append("the rebuilt store did not carry the first turn's memory into the second")
    if totals["prompt_tokens"] <= first_tokens:
        problems.append("token totals did not accumulate across the restart")
    if not mine or mine[0]["turns"] != 2:
        problems.append(f"listing wrong: {listed}")
    if stranger is not None:
        problems.append("another actor could read the conversation")
    if not ended or gone is not None:
        problems.append("the row was not deleted")
    if problems:
        print("FAILED: " + "; ".join(problems))
        return 1
    print("PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
