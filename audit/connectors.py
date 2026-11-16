"""Evidence connectors (CR-3): where the records under audit come from.

This module is deliberately small, and the reason is the shape of the engine
underneath it. `audit/batch.py` already takes its tickets as an argument
(`run_batch(pack, tickets, ...)`) and `core/retrieval.ingest` already rebuilds
the vector store from a corpus directory. So a connector never has to touch the
agent, the judge, or the report. It has exactly one job:

    turn whatever a source hands back into the SAME record shape the synthetic
    corpus uses, then write it down.

    source.fetch() -> EvidenceRecords -> snapshot(dir) -> ingest(dir) -> run_batch(load_tickets(dir))

Two decisions worth stating, because both were judgement calls:

*Why write a snapshot to disk instead of streaming live records into the run.*
An audit finding has to stay reproducible against the evidence as it stood when
the finding was made. Live tickets mutate - someone edits a comment, closes an
issue, adds an approval. A snapshot freezes what was audited, and
`provenance.json` records where it came from and when. It also means the live
path and the synthetic path are the same code path from `ingest` onward, so the
connector cannot break the eval.

*Why the shape gate is here and not in each source.* `validate_ticket` is the
contract. Every source - live MCP, file import, a future Jira connector - is
checked against the identical field set, so "the data format did not change" is
a test, not a claim (`tests/test_connectors.py`).

Req: CR-3.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol, runtime_checkable

# The record contract, read off assistant/data/corpus/tickets.json. Every source
# must produce exactly these keys on every ticket. `satisfaction_survey` exists
# on one synthetic ticket and is the reason extra keys are tolerated: the corpus
# itself is not uniform, so the gate checks "no missing, no unknown-required"
# rather than strict equality.
TICKET_FIELDS: frozenset[str] = frozenset(
    {
        "ticket_id",
        "title",
        "type",
        "priority",
        "status",
        "created",
        "closed",
        "reporter",
        "assignee",
        "description",
        "comments",
        "linked_changes",
        "linked_emails",
        "resolution",
        "signoff",
    }
)

# Value domains the checklist rubrics and the judge prompt already assume.
TICKET_TYPES: frozenset[str] = frozenset(
    {"incident", "service_request", "change_request", "complaint"}
)
PRIORITIES: frozenset[str] = frozenset({"high", "medium", "low"})

CHANGE_ID_RE = re.compile(r"\bCHG-\d+\b")
EMAIL_ID_RE = re.compile(r"\bEML-\d+\b")


class ConnectorError(RuntimeError):
    """A source could not be read, or returned records of the wrong shape."""


@dataclass
class EvidenceRecords:
    """One fetch from one source, in corpus shape.

    `changes` and `emails` are frequently empty: a ticket system knows about
    tickets. Change-approval records and client correspondence live in other
    systems, which are separate connectors (see `--artifacts-from` on the CLI).
    An empty list is an honest answer, not a failure - the audit report will
    show the resulting evidence gaps, which is the correct finding.
    """

    tickets: list[dict[str, Any]]
    changes: list[dict[str, Any]] = field(default_factory=list)
    emails: list[dict[str, Any]] = field(default_factory=list)
    source: str = "unknown"
    fetched_at: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.fetched_at:
            self.fetched_at = datetime.now(timezone.utc).isoformat()


@runtime_checkable
class TicketSource(Protocol):
    """What a connector has to be. Deliberately one method.

    Everything harder - pagination, auth, an MCP session, a CSV dialect - is the
    source's own problem and stays behind `fetch`.
    """

    name: str

    def fetch(self, limit: int | None = None) -> EvidenceRecords: ...


def validate_ticket(ticket: dict[str, Any], *, where: str = "ticket") -> None:
    """Raise unless `ticket` matches the corpus contract. The gate all sources pass.

    Checks shape, not truth: a field may be empty, but it may not be absent or
    the wrong Python type, because `core/tools.py` and the judge prompt index
    into these without guarding.
    """
    if not isinstance(ticket, dict):
        raise ConnectorError(f"{where}: expected a dict, got {type(ticket).__name__}")

    missing = sorted(TICKET_FIELDS - set(ticket))
    if missing:
        raise ConnectorError(f"{where}: missing required field(s) {missing}")

    tid = ticket["ticket_id"]
    if not isinstance(tid, str) or not tid.strip():
        raise ConnectorError(f"{where}: ticket_id must be a non-empty string")

    if ticket["type"] not in TICKET_TYPES:
        raise ConnectorError(
            f"{tid}: type {ticket['type']!r} not one of {sorted(TICKET_TYPES)}"
        )
    if ticket["priority"] not in PRIORITIES:
        raise ConnectorError(
            f"{tid}: priority {ticket['priority']!r} not one of {sorted(PRIORITIES)}"
        )

    for key in ("title", "status", "reporter", "description", "resolution"):
        if not isinstance(ticket[key], str):
            raise ConnectorError(f"{tid}: {key} must be a string (got {type(ticket[key]).__name__})")

    for key in ("comments", "linked_changes", "linked_emails"):
        if not isinstance(ticket[key], list):
            raise ConnectorError(f"{tid}: {key} must be a list (got {type(ticket[key]).__name__})")

    for i, comment in enumerate(ticket["comments"]):
        if not isinstance(comment, dict) or not {"author", "timestamp", "body"} <= set(comment):
            raise ConnectorError(
                f"{tid}: comments[{i}] must be a dict with author, timestamp, body"
            )

    if ticket["signoff"] is not None and not isinstance(ticket["signoff"], dict):
        raise ConnectorError(f"{tid}: signoff must be a dict or None")

    # closed may be None (still open); created may not.
    if not isinstance(ticket["created"], str) or not ticket["created"]:
        raise ConnectorError(f"{tid}: created must be a non-empty ISO timestamp")
    if ticket["closed"] is not None and not isinstance(ticket["closed"], str):
        raise ConnectorError(f"{tid}: closed must be an ISO timestamp or None")


def validate_records(records: EvidenceRecords) -> None:
    """Gate a whole fetch, and refuse duplicate ticket ids.

    Duplicates matter because `load_tickets` builds a dict keyed by id: a
    duplicate silently drops a ticket from the audit, which is the kind of
    quiet loss an audit tool must never do.
    """
    seen: set[str] = set()
    for i, ticket in enumerate(records.tickets):
        validate_ticket(ticket, where=f"{records.source}: tickets[{i}]")
        tid = ticket["ticket_id"]
        if tid in seen:
            raise ConnectorError(f"{records.source}: duplicate ticket_id {tid}")
        seen.add(tid)


def blank_ticket(**overrides: Any) -> dict[str, Any]:
    """A contract-shaped ticket with empty values, for sources to fill in.

    Sources build on this rather than assembling a literal, so that adding a
    field to the contract is one edit here instead of one per connector.
    """
    ticket: dict[str, Any] = {
        "ticket_id": "",
        "title": "",
        "type": "service_request",
        "priority": "medium",
        "status": "open",
        "created": "",
        "closed": None,
        "reporter": "",
        "assignee": None,
        "description": "",
        "comments": [],
        "linked_changes": [],
        "linked_emails": [],
        "resolution": "",
        "signoff": None,
    }
    unknown = sorted(set(overrides) - TICKET_FIELDS)
    if unknown:
        raise ConnectorError(f"blank_ticket: unknown field(s) {unknown}")
    ticket.update(overrides)
    return ticket


def fill_ticket(raw: dict[str, Any]) -> dict[str, Any]:
    """Fill any missing contract field with a blank, and keep unknown keys.

    Unknown keys are kept rather than dropped because the synthetic corpus
    itself carries one (`satisfaction_survey` on TCK-1008) and the retrieval
    layer indexes whatever it is given. Dropping fields would make an import
    lossy in a way nobody would notice until an audit finding went missing.
    """
    known = {k: v for k, v in raw.items() if k in TICKET_FIELDS}
    extras = {k: v for k, v in raw.items() if k not in TICKET_FIELDS}
    return blank_ticket(**known) | extras


def find_linked_ids(*texts: str | None) -> tuple[list[str], list[str]]:
    """Recover CHG-/EML- references from free text, first-seen order.

    A live ticket system has no foreign keys into the change or mail system;
    what it has is people writing "see CHG-3001" in a comment. Recovering the
    ids by pattern is the honest mapping, and it is the same join
    `audit/graph.retrieve` then performs by id.
    """
    changes: list[str] = []
    emails: list[str] = []
    for text in texts:
        if not text:
            continue
        for match in CHANGE_ID_RE.findall(text):
            if match not in changes:
                changes.append(match)
        for match in EMAIL_ID_RE.findall(text):
            if match not in emails:
                emails.append(match)
    return changes, emails


# ---------------------------------------------------------------------------
# File import (CR-3: "file import (CSV/JSON)")
# ---------------------------------------------------------------------------


class JsonFileSource:
    """Import a corpus-shaped directory or a single tickets JSON file.

    This is the source that proves the interface offline: it needs no network,
    so `tests/test_connectors.py` drives the whole pipeline through it.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.name = f"json:{self.path}"

    def fetch(self, limit: int | None = None) -> EvidenceRecords:
        if self.path.is_dir():
            tickets = _read_json_list(self.path / "tickets.json")
            changes = _read_json_list(self.path / "changes.json", optional=True)
            emails = _read_json_list(self.path / "emails.json", optional=True)
        else:
            tickets, changes, emails = _read_json_list(self.path), [], []

        if limit is not None:
            tickets = tickets[:limit]
        records = EvidenceRecords(
            tickets=[fill_ticket(t) for t in tickets],
            changes=changes,
            emails=emails,
            source=self.name,
            detail={"path": str(self.path)},
        )
        validate_records(records)
        return records


class CsvFileSource:
    """Import a flat CSV export of a ticket system.

    List-valued columns (`comments`, `linked_changes`, `linked_emails`) are the
    awkward part of any CSV export. Accepted forms: a JSON array in the cell, or
    a semicolon-separated list of ids. `comments` must be JSON if present.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.name = f"csv:{self.path}"

    def fetch(self, limit: int | None = None) -> EvidenceRecords:
        if not self.path.exists():
            raise ConnectorError(f"{self.name}: no such file")

        tickets: list[dict[str, Any]] = []
        with self.path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                tickets.append(self._row_to_ticket(row))
                if limit is not None and len(tickets) >= limit:
                    break

        records = EvidenceRecords(
            tickets=tickets, source=self.name, detail={"path": str(self.path)}
        )
        validate_records(records)
        return records

    @staticmethod
    def _row_to_ticket(row: dict[str, str]) -> dict[str, Any]:
        fields: dict[str, Any] = {
            k: (v or "") for k, v in row.items() if k in TICKET_FIELDS
        }
        for key in ("comments", "linked_changes", "linked_emails"):
            fields[key] = _parse_list_cell(row.get(key), as_json=(key == "comments"))
        for key in ("closed", "assignee"):
            fields[key] = fields.get(key) or None
        if row.get("signoff"):
            fields["signoff"] = json.loads(row["signoff"])
        if not fields.get("linked_changes") and not fields.get("linked_emails"):
            changes, emails = find_linked_ids(
                fields.get("description"), fields.get("resolution")
            )
            fields["linked_changes"], fields["linked_emails"] = changes, emails
        return blank_ticket(**fields)


class StubSource:
    """A connector named in the requirements but not built (CR-3: "others stubbed").

    It fails loudly with what it would have to return, rather than pretending to
    be a source that yields nothing. Someone wiring Jira next reads this message
    and knows the contract.
    """

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self.reason = reason

    def fetch(self, limit: int | None = None) -> EvidenceRecords:
        raise NotImplementedError(
            f"{self.name} connector is stubbed. {self.reason} "
            f"To implement it, return EvidenceRecords whose tickets satisfy "
            f"connectors.validate_ticket (fields: {sorted(TICKET_FIELDS)})."
        )


# Every source named in CR-3 is built (GitHub over MCP, Jira over REST, Linear
# over GraphQL, CSV/JSON, .eml/.mbox). What remains stubbed is stubbed for a
# reason that is about access, not about effort - each message names the thing
# that has to exist before code would mean anything.
STUBS: dict[str, StubSource] = {
    "teams": StubSource(
        "Microsoft Teams",
        "Approvals in regulated delivery are often *said in a channel* - 'yes, go ahead, "
        "approved' - and never written into the ticket, so a Teams channel is real audit "
        "evidence and is the source most likely to close the sign-off gap that GitHub, base "
        "Jira and Linear all leave open. Reading it needs an Entra ID app registration with "
        "ChannelMessage.Read.All (admin consent, not a user token), a tenant to test against, "
        "and a retention policy decision, because a channel export is mostly *not* evidence - "
        "it is a workplace conversation. The mapping itself is the easy part: a message with "
        "an author, a timestamp and a body is already a comment; a reply chain referencing a "
        "ticket id is already a linked_email edge. Written down rather than built because "
        "shipping a mailbox reader without the consent design is the exact mistake the "
        "ethics section of the README refuses to make.",
    ),
}


def _read_json_list(path: Path, *, optional: bool = False) -> list[dict[str, Any]]:
    if not path.exists():
        if optional:
            return []
        raise ConnectorError(f"no such file: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ConnectorError(f"{path}: expected a JSON array of records")
    return data


def _parse_list_cell(cell: str | None, *, as_json: bool) -> list[Any]:
    if not cell or not cell.strip():
        return []
    text = cell.strip()
    if text.startswith("["):
        return json.loads(text)
    if as_json:
        raise ConnectorError(f"expected a JSON array, got {text[:40]!r}")
    return [part.strip() for part in text.split(";") if part.strip()]


# ---------------------------------------------------------------------------
# Snapshot: freeze what was audited
# ---------------------------------------------------------------------------


def snapshot(
    records: EvidenceRecords,
    out_dir: str | Path,
    *,
    artifacts_from: str | Path | None = None,
    emails_from: EvidenceRecords | None = None,
) -> dict[str, Any]:
    """Write `records` as a corpus directory and return the provenance stamp.

    The output is byte-for-byte the layout `core/retrieval.ingest` and
    `core/tools.load_tickets` already read, which is the whole point: from here
    on, live evidence and synthetic evidence are indistinguishable to the
    engine.

    `artifacts_from` copies `changes.json` / `emails.json` from another corpus
    directory when the live source has none of its own. That is not a cheat, it
    is the real architecture: a ticket system holds tickets, and change
    approvals and client correspondence come from separate systems. The
    provenance file records that those two files came from elsewhere, so a
    reader of the report can never mistake a file-imported artifact for a live
    one.

    `emails_from` is a fetch from `audit.email_evidence.EmlFileSource`:
    correspondence a human exported. Those emails are added to whatever the
    snapshot already has (live or borrowed), duplicates by id dropped, and each
    ticket an email names gets the reverse `linked_emails` edge - because the
    judge is handed linked records by id from the ticket side, and an email that
    is only indexed is evidence the judge may never see.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    validate_records(records)

    changes, emails = list(records.changes), list(records.emails)
    borrowed: dict[str, str] = {}
    if artifacts_from is not None:
        donor = Path(artifacts_from)
        if not changes:
            changes = _read_json_list(donor / "changes.json", optional=True)
            borrowed["changes.json"] = str(donor)
        if not emails:
            emails = _read_json_list(donor / "emails.json", optional=True)
            borrowed["emails.json"] = str(donor)

    imported: dict[str, Any] = {}
    if emails_from is not None:
        from audit.email_evidence import link_emails_to_tickets

        have = {e.get("email_id") for e in emails}
        new = [e for e in emails_from.emails if e.get("email_id") not in have]
        emails = emails + new
        links = link_emails_to_tickets(records.tickets, new)
        imported = {
            "source": emails_from.source,
            "emails": len(new),
            "duplicates_skipped": len(emails_from.emails) - len(new),
            "ticket_links_added": links,
            "detail": emails_from.detail,
        }

    _write_json(out / "tickets.json", records.tickets)
    _write_json(out / "changes.json", changes)
    _write_json(out / "emails.json", emails)

    dangling_changes, dangling_emails = _dangling(records.tickets, changes, emails)
    provenance = {
        "source": records.source,
        "fetched_at": records.fetched_at,
        "written_at": datetime.now(timezone.utc).isoformat(),
        "emails_imported": imported,
        "counts": {
            "tickets": len(records.tickets),
            "changes": len(changes),
            "emails": len(emails),
        },
        "artifacts_borrowed_from": borrowed,
        # Referenced-but-absent ids. Not an error: it is exactly the evidence
        # gap an audit should report, so it is recorded rather than repaired.
        "dangling_references": {
            "changes": dangling_changes,
            "emails": dangling_emails,
        },
        "detail": records.detail,
    }
    _write_json(out / "provenance.json", provenance)
    return provenance


def _dangling(
    tickets: Iterable[dict[str, Any]],
    changes: list[dict[str, Any]],
    emails: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    have_changes = {c.get("change_id") for c in changes}
    have_emails = {e.get("email_id") for e in emails}
    missing_changes: list[str] = []
    missing_emails: list[str] = []
    for ticket in tickets:
        for cid in ticket.get("linked_changes", []):
            if cid not in have_changes and cid not in missing_changes:
                missing_changes.append(cid)
        for eid in ticket.get("linked_emails", []):
            if eid not in have_emails and eid not in missing_emails:
                missing_emails.append(eid)
    return missing_changes, missing_emails


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def build_source(kind: str, **kwargs: Any) -> TicketSource:
    """Resolve a source name to a connector. The one place sources are listed."""
    if kind == "github":
        from audit.mcp_evidence import GitHubIssuesSource  # local: needs the MCP stack

        return GitHubIssuesSource(**kwargs)
    if kind == "jira":
        from audit.jira_evidence import JiraIssuesSource  # local: needs httpx

        return JiraIssuesSource(**kwargs)
    if kind == "linear":
        from audit.linear_evidence import LinearIssuesSource  # local: needs httpx

        return LinearIssuesSource(**kwargs)
    if kind == "eml":
        from audit.email_evidence import EmlFileSource  # local: keeps this module stdlib-only

        return EmlFileSource(kwargs["path"], known_tickets=kwargs.get("known_tickets"))
    if kind == "json":
        return JsonFileSource(kwargs["path"])
    if kind == "csv":
        return CsvFileSource(kwargs["path"])
    if kind in STUBS:
        return STUBS[kind]
    raise ConnectorError(
        f"unknown source {kind!r}; available: github, jira, linear, json, csv, eml"
        + (f", {', '.join(sorted(STUBS))}" if STUBS else "")
    )


def main(argv: list[str] | None = None) -> int:
    """CLI: pull evidence from a source, freeze it, optionally index it.

    Kept separate from `audit/batch.py` on purpose - fetching evidence and
    judging it are different operations with different failure modes, and you
    want to be able to inspect a snapshot before spending LLM calls on it.
    """
    import argparse

    from dotenv import load_dotenv

    load_dotenv()  # the CLI is an entry point; the source classes only read env

    parser = argparse.ArgumentParser(
        description="Pull audit evidence from a source into a frozen corpus snapshot (CR-3).",
    )
    parser.add_argument(
        "--source",
        default="github",
        help="github (MCP) | jira (REST) | linear (GraphQL) | json | csv | eml (exported mail)",
    )
    parser.add_argument("--repo", help="owner/name, for --source github")
    parser.add_argument("--project", help="project key, for --source jira (e.g. AUD)")
    parser.add_argument("--jql", help="raw JQL, for --source jira (overrides --project)")
    parser.add_argument("--team", help="team key, for --source linear (e.g. AUD)")
    parser.add_argument("--path", help="file or directory, for --source json|csv")
    parser.add_argument(
        "--state",
        choices=["OPEN", "CLOSED"],
        default=None,
        help="narrow a github fetch to one state (default: both)",
    )
    parser.add_argument(
        "--label",
        action="append",
        default=None,
        dest="labels",
        help="narrow a github fetch to a label; repeatable",
    )
    parser.add_argument(
        "--out",
        default="assistant/data/live",
        help="snapshot directory to write (default: assistant/data/live)",
    )
    parser.add_argument(
        "--artifacts-from",
        default=None,
        help="corpus dir to take changes.json/emails.json from when the source has none",
    )
    parser.add_argument(
        "--emails-from",
        default=None,
        metavar="PATH",
        help="exported mail to add as evidence: an .eml file, a directory of .eml files, "
        "or an .mbox (Gmail Takeout, Outlook, Apple Mail, Thunderbird all export one of these)",
    )
    parser.add_argument("--limit", type=int, default=None, help="fetch at most N tickets")
    parser.add_argument(
        "--ingest",
        action="store_true",
        help="rebuild the vector store from the snapshot after writing it "
        "(set AUDITREADY_PERSIST_DIR first to keep the synthetic store intact)",
    )
    args = parser.parse_args(argv)

    kwargs: dict[str, Any] = {}
    if args.source == "github":
        if not args.repo or "/" not in args.repo:
            parser.error("--source github needs --repo owner/name")
        owner, repo = args.repo.split("/", 1)
        kwargs = {
            "owner": owner,
            "repo": repo,
            "state": args.state,
            "labels": args.labels,
        }
    elif args.source == "jira":
        if not args.project and not args.jql:
            parser.error("--source jira needs --project KEY or --jql QUERY")
        kwargs = {"project": args.project, "jql": args.jql}
    elif args.source == "linear":
        kwargs = {"team": args.team}
    elif args.source in {"json", "csv", "eml"}:
        if not args.path:
            parser.error(f"--source {args.source} needs --path")
        kwargs = {"path": args.path}

    try:
        source = build_source(args.source, **kwargs)
        print(f"fetching from {source.name} ...", flush=True)
        records = source.fetch(limit=args.limit)
    except (ConnectorError, NotImplementedError) as exc:
        print(f"error: {exc}")
        return 1

    mail = None
    if args.emails_from:
        from audit.email_evidence import EmlFileSource

        try:
            mail = EmlFileSource(
                args.emails_from, known_tickets=[t["ticket_id"] for t in records.tickets]
            ).fetch()
        except ConnectorError as exc:
            print(f"error: {exc}")
            return 1
        print(f"  imported {mail.detail['emails']} email(s) from {args.emails_from}; "
              f"{mail.detail['linked_to_a_ticket']} name a ticket in this snapshot")

    provenance = snapshot(records, args.out, artifacts_from=args.artifacts_from, emails_from=mail)
    counts = provenance["counts"]
    print(
        f"wrote {counts['tickets']} tickets, {counts['changes']} changes, "
        f"{counts['emails']} emails to {args.out}/"
    )
    if provenance["artifacts_borrowed_from"]:
        print(f"  artifacts file-imported from: {provenance['artifacts_borrowed_from']}")
    if provenance.get("emails_imported"):
        imp = provenance["emails_imported"]
        print(f"  emails imported: {imp['emails']} ({imp['ticket_links_added']} ticket link(s) written, "
              f"{imp['duplicates_skipped']} duplicate(s) skipped)")
    dangling = provenance["dangling_references"]
    if dangling["changes"] or dangling["emails"]:
        print(
            f"  referenced but absent (reportable evidence gaps): "
            f"{dangling['changes'] + dangling['emails']}"
        )

    if args.ingest:
        from core.retrieval import DEFAULT_PERSIST_DIR, ingest

        print(f"indexing into {DEFAULT_PERSIST_DIR} ...", flush=True)
        result = ingest(corpus_dir=args.out)
        print(f"  indexed {result} chunks")

    print(f"\nnext: uv run python audit/batch.py --corpus {args.out} --pack <pack>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
