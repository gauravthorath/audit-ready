"""Smoke test for the Jira Cloud evidence connector (CR-3).

Hits a real Jira Cloud site with a real API token, so it is a script and not a
unit test. It proves the three things the offline tests cannot: the site
answers, `/search/jql` still pages the way we page it, and what comes back
survives the contract gate.

    uv run python scripts/smoke_jira.py --project AUD
    uv run python scripts/smoke_jira.py --jql 'project = AUD AND status = Done' --limit 3
    uv run python scripts/smoke_jira.py --project AUD --out assistant/data/live-jira

Needs AUDITREADY_JIRA_SITE, AUDITREADY_JIRA_EMAIL, AUDITREADY_JIRA_TOKEN.
"""

from __future__ import annotations

import argparse
import json
import sys

from dotenv import load_dotenv

from audit.connectors import ConnectorError, snapshot, validate_records
from audit.jira_evidence import JiraIssuesSource, jira_auth, jira_site


def main(argv: list[str] | None = None) -> int:
    load_dotenv()  # the connectors read env; a script is where .env gets loaded
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", help="project key, e.g. AUD")
    parser.add_argument("--jql", help="raw JQL (overrides --project)")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--out", help="also write a snapshot here (default: fetch only)")
    args = parser.parse_args(argv)

    if not args.project and not args.jql:
        parser.error("pass --project KEY or --jql QUERY")

    # Fail on config before spending a round trip, and name the missing variable.
    try:
        site = jira_site()
        jira_auth()
    except ConnectorError as exc:
        print(f"FAIL: {exc}")
        return 1
    print(f"site: {site}")

    source = JiraIssuesSource(args.project, jql=args.jql)
    print(f"jql:  {source.jql}")
    try:
        records = source.fetch(limit=args.limit)
    except ConnectorError as exc:
        print(f"FAIL: {exc}")
        return 1

    if not records.tickets:
        # Empty project and wrong key look identical from /search/jql. One more
        # read tells them apart and names the keys that do exist.
        import httpx
        from audit.jira_evidence import API
        with httpx.Client(base_url=site, headers={"Authorization": jira_auth(),
                                                    "Accept": "application/json"}, timeout=30) as c:
            r = c.get(f"{API}/project/search", params={"maxResults": 50})
            keys = [p["key"] for p in r.json().get("values", [])] if r.status_code == 200 else []
        if args.project and args.project not in keys:
            print(f"FAIL: no project with key {args.project!r} on {site}. "
                  f"Visible projects: {', '.join(keys) or 'none'}")
        else:
            print("FAIL: the query returned no issues — the project exists but is empty. "
                  f"Seed it: uv run python scripts/seed_demo_evidence.py --jira {args.project or '<KEY>'}")
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
