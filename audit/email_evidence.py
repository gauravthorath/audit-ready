"""Email as audit evidence, from exported files (CR-3: "file import (.eml)").

Every mail system exports one of two things: `.eml` files (Outlook, Apple Mail,
Thunderbird, "download original" in Gmail) or an `.mbox` (Gmail Takeout,
Thunderbird). This module reads both into the corpus's email shape — the one
`core/retrieval` indexes and `audit/graph.retrieve` joins to tickets by id —
so client correspondence exported from *any* provider becomes evidence.

*Why files and not the Gmail API or Microsoft Graph.* A mailbox OAuth scope
grants an audit tool every message in the account: unrelated correspondence,
other people's data, everything. The README's ethics section says a live
mailbox connector needs a data-protection review and a scope-minimising design
first, and that is still true. A file export is the consent-shaped version of
the same idea: a human chooses exactly which threads constitute evidence, and
nothing else ever enters the system. Live connectors, when they come, produce
the same records through the same `fetch` — this module is the contract they
will meet.

*Three mapping decisions.*
1. **Ids.** The corpus joins emails by `EML-<digits>`, and `find_linked_ids`
   recovers that pattern from ticket prose. An imported message therefore gets
   `EML-` plus six digits derived from its Message-ID, so the id is stable
   across re-imports and matches the pattern the rest of the engine expects.
2. **Linking.** An email is joined to a ticket when the ticket's id appears in
   its subject or body — "re: TCK-1001", "see KAN-7". When the caller knows
   which tickets exist, only those count, so a stray product code cannot become
   a phantom link. `link_emails_to_tickets` then writes the reverse edge onto
   the ticket, because `graph.retrieve` walks `ticket.linked_emails`, not
   `email.related_tickets`.
3. **Bodies.** text/plain when present, else text/html with the tags stripped.
   Formatting is lost on purpose; the sentence that records an approval is not.
   Attachments are counted and ignored — an attachment is a document, not
   correspondence, and it would need its own connector.

Email bodies are attacker-controlled input like every other record: they pass
through `core.guardrails.sanitize_record` at judge time, the same as a ticket
comment. Nothing here needs to know that; it is the engine's rule.

Req: CR-3.
"""

from __future__ import annotations

import hashlib
import mailbox
import re
from datetime import timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Iterator

from audit.connectors import ConnectorError, EvidenceRecords

# A ticket key looks like TCK-1001, KAN-7, MER-12. Change and email ids share the
# shape, so they are excluded by prefix rather than matched and filtered later.
# The trailing lookahead rejects codes that keep going — INV-2026-0001 is an
# invoice, not ticket INV-2026 — which real mailboxes are full of.
TICKET_REF_RE = re.compile(r"\b(?!CHG-|EML-)[A-Z][A-Z0-9]{1,9}-\d{1,6}\b(?!-\d)")

EMAIL_FIELDS = ("email_id", "from", "to", "date", "subject", "body", "related_tickets")


# ---------------------------------------------------------------------------
# Reading files
# ---------------------------------------------------------------------------


def iter_messages(path: str | Path) -> Iterator[tuple[EmailMessage, str]]:
    """Yield (message, origin) for a .eml file, a directory of them, or an .mbox.

    Directories are walked in sorted order so an import is deterministic.
    Unparseable files raise; a silent skip would drop evidence without a trace,
    which is the one thing an evidence importer must never do.
    """
    p = Path(path)
    if not p.exists():
        raise ConnectorError(f"eml: no such file or directory: {p}")

    if p.is_dir():
        files = sorted(x for x in p.rglob("*") if x.is_file() and x.suffix.lower() in (".eml", ".mbox"))
        if not files:
            raise ConnectorError(f"eml: {p} contains no .eml or .mbox files")
        for f in files:
            yield from iter_messages(f)
        return

    if p.suffix.lower() == ".mbox":
        box = mailbox.mbox(str(p), factory=lambda fh: BytesParser(policy=policy.default).parse(fh))
        try:
            for i, msg in enumerate(box):
                yield msg, f"{p}#{i}"  # type: ignore[misc]
        finally:
            box.close()
        return

    try:
        with p.open("rb") as fh:
            msg = BytesParser(policy=policy.default).parse(fh)
    except Exception as exc:  # noqa: BLE001 — name the file, whatever went wrong
        raise ConnectorError(f"eml: cannot parse {p}: {exc}") from exc
    yield msg, str(p)


# ---------------------------------------------------------------------------
# Mapping one message
# ---------------------------------------------------------------------------


class _TextFromHTML(HTMLParser):
    """Collect visible text; block-level tags become newlines; scripts vanish."""

    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "blockquote", "pre"}
    _SKIP = {"script", "style", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextFromHTML()
    parser.feed(html)
    text = "".join(parser.parts)
    return _tidy(text)


def _tidy(text: str) -> str:
    """Normalise whitespace without flattening paragraphs."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def body_text(msg: EmailMessage) -> tuple[str, str, int]:
    """(body, kind, attachments) — kind is 'plain', 'html' or 'empty'."""
    attachments = 0
    plain: str | None = None
    html: str | None = None
    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or "").lower()
        ctype = part.get_content_type()
        if disposition == "attachment":
            attachments += 1
            continue
        try:
            content = part.get_content()
        except Exception:  # noqa: BLE001 — undecodable part: skip it, count nothing
            continue
        if not isinstance(content, str):
            attachments += 1  # binary inline part — an image, a signature blob
            continue
        if ctype == "text/plain" and plain is None:
            plain = content
        elif ctype == "text/html" and html is None:
            html = content
    if plain and plain.strip():
        return _tidy(plain), "plain", attachments
    if html and html.strip():
        return html_to_text(html), "html", attachments
    return "", "empty", attachments


def email_id_for(msg: EmailMessage, origin: str = "") -> str:
    """`EML-` + six digits, stable for the same message across imports.

    Message-ID is the natural key; when a message lacks one (some exports strip
    it) the from/date/subject triple stands in, with the origin as a last
    resort so two blank messages in one mbox do not collide.
    """
    key = (msg.get("Message-ID") or "").strip()
    if not key:
        key = "|".join([str(msg.get("From", "")), str(msg.get("Date", "")),
                        str(msg.get("Subject", "")), origin])
    digest = hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()
    return f"EML-{int(digest[:12], 16) % 1_000_000:06d}"


def addresses(msg: EmailMessage, header: str) -> list[str]:
    raw = msg.get_all(header, [])
    return [addr for _, addr in getaddresses(raw) if addr]


def iso_date(msg: EmailMessage) -> str:
    """The Date header as ISO 8601 in UTC; empty string when absent or unparseable."""
    raw = msg.get("Date")
    if not raw:
        return ""
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_ticket_refs(*texts: str | None, known: Iterable[str] | None = None) -> list[str]:
    """Ticket ids mentioned in the texts, first-seen order, optionally restricted to `known`."""
    allowed = set(known) if known is not None else None
    out: list[str] = []
    for text in texts:
        for match in TICKET_REF_RE.findall(text or ""):
            if allowed is not None and match not in allowed:
                continue
            if match not in out:
                out.append(match)
    return out


def map_message(
    msg: EmailMessage,
    *,
    origin: str = "",
    known_tickets: Iterable[str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """One message as one corpus-shaped email record, plus its mapping notes."""
    body, kind, attachments = body_text(msg)
    subject = str(msg.get("Subject", "") or "").strip()
    sender = addresses(msg, "From")
    recipients = addresses(msg, "To") + addresses(msg, "Cc")
    related = find_ticket_refs(subject, body, known=known_tickets)

    notes: list[str] = []
    if kind == "html":
        notes.append("body taken from text/html (tags stripped)")
    if kind == "empty":
        notes.append("no readable body")
    if attachments:
        notes.append(f"{attachments} attachment(s) ignored")
    if not related:
        notes.append("no ticket reference found in subject or body")
    if not msg.get("Message-ID"):
        notes.append("no Message-ID; id derived from from/date/subject")

    record = {
        "email_id": email_id_for(msg, origin),
        "from": sender[0] if sender else str(msg.get("From", "") or "unknown"),
        "to": recipients,
        "date": iso_date(msg),
        "subject": subject,
        "body": body,
        "related_tickets": related,
        # Extra keys survive: this is what lets a reviewer find the original.
        "source_message_id": (msg.get("Message-ID") or "").strip() or None,
        "source_file": origin or None,
    }
    return record, notes


def validate_email(record: dict[str, Any], *, where: str = "email") -> None:
    """The email contract, as `core/retrieval._email_chunk` and `load_artifacts` read it."""
    missing = sorted(set(EMAIL_FIELDS) - set(record))
    if missing:
        raise ConnectorError(f"{where}: missing field(s) {missing}")
    if not re.fullmatch(r"EML-\d+", str(record["email_id"])):
        raise ConnectorError(f"{where}: email_id {record['email_id']!r} must match EML-<digits>")
    if not isinstance(record["to"], list) or not isinstance(record["related_tickets"], list):
        raise ConnectorError(f"{where}: to and related_tickets must be lists")
    for key in ("from", "date", "subject", "body"):
        if not isinstance(record[key], str):
            raise ConnectorError(f"{where}: {key} must be a string")


def link_emails_to_tickets(tickets: list[dict[str, Any]], emails: list[dict[str, Any]]) -> int:
    """Write the reverse edge: every email that names a ticket appears in that ticket's `linked_emails`.

    `graph.retrieve` joins from the ticket side, so without this an imported
    thread is indexed but never handed to the judge as a linked record.
    Idempotent; preserves existing order. Returns how many links were added.
    """
    by_id = {t.get("ticket_id"): t for t in tickets}
    added = 0
    for e in emails:
        for tid in e.get("related_tickets") or []:
            t = by_id.get(tid)
            if t is None:
                continue
            linked = t.setdefault("linked_emails", [])
            if e["email_id"] not in linked:
                linked.append(e["email_id"])
                added += 1
    return added


# ---------------------------------------------------------------------------
# The source
# ---------------------------------------------------------------------------


class EmlFileSource:
    """Exported mail — .eml files, a directory of them, or an .mbox — as evidence records.

    Produces emails only; tickets come from a ticket connector and the two are
    merged by `connectors.snapshot`. `known_tickets` restricts linking to ids
    that exist, so a product code in a signature cannot become a phantom join.
    """

    name = "eml"

    def __init__(self, path: str | Path, *, known_tickets: Iterable[str] | None = None) -> None:
        self.path = Path(path)
        self.known_tickets = set(known_tickets) if known_tickets is not None else None

    def fetch(self, limit: int | None = None) -> EvidenceRecords:
        emails: list[dict[str, Any]] = []
        notes: dict[str, list[str]] = {}
        seen: set[str] = set()
        kinds = {"plain": 0, "html": 0, "empty": 0}
        attachments = 0
        for msg, origin in iter_messages(self.path):
            if limit is not None and len(emails) >= limit:
                break
            record, why = map_message(msg, origin=origin, known_tickets=self.known_tickets)
            validate_email(record, where=f"eml: {origin}")
            if record["email_id"] in seen:
                # Same Message-ID exported twice (a thread saved from two
                # folders). Keep the first; say so.
                notes.setdefault(record["email_id"], []).append(f"duplicate skipped: {origin}")
                continue
            seen.add(record["email_id"])
            emails.append(record)
            for n in why:
                if n.startswith("body taken from text/html"):
                    kinds["html"] += 1
                elif n == "no readable body":
                    kinds["empty"] += 1
                elif n.endswith("attachment(s) ignored"):
                    attachments += int(n.split()[0])
            if not any(n.startswith("body taken") or n == "no readable body" for n in why):
                kinds["plain"] += 1
            if why:
                notes[record["email_id"]] = why
        linked = sum(1 for e in emails if e["related_tickets"])
        return EvidenceRecords(
            tickets=[],
            emails=emails,
            source=f"eml:{self.path}",
            detail={
                "transport": "file",
                "path": str(self.path),
                "emails": len(emails),
                "linked_to_a_ticket": linked,
                "bodies": kinds,
                "attachments_ignored": attachments,
                "mapping_notes": notes,
                "consent": "only the exported threads entered the system; nothing was fetched from a mailbox",
            },
        )
