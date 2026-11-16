"""Smoke test for the live MCP evidence connector (CR-3).

Hits the real GitHub MCP server with a real token, so it is a script and not
a unit test. It proves the three things the offline tests cannot: the server
answers, the tools we depend on exist under the names we call, and what comes
back survives the contract gate.

    uv run python scripts/smoke_mcp.py                       # default repo
    uv run python scripts/smoke_mcp.py --repo owner/name --limit 3
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from audit.connectors import ConnectorError, snapshot, validate_records
from audit.mcp_evidence import GitHubIssuesSource, github_token

DEFAULT_REPO = "langchain-ai/langgraph"


def main(argv: list[str] | None = None) -> int:
    load_dotenv()  # the connectors read env; a script is where .env gets loaded
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=DEFAULT_REPO, help="owner/name")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--state", default="CLOSED", choices=["OPEN", "CLOSED"])
    parser.add_argument(
        "--out",
        help="also write a snapshot here (default: fetch only, write nothing)",
    )
    args = parser.parse_args(argv)

    try:
        github_token()
    except ConnectorError as exc:
        print(f"FAIL: {exc}")
        return 1

    owner, _, repo = args.repo.partition("/")
    if not repo:
        print("FAIL: --repo wants owner/name")
        return 1

    source = GitHubIssuesSource(owner=owner, repo=repo, state=args.state)
    print(f"fetching up to {args.limit} {args.state.lower()} issues from {args.repo} ...")
    try:
        records = source.fetch(limit=args.limit)
    except ConnectorError as exc:
        print(f"FAIL: {exc}")
        return 1

    if not records.tickets:
        print("FAIL: the server answered but sent no issues")
        return 1

    # The contract gate is the whole point: a mapping drift shows up here, not
    # three layers later in a judge prompt.
    try:
        validate_records(records)
    except ConnectorError as exc:
        print(f"FAIL: mapped records break the contract: {exc}")
        return 1

    print(f"ok: {len(records.tickets)} tickets, contract-valid")
    first = records.tickets[0]
    print(f"  {first['ticket_id']}  {first['type']}/{first['priority']}  {first['title'][:60]}")
    print(f"  comments={len(first['comments'])} "
          f"links={first['linked_changes']}/{first['linked_emails']}")
    inferred = records.detail.get("inferred", {})
    print(f"  classifications guessed for {len(inferred)}/{len(records.tickets)} tickets")
    for ticket_id, notes in list(inferred.items())[:2]:
        print(f"    {ticket_id}: {'; '.join(notes)}")
    for gap in records.detail.get("contract_gaps", []):
        print(f"  gap: {gap}")

    if args.out:
        provenance = snapshot(records, args.out)
        print(f"wrote {args.out}/ - provenance: {json.dumps(provenance['counts'])}")
        print(f"  next: AUDITREADY_CORPUS_DIR={args.out} uv run python audit/batch.py "
              f"--corpus {args.out} --pack <pack>")

    return 0


if __name__ == "__main__":
    sys.exit(main())
