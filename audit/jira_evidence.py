"""Jira Cloud issues as audit evidence (CR-3).

The second live connector. `audit/mcp_evidence.py` proved the interface over
MCP; this one proves the interface is not *about* MCP. Same `TicketSource`
protocol, same `validate_ticket` gate, same snapshot-then-ingest path — a
different transport underneath.

*Why the REST API and not Atlassian's MCP server.* The plan said "Linear or Jira
via their official MCP servers", and that was the right first guess. It does not
survive contact: Atlassian's remote MCP server is OAuth-only, which means a
browser consent flow against a provisioned Cloud site. A connector that cannot
run headless cannot run in CI, cannot run in a cron, and cannot be smoke-tested
without a human at a keyboard. The Cloud REST API takes an API token over Basic
auth, so it can. The MCP claim in the architecture story is carried by GitHub,
which genuinely is MCP; claiming it here too would be false.

*Why /search/jql and not /search.* `POST /rest/api/3/search` is deprecated and
removed. The replacement pages with an opaque `nextPageToken` and returns no
total, which is why `fetch` counts what it received rather than trusting a
count it was never given.

*Why comments are a second request per issue.* The JQL search will return a
`comment` field, but it truncates: on a long thread you get the first page and
no marker saying so. An audit record that silently drops the comment carrying
the approval is worse than a slower fetch, so each issue's comments are read
from `/issue/{key}/comment`, which pages honestly.

*What Jira cannot supply.* There is no sign-off record in base Jira software
projects — approvals are a Jira Service Management workflow feature. `signoff`
is therefore None and the mapping notes say so, exactly as the GitHub connector
does. Inventing an approver would be the one unforgivable bug in an audit tool.

Env:
    AUDITREADY_JIRA_SITE   mycompany.atlassian.net (or the full https:// URL)
    AUDITREADY_JIRA_EMAIL  the Atlassian account email the token belongs to
    AUDITREADY_JIRA_TOKEN  from https://id.atlassian.com/manage/api-tokens

Req: CR-3.
"""

from __future__ import annotations

import base64
import os
from typing import Any, Iterable
from urllib.parse import urlparse

import httpx

from audit.connectors import (
    ConnectorError,
    EvidenceRecords,
    fill_ticket,
    find_linked_ids,
)

API = "/rest/api/3"
TIMEOUT = 30.0

# Jira issue types are per-project and user-renameable, so this maps the
# defaults every Cloud site ships with and admits a guess for anything else.
TYPE_BY_ISSUETYPE: dict[str, str] = {
    "bug": "incident",
    "incident": "incident",
    "outage": "incident",
    "problem": "incident",
    "task": "service_request",
    "sub-task": "service_request",
    "subtask": "service_request",
    "story": "service_request",
    "service request": "service_request",
    "[system] service request": "service_request",
    "support": "service_request",
    "change": "change_request",
    "change request": "change_request",
    "[system] change": "change_request",
    "new feature": "change_request",
    "improvement": "change_request",
    "epic": "change_request",
    "complaint": "complaint",
}
DEFAULT_TYPE = "service_request"

# The five default Jira priorities, plus the ITSM names.
PRIORITY_BY_NAME: dict[str, str] = {
    "highest": "high",
    "critical": "high",
    "urgent": "high",
    "high": "high",
    "medium": "medium",
    "normal": "medium",
    "moderate": "medium",
    "low": "low",
    "lowest": "low",
    "minor": "low",
}
DEFAULT_PRIORITY = "medium"

# Fields worth asking for by name. /search/jql returns nothing you did not
# request, and a wildcard fetch on a real site is slow and mostly noise.
SEARCH_FIELDS = (
    "summary,issuetype,priority,status,resolution,resolutiondate,created,"
    "updated,reporter,assignee,description,labels,parent"
)


# ---------------------------------------------------------------------------
# Config and auth
# ---------------------------------------------------------------------------


def jira_site(explicit: str | None = None) -> str:
    """The Cloud site as a bare https origin, however it was written down.

    People paste `mycompany.atlassian.net`, `https://mycompany.atlassian.net`,
    and `https://mycompany.atlassian.net/jira/software/...` interchangeably.
    All three mean the same site, so all three are accepted.
    """
    raw = (explicit or os.getenv("AUDITREADY_JIRA_SITE") or "").strip()
    if not raw:
        raise ConnectorError(
            "no Jira site: set AUDITREADY_JIRA_SITE to your Cloud host, "
            "e.g. mycompany.atlassian.net"
        )
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    if not parsed.netloc:
        raise ConnectorError(f"AUDITREADY_JIRA_SITE is not a host or URL: {raw!r}")
    return f"https://{parsed.netloc}"


def jira_auth(email: str | None = None, token: str | None = None) -> str:
    """A Basic auth header value from email + API token.

    Atlassian deprecated password auth; an API token is the supported way for a
    script to authenticate, and it is scoped to the account that minted it.
    """
    email = (email or os.getenv("AUDITREADY_JIRA_EMAIL") or "").strip()
    token = (token or os.getenv("AUDITREADY_JIRA_TOKEN") or "").strip()
    if not email or not token:
        raise ConnectorError(
            "no Jira credentials: set AUDITREADY_JIRA_EMAIL and "
            "AUDITREADY_JIRA_TOKEN (mint a token at "
            "https://id.atlassian.com/manage/api-tokens)"
        )
    raw = f"{email}:{token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


# ---------------------------------------------------------------------------
# Atlassian Document Format
# ---------------------------------------------------------------------------

_ADF_BLOCKS = frozenset(
    {
        "paragraph",
        "heading",
        "blockquote",
        "codeBlock",
        "listItem",
        "panel",
        "rule",
        "tableRow",
        "mediaSingle",
    }
)


def flatten_adf(node: Any) -> str:
    """Atlassian Document Format down to plain prose.

    The v3 API serves `description` and comment bodies as an ADF document — a
    nested node tree, not a string. The judge reads prose and cites fields, so
    the tree has to become text at the boundary rather than leaking JSON into a
    prompt. Anything already a string passes through, because v2 responses and
    hand-written fixtures are both plain.

    Structure is flattened, not preserved: paragraphs and list items become
    newlines, mentions become @name, links keep their URL. Losing bold is fine.
    Losing the sentence that records an approval would not be.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node.strip()
    if isinstance(node, list):
        return "\n".join(p for p in (flatten_adf(n) for n in node) if p)
    if not isinstance(node, dict):
        return str(node)

    kind = node.get("type")
    if kind == "text":
        text = str(node.get("text", ""))
        for mark in node.get("marks") or []:
            if isinstance(mark, dict) and mark.get("type") == "link":
                href = (mark.get("attrs") or {}).get("href")
                if href and href != text:
                    text = f"{text} ({href})"
        return text
    if kind == "hardBreak":
        return "\n"
    if kind == "mention":
        attrs = node.get("attrs") or {}
        return "@" + str(attrs.get("text") or attrs.get("id") or "unknown").lstrip("@")
    if kind == "emoji":
        return str((node.get("attrs") or {}).get("shortName") or "")
    if kind in ("inlineCard", "blockCard", "embedCard"):
        return str((node.get("attrs") or {}).get("url") or "")

    inner = node.get("content")
    if kind == "listItem" or kind in _ADF_BLOCKS:
        # A block's children join inline; the block itself ends a line.
        parts = [flatten_adf(c) for c in inner or []]
        joined = "".join(p for p in parts if p) if kind == "paragraph" else \
            "\n".join(p for p in parts if p)
        return joined
    if inner is not None:
        return "\n".join(p for p in (flatten_adf(c) for c in inner) if p)
    return ""


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def name_of(value: Any) -> str:
    """A Jira nested object as its display name, whatever shape it arrives in."""
    if isinstance(value, dict):
        for key in ("displayName", "name", "value", "emailAddress"):
            got = value.get(key)
            if isinstance(got, str) and got.strip():
                return got.strip()
        return ""
    if isinstance(value, str):
        return value.strip()
    return ""


def map_type(issuetype: Any, labels: Iterable[str]) -> tuple[str, bool]:
    """Ticket type from the Jira issue type, falling back to labels.

    The flag says whether it was a guess, so `map_issue` can record it.
    """
    name = name_of(issuetype).lower()
    if name in TYPE_BY_ISSUETYPE:
        return TYPE_BY_ISSUETYPE[name], False
    for label in labels:
        got = TYPE_BY_ISSUETYPE.get(str(label).lower().replace("_", " "))
        if got:
            return got, False
    return DEFAULT_TYPE, True


def map_priority(priority: Any) -> tuple[str, bool]:
    """Priority from the Jira priority name; the flag says whether it was a guess."""
    name = name_of(priority).lower()
    if name in PRIORITY_BY_NAME:
        return PRIORITY_BY_NAME[name], False
    return DEFAULT_PRIORITY, True


def map_comments(raw: Any) -> list[dict[str, str]]:
    """Jira comments in corpus comment shape: author, timestamp, body."""
    out: list[dict[str, str]] = []
    for comment in raw or []:
        if not isinstance(comment, dict):
            continue
        out.append(
            {
                "author": name_of(comment.get("author")) or "unknown",
                "timestamp": str(comment.get("created", "") or ""),
                "body": flatten_adf(comment.get("body")),
            }
        )
    return out


def map_resolution(fields: dict[str, Any]) -> str:
    """Jira's closure facts as a sentence, or an explicit statement of absence.

    Both branches are true statements about Jira. Whether they earn a verdict is
    the auditor's business, not this function's.
    """
    resolution = name_of(fields.get("resolution"))
    when = str(fields.get("resolutiondate") or "") or "unknown date"
    status = name_of(fields.get("status"))
    if not resolution:
        return (
            f"Not resolved: the issue sits in status {status or 'unknown'} with no "
            "resolution set in Jira."
        )
    return (
        f"Resolved as {resolution} on {when} (status {status or 'unknown'}). Jira "
        "records the resolution name, not the resolving narrative."
    )


def map_issue(
    issue: dict[str, Any],
    comments: list[dict[str, Any]],
    *,
    site: str,
) -> tuple[dict[str, Any], list[str]]:
    """One Jira issue as one contract-shaped ticket, plus its mapping notes."""
    key = str(issue.get("key") or "").strip()
    if not key:
        raise ConnectorError(f"jira: issue has no key: {str(issue)[:200]}")
    fields = issue.get("fields") or {}
    if not isinstance(fields, dict):
        raise ConnectorError(f"{key}: fields is not an object")

    labels = [str(x) for x in (fields.get("labels") or []) if x]
    ticket_type, guessed_type = map_type(fields.get("issuetype"), labels)
    priority, guessed_priority = map_priority(fields.get("priority"))
    mapped_comments = map_comments(comments)
    description = flatten_adf(fields.get("description"))
    changes, emails = find_linked_ids(description, *(c["body"] for c in mapped_comments))

    notes: list[str] = []
    if guessed_type:
        notes.append(
            f"type defaulted to {DEFAULT_TYPE} "
            f"(issuetype: {name_of(fields.get('issuetype')) or 'none'})"
        )
    if guessed_priority:
        notes.append(
            f"priority defaulted to {DEFAULT_PRIORITY} "
            f"(priority: {name_of(fields.get('priority')) or 'none'})"
        )

    ticket = fill_ticket(
        {
            "ticket_id": key,
            "title": str(fields.get("summary") or ""),
            "type": ticket_type,
            "priority": priority,
            "status": (name_of(fields.get("status")) or "open").lower(),
            "created": str(fields.get("created") or ""),
            "closed": fields.get("resolutiondate") or None,
            "reporter": name_of(fields.get("reporter")) or "unknown",
            "assignee": name_of(fields.get("assignee")) or None,
            "description": description,
            "comments": mapped_comments,
            "linked_changes": changes,
            "linked_emails": emails,
            "resolution": map_resolution(fields),
            # Base Jira has no approval record; see the module docstring.
            "signoff": None,
            # Extra keys survive fill_ticket, and this is what lets a reviewer
            # open the evidence a verdict cites.
            "source_url": f"{site}/browse/{key}",
            "source_labels": labels,
        }
    )
    return ticket, notes


# ---------------------------------------------------------------------------
# The connector
# ---------------------------------------------------------------------------


class JiraIssuesSource:
    """Read issues from one Jira Cloud project (or any JQL) over the REST API.

    `jql` wins if given, otherwise a project key builds the obvious query. The
    ordering is pinned to `created ASC` so two fetches of an unchanged project
    return the same snapshot — an audit that reshuffles its evidence between
    runs is hard to trust and harder to diff.
    """

    name = "jira"

    def __init__(
        self,
        project: str | None = None,
        *,
        jql: str | None = None,
        site: str | None = None,
        email: str | None = None,
        token: str | None = None,
        page_size: int = 50,
        max_comments: int = 200,
        client: httpx.Client | None = None,
    ) -> None:
        if not project and not jql:
            raise ConnectorError("jira: pass a project key or a jql query")
        self.project = project
        self.jql = jql or f'project = "{project}" ORDER BY created ASC'
        self.site = jira_site(site)
        self.page_size = max(1, min(int(page_size), 100))
        self.max_comments = max(1, int(max_comments))
        self._client = client
        self._auth = jira_auth(email, token) if client is None else None

    # -- HTTP -------------------------------------------------------------

    def _open(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        return httpx.Client(
            base_url=self.site,
            headers={
                "Authorization": self._auth or "",
                "Accept": "application/json",
                "User-Agent": "auditready/0.1",
            },
            timeout=TIMEOUT,
        )

    def _get(self, client: httpx.Client, path: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"jira: {path} failed: {exc}") from exc
        if response.status_code == 401:
            raise ConnectorError(
                "jira: 401 unauthorized — check AUDITREADY_JIRA_EMAIL matches the "
                "account that minted AUDITREADY_JIRA_TOKEN"
            )
        if response.status_code == 403:
            raise ConnectorError("jira: 403 forbidden — the account cannot browse this project")
        if response.status_code == 404:
            raise ConnectorError(f"jira: 404 for {path} — check the site and project key")
        if response.status_code >= 400:
            raise ConnectorError(
                f"jira: {path} returned {response.status_code}: {response.text[:300]}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ConnectorError(f"jira: {path} returned non-JSON") from exc
        if not isinstance(payload, dict):
            raise ConnectorError(f"jira: {path} returned {type(payload).__name__}, expected object")
        return payload

    def _comments(self, client: httpx.Client, key: str) -> list[dict[str, Any]]:
        """Every comment on one issue, paged until the source says it is done."""
        out: list[dict[str, Any]] = []
        start = 0
        while len(out) < self.max_comments:
            page = self._get(
                client,
                f"{API}/issue/{key}/comment",
                {"startAt": start, "maxResults": 100, "orderBy": "created"},
            )
            batch = page.get("comments") or []
            if not isinstance(batch, list) or not batch:
                break
            out.extend(x for x in batch if isinstance(x, dict))
            total = page.get("total")
            start += len(batch)
            if isinstance(total, int) and start >= total:
                break
        return out[: self.max_comments]

    # -- TicketSource -----------------------------------------------------

    def fetch(self, limit: int | None = None) -> EvidenceRecords:
        """Issues for the query, in corpus shape, with mapping notes recorded.

        `changes` and `emails` come back empty: Jira knows about issues. Change
        approvals and client mail live elsewhere, which is a different connector
        and, until one is pointed at them, a reportable evidence gap.
        """
        client = self._open()
        owns_client = self._client is None
        tickets: list[dict[str, Any]] = []
        notes: dict[str, list[str]] = {}
        pages = 0
        token: str | None = None

        try:
            while True:
                params: dict[str, Any] = {
                    "jql": self.jql,
                    "fields": SEARCH_FIELDS,
                    "maxResults": self.page_size,
                }
                if token:
                    params["nextPageToken"] = token
                # /search/jql replaced /search, which is removed. It pages with an
                # opaque token and reports no total, so we count what we received.
                page = self._get(client, f"{API}/search/jql", params)
                pages += 1
                issues = page.get("issues")
                if not isinstance(issues, list):
                    raise ConnectorError("jira: /search/jql returned no issues array")

                for issue in issues:
                    if limit is not None and len(tickets) >= limit:
                        break
                    if not isinstance(issue, dict):
                        continue
                    key = str(issue.get("key") or "")
                    ticket, issue_notes = map_issue(
                        issue, self._comments(client, key), site=self.site
                    )
                    tickets.append(ticket)
                    if issue_notes:
                        notes[ticket["ticket_id"]] = issue_notes

                token = page.get("nextPageToken")
                done = page.get("isLast") is True or not token
                if done or (limit is not None and len(tickets) >= limit):
                    break
        finally:
            if owns_client:
                client.close()

        return EvidenceRecords(
            tickets=tickets,
            source=f"jira:{self.project or self.jql}",
            detail={
                "transport": "rest",
                "api": f"{self.site}{API}/search/jql",
                "jql": self.jql,
                "pages": pages,
                "tickets": len(tickets),
                "mapping_notes": notes,
                "not_supplied": {
                    "signoff": "base Jira has no approval record (JSM workflow feature)",
                    "changes": "a ticket system holds no change-approval records",
                    "emails": "a ticket system holds no client correspondence",
                },
            },
        )
