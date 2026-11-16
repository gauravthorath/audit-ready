"""Linear issues as audit evidence (CR-3).

The third live connector, and the one that makes the interface claim testable:
GitHub arrives over MCP, Jira over REST, Linear over GraphQL. Three transports,
one `TicketSource` protocol, one `validate_ticket` gate, one snapshot path. If
the abstraction were leaking, adding a third shape would have shown it.

*Why the GraphQL API and not Linear's MCP server.* Same reason as Jira: the MCP
server is OAuth-only, so it needs a browser and a human. A personal API key
works headlessly, which is what a connector needs to be smoke-tested and run
unattended.

*Why one query instead of N+1.* Linear returns an issue's comments inline as a
connection, so a page of issues and their comments is one round trip. That is
the opposite of the Jira connector, which pages comments per issue because
Jira's search truncates them without saying so. The difference is the source's,
not ours - which is the whole point of putting the transport behind `fetch`.

*What Linear cannot supply.* No approval record and no resolution field. An
issue is done when its workflow state has type `completed`, and the narrative
of *why* lives in comments. `signoff` is None and `resolution` states what the
source actually holds, with the mapping notes saying so. Priority is a 0-4
integer where 0 means "no priority set" - that is a real absence, recorded as a
guess rather than silently read as medium.

Env:
    AUDITREADY_LINEAR_TOKEN   a personal API key from Linear
                              (Settings -> Security & access -> Personal API keys)

Req: CR-3.
"""

from __future__ import annotations

import os
from typing import Any, Iterable

import httpx

from audit.connectors import (
    ConnectorError,
    EvidenceRecords,
    fill_ticket,
    find_linked_ids,
)

ENDPOINT = "https://api.linear.app/graphql"
TIMEOUT = 30.0

# Linear has no issue-type field; teams express it with labels. Same table as
# the GitHub connector uses, for the same reason.
TYPE_BY_LABEL: dict[str, str] = {
    "bug": "incident",
    "incident": "incident",
    "outage": "incident",
    "regression": "incident",
    "feature": "change_request",
    "enhancement": "change_request",
    "improvement": "change_request",
    "change": "change_request",
    "change request": "change_request",
    "complaint": "complaint",
    "support": "service_request",
    "request": "service_request",
    "task": "service_request",
    "chore": "service_request",
}
DEFAULT_TYPE = "service_request"

# Linear priority is an integer: 0 no priority, 1 urgent, 2 high, 3 medium, 4 low.
PRIORITY_BY_LEVEL: dict[int, str] = {1: "high", 2: "high", 3: "medium", 4: "low"}
DEFAULT_PRIORITY = "medium"

# One page of issues with everything a ticket needs, comments included.
ISSUES_QUERY = """
query AuditEvidence($first: Int!, $after: String, $filter: IssueFilter) {
  issues(first: $first, after: $after, filter: $filter, orderBy: createdAt) {
    pageInfo { hasNextPage endCursor }
    nodes {
      identifier
      title
      description
      priority
      priorityLabel
      createdAt
      completedAt
      canceledAt
      url
      state { name type }
      creator { name displayName email }
      assignee { name displayName email }
      labels(first: 20) { nodes { name } }
      comments(first: 100) {
        nodes {
          createdAt
          body
          user { name displayName email }
        }
      }
    }
  }
}
""".strip()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def linear_token(explicit: str | None = None) -> str:
    """The personal API key. Sent raw, with no `Bearer` prefix.

    Linear is unusual here: `Authorization: <key>`, not `Bearer <key>`. Getting
    it wrong returns a 400 with an authentication message, which reads like a
    bad key rather than a bad header, so it is worth stating once.
    """
    token = (
        explicit
        or os.getenv("AUDITREADY_LINEAR_TOKEN")
        or os.getenv("LINEAR_API_KEY")
        or ""
    ).strip()
    if not token:
        raise ConnectorError(
            "no Linear token: set AUDITREADY_LINEAR_TOKEN (Linear -> Settings -> "
            "Security & access -> Personal API keys)"
        )
    return token


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def actor_name(value: Any) -> str:
    """A Linear user as a display string, whichever fields the query returned."""
    if isinstance(value, dict):
        for key in ("displayName", "name", "email"):
            got = value.get(key)
            if isinstance(got, str) and got.strip():
                return got.strip()
    elif isinstance(value, str):
        return value.strip()
    return ""


def label_names(raw: Any) -> list[str]:
    """The `labels { nodes { name } }` connection as a flat list of names."""
    nodes = (raw or {}).get("nodes") if isinstance(raw, dict) else raw
    out: list[str] = []
    for node in nodes or []:
        name = node.get("name") if isinstance(node, dict) else node
        if isinstance(name, str) and name.strip():
            out.append(name.strip())
    return out


def map_type(labels: Iterable[str]) -> tuple[str, bool]:
    """Ticket type from labels; the flag says whether it was a guess."""
    for label in labels:
        got = TYPE_BY_LABEL.get(str(label).lower().replace("_", " "))
        if got:
            return got, False
    return DEFAULT_TYPE, True


def map_priority(level: Any) -> tuple[str, bool]:
    """Priority from Linear's 0-4 integer; 0 ("no priority") is a real absence."""
    try:
        value = int(level)
    except (TypeError, ValueError):
        return DEFAULT_PRIORITY, True
    if value in PRIORITY_BY_LEVEL:
        return PRIORITY_BY_LEVEL[value], False
    return DEFAULT_PRIORITY, True


def map_status(state: Any) -> str:
    """The workflow state name, lowercased. Linear team states are user-defined."""
    if isinstance(state, dict):
        name = state.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip().lower()
    return "open"


def map_comments(raw: Any) -> list[dict[str, str]]:
    """Linear comments in corpus comment shape: author, timestamp, body."""
    nodes = (raw or {}).get("nodes") if isinstance(raw, dict) else raw
    out: list[dict[str, str]] = []
    for comment in nodes or []:
        if not isinstance(comment, dict):
            continue
        out.append(
            {
                "author": actor_name(comment.get("user")) or "unknown",
                "timestamp": str(comment.get("createdAt", "") or ""),
                # Linear serves markdown, which is already prose.
                "body": str(comment.get("body") or "").strip(),
            }
        )
    return out


def map_resolution(issue: dict[str, Any]) -> str:
    """Linear's closure facts as a sentence, or an explicit statement of absence.

    Linear has no resolution field. What it has is a workflow state with a type,
    and a completion timestamp. Saying that plainly is more useful to a judge
    than synthesising a resolution out of the last comment.
    """
    state = issue.get("state") if isinstance(issue.get("state"), dict) else {}
    name = str(state.get("name") or "unknown")
    kind = str(state.get("type") or "")
    completed = issue.get("completedAt")
    canceled = issue.get("canceledAt")

    if canceled:
        return (
            f"Cancelled on {canceled} in state {name}. Linear has no resolution "
            "field; this is the closure record the source holds."
        )
    if kind == "completed" or completed:
        when = str(completed or "unknown date")
        return (
            f"Completed on {when} in state {name}. Linear has no resolution field; "
            "this is the closure record the source holds."
        )
    return f"Not resolved: the issue sits in state {name} (type {kind or 'unknown'}) in Linear."


def map_issue(issue: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """One Linear issue as one contract-shaped ticket, plus its mapping notes."""
    identifier = str(issue.get("identifier") or "").strip()
    if not identifier:
        raise ConnectorError(f"linear: issue has no identifier: {str(issue)[:200]}")

    labels = label_names(issue.get("labels"))
    ticket_type, guessed_type = map_type(labels)
    priority, guessed_priority = map_priority(issue.get("priority"))
    comments = map_comments(issue.get("comments"))
    description = str(issue.get("description") or "").strip()
    changes, emails = find_linked_ids(description, *(c["body"] for c in comments))
    closed = issue.get("completedAt") or issue.get("canceledAt") or None

    notes: list[str] = []
    if guessed_type:
        notes.append(f"type defaulted to {DEFAULT_TYPE} (labels: {labels or 'none'})")
    if guessed_priority:
        label = issue.get("priorityLabel") or issue.get("priority")
        notes.append(
            f"priority defaulted to {DEFAULT_PRIORITY} (Linear priority: {label!r} "
            "- 0 means no priority set)"
        )

    ticket = fill_ticket(
        {
            "ticket_id": identifier,
            "title": str(issue.get("title") or ""),
            "type": ticket_type,
            "priority": priority,
            "status": map_status(issue.get("state")),
            "created": str(issue.get("createdAt") or ""),
            "closed": closed,
            "reporter": actor_name(issue.get("creator")) or "unknown",
            "assignee": actor_name(issue.get("assignee")) or None,
            "description": description,
            "comments": comments,
            "linked_changes": changes,
            "linked_emails": emails,
            "resolution": map_resolution(issue),
            # Linear has no approval record; see the module docstring.
            "signoff": None,
            "source_url": str(issue.get("url") or ""),
            "source_labels": labels,
        }
    )
    return ticket, notes


# ---------------------------------------------------------------------------
# The connector
# ---------------------------------------------------------------------------


class LinearIssuesSource:
    """Read issues from a Linear workspace over the GraphQL API.

    `team` narrows to one team by key (the prefix on issue identifiers, e.g.
    AUD in AUD-12). Ordering is pinned to createdAt so two fetches of an
    unchanged workspace produce the same snapshot.
    """

    name = "linear"

    def __init__(
        self,
        team: str | None = None,
        *,
        token: str | None = None,
        page_size: int = 50,
        endpoint: str = ENDPOINT,
        client: httpx.Client | None = None,
    ) -> None:
        self.team = team
        self.endpoint = endpoint
        self.page_size = max(1, min(int(page_size), 100))
        self._client = client
        self._token = linear_token(token) if client is None else None

    def _open(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        return httpx.Client(
            headers={
                "Authorization": self._token or "",
                "Content-Type": "application/json",
                "User-Agent": "auditready/0.1",
            },
            timeout=TIMEOUT,
        )

    def _query(self, client: httpx.Client, variables: dict[str, Any]) -> dict[str, Any]:
        """One GraphQL round trip, with GraphQL errors raised as connector errors.

        GraphQL answers 200 with an `errors` array, so a status check alone
        would let a failed query through as an empty fetch - which in an audit
        tool reads as "no evidence found" rather than "the query broke".
        """
        try:
            response = client.post(
                self.endpoint, json={"query": ISSUES_QUERY, "variables": variables}
            )
        except httpx.HTTPError as exc:
            raise ConnectorError(f"linear: request failed: {exc}") from exc

        if response.status_code in (400, 401, 403):
            raise ConnectorError(
                f"linear: {response.status_code} - check AUDITREADY_LINEAR_TOKEN is a "
                f"personal API key sent without a 'Bearer' prefix: {response.text[:200]}"
            )
        if response.status_code >= 400:
            raise ConnectorError(f"linear: {response.status_code}: {response.text[:300]}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise ConnectorError("linear: response was not JSON") from exc
        if not isinstance(payload, dict):
            raise ConnectorError("linear: response was not a JSON object")

        errors = payload.get("errors")
        if errors:
            first = errors[0] if isinstance(errors, list) and errors else errors
            message = first.get("message") if isinstance(first, dict) else str(first)
            raise ConnectorError(f"linear: GraphQL error: {message}")

        data = payload.get("data")
        if not isinstance(data, dict):
            raise ConnectorError("linear: response had no data object")
        return data

    def fetch(self, limit: int | None = None) -> EvidenceRecords:
        """Issues for the workspace (or one team), in corpus shape.

        `changes` and `emails` come back empty for the same reason as every
        other ticket connector: a ticket system holds tickets. The resulting
        evidence gap is a correct finding, not a failure.
        """
        client = self._open()
        owns_client = self._client is None
        filter_: dict[str, Any] | None = (
            {"team": {"key": {"eq": self.team}}} if self.team else None
        )
        tickets: list[dict[str, Any]] = []
        notes: dict[str, list[str]] = {}
        pages = 0
        cursor: str | None = None

        try:
            while True:
                variables: dict[str, Any] = {"first": self.page_size, "after": cursor}
                if filter_ is not None:
                    variables["filter"] = filter_
                data = self._query(client, variables)
                pages += 1

                connection = data.get("issues")
                if not isinstance(connection, dict):
                    raise ConnectorError("linear: query returned no issues connection")
                for node in connection.get("nodes") or []:
                    if limit is not None and len(tickets) >= limit:
                        break
                    if not isinstance(node, dict):
                        continue
                    ticket, issue_notes = map_issue(node)
                    tickets.append(ticket)
                    if issue_notes:
                        notes[ticket["ticket_id"]] = issue_notes

                info = connection.get("pageInfo") or {}
                cursor = info.get("endCursor")
                if not info.get("hasNextPage") or not cursor:
                    break
                if limit is not None and len(tickets) >= limit:
                    break
        finally:
            if owns_client:
                client.close()

        return EvidenceRecords(
            tickets=tickets,
            source=f"linear:{self.team or 'workspace'}",
            detail={
                "transport": "graphql",
                "api": self.endpoint,
                "team": self.team,
                "pages": pages,
                "tickets": len(tickets),
                "mapping_notes": notes,
                "not_supplied": {
                    "signoff": "Linear has no approval record",
                    "resolution": "Linear has no resolution field; state type is used",
                    "changes": "a ticket system holds no change-approval records",
                    "emails": "a ticket system holds no client correspondence",
                },
            },
        )
