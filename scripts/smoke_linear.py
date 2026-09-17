"""Smoke test for the Linear evidence connector (CR-3).

Hits the real Linear GraphQL API with a real personal API key, so it is a
script and not a unit test. It proves the query still matches Linear's schema —
the failure mode a mocked test cannot catch, because a renamed field returns
200 with an `errors` array rather than a transport error.

    uv run python scripts/smoke_linear.py
    uv run python scripts/smoke_linear.py --team AUD --limit 3
    uv run python scripts/smoke_linear.py --team AUD --out assistant/data/live-linear

Needs AUDITREADY_LINEAR_TOKEN.
"""

from __future__ import annotations

import argparse
import json
import sys

from dotenv import load_dotenv

from audit.connectors import ConnectorError, snapshot, validate_records
from audit.linear_evidence import LinearIssuesSource, linear_token


def main(argv: list[str] | None = None) -> int:
    load_dotenv()  # the connectors read env; a script is where .env gets loaded
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--team", help="team key, e.g. AUD (default: whole workspace)")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--out", help="also write a snapshot here (default: fetch only)")
    args = parser.parse_args(argv)

    try:
        linear_token()
    except ConnectorError as exc:
        print(f"FAIL: {exc}")
        return 1

    source = LinearIssuesSource(args.team)
    print(f"endpoint: {source.endpoint}")
    print(f"team:     {args.team or '(whole workspace)'}")
    try:
        records = source.fetch(limit=args.limit)
    except ConnectorError as exc:
        print(f"FAIL: {exc}")
        return 1

    if not records.tickets:
        print("FAIL: the workspace returned no issues — check the team key, or drop "
              "--team to fetch everything")
        return 1

    try:
        validate_records(records)
    except ConnectorError as exc:
        print(f"FAIL: fetched {len(records.tickets)} tickets but the contract gate "
              f"refused them: {exc}")
        return 1

    print(f"OK: {len(records.tickets)} tickets over {records.detail['pages']} page(s), "
          f"contract-valid")
    first = records.tickets[0]
    print(f"  {first['ticket_id']}: {first['title'][:60]!r}")
    print(f"  type={first['type']} priority={first['priority']} status={first['status']}")
    print(f"  comments={len(first['comments'])} "
          f"links={first['linked_changes']}/{first['linked_emails']}")
    print(f"  url={first['source_url']}")

    notes = records.detail.get("mapping_notes", {})
    print(f"  classifications guessed for {len(notes)}/{len(records.tickets)} tickets")
    for ticket_id, why in list(notes.items())[:2]:
        print(f"    {ticket_id}: {'; '.join(why)}")
    for field, why in records.detail.get("not_supplied", {}).items():
        print(f"  not supplied: {field} — {why}")

    if args.out:
        provenance = snapshot(records, args.out)
        print(f"wrote {args.out}/ — provenance: {json.dumps(provenance['counts'])}")
        print(f"  next: uv run python audit/batch.py --corpus {args.out} --pack <pack>")

    return 0


if __name__ == "__main__":
    sys.exit(main())
