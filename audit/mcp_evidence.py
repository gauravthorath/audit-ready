"""GitHub Issues over MCP — the one live evidence connector (CR-3).

Why MCP rather than the REST API. The REST call would be shorter. The point of
CR-3 is not to fetch JSON, it is to show the engine reading a real system
through the same protocol the rest of the agent stack speaks, so that the next
connector is a URL and a tool name rather than a new HTTP client. What this
module owns is therefore narrow: an MCP session, pagination, and the mapping
from what GitHub stores to the record contract in `audit/connectors.py`.

Why the mapping is the interesting part. GitHub Issues is not an ISO ticket
system: it has no priority field, no ticket type, no resolution statement and no
approval record. Three honest choices follow, and each one is recorded in the
snapshot's provenance rather than hidden:

  * type and priority are *inferred from labels*, and every ticket that fell
    back to a default says so in `detail["inferred"]`. An auditor reading the
    report can see which classifications the source stated and which this
    connector guessed.
  * `resolution` carries GitHub's own closure facts (state_reason, who closed
    it, when) and says plainly that the source has no resolution narrative. It
    is derived, never invented.
  * `signoff` stays None. Closing an issue is not an approval, and pretending
    otherwise would manufacture the exact evidence an audit exists to check.
    Controls that require sign-off will report a gap against this source — that
    finding is true, and it is about the source system, not about the engine.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import subprocess
from typing import Any

from audit.connectors import (
    ConnectorError,
    EvidenceRecords,
    fill_ticket,
    find_linked_ids,
)

GITHUB_MCP_URL = os.getenv("AUDITREADY_GITHUB_MCP_URL", "https://api.githubcopilot.com/mcp/")

PAGE_SIZE = 100
# The per-issue detail and comment reads are two calls per ticket. Four at a
# time is the same bound `coverage_report` and the batch runner use: enough to
# hide the round trips, low enough not to trip secondary rate limits.
MAX_CONCURRENT_READS = 4

# Label vocabularies. Deliberately data, not code: a team whose labels differ
# edits these two dicts, and nothing else in the connector changes.
TYPE_BY_LABEL: dict[str, str] = {
    "bug": "incident",
    "incident": "incident",
    "outage": "incident",
    "regression": "incident",
    "enhancement": "change_request",
    "feature": "change_request",
    "change": "change_request",
    "change_request": "change_request",
    "question": "service_request",
    "support": "service_request",
    "help wanted": "service_request",
    "documentation": "service_request",
    "complaint": "complaint",
}
DEFAULT_TYPE = "service_request"

PRIORITY_BY_LABEL: dict[str, str] = {
    "p0": "high",
    "p1": "high",
    "critical": "high",
    "urgent": "high",
    "high": "high",
    "p2": "medium",
    "medium": "medium",
    "p3": "low",
    "low": "low",
    "minor": "low",
}
DEFAULT_PRIORITY = "medium"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def github_token(explicit: str | None = None) -> str:
    """The token, from the argument, the environment, or the `gh` CLI.

    Falling back to `gh auth token` is not a convenience: it is what keeps a
    long-lived PAT out of `.env` on a developer machine that already has a
    scoped, refreshable credential. In a container neither the CLI nor a
    keychain exists, which is why the environment variable comes first.
    """
    if explicit:
        return explicit
    for var in ("AUDITREADY_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        value = os.getenv(var)
        if value:
            return value
    try:
        done = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConnectorError(
            "no GitHub token: set GITHUB_TOKEN, or install the gh CLI and run "
            f"`gh auth login` ({exc})"
        ) from exc
    token = done.stdout.strip()
    if done.returncode != 0 or not token:
        raise ConnectorError(
            "no GitHub token: set GITHUB_TOKEN (repo:read scope is enough), or "
            "run `gh auth login`"
        )
    return token


# ---------------------------------------------------------------------------
# Mapping: GitHub -> the record contract
# ---------------------------------------------------------------------------


def unwrap(payload: Any) -> Any:
    """Pull the JSON out of an MCP tool result.

    The server answers with content blocks — `[{"type": "text", "text": "<json>"}]`
    — so the useful payload is a JSON string inside a list inside a result. This
    lives in one function because every tool call needs it and because the shape
    is the server's, not ours; if it changes, one place changes.
    """
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ConnectorError(f"MCP returned non-JSON text: {payload[:200]}") from exc
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        block = payload[0]
        if "text" in block:
            return unwrap(block["text"])
    return payload


def label_names(raw: Any) -> list[str]:
    """Label names, lowercased, from either a list of strings or of objects."""
    names = []
    for item in raw or []:
        name = item if isinstance(item, str) else (item or {}).get("name", "")
        if name:
            names.append(str(name).strip().lower())
    return names


def map_type(labels: list[str]) -> tuple[str, bool]:
    """Ticket type from labels; the flag says whether it was a guess."""
    for label in labels:
        if label in TYPE_BY_LABEL:
            return TYPE_BY_LABEL[label], False
    return DEFAULT_TYPE, True


def map_priority(labels: list[str]) -> tuple[str, bool]:
    """Priority from labels; the flag says whether it was a guess."""
    for label in labels:
        if label in PRIORITY_BY_LABEL:
            return PRIORITY_BY_LABEL[label], False
    return DEFAULT_PRIORITY, True


def text_of(value: Any) -> str:
    """GitHub serves issue text HTML-escaped; audit records should read as prose.

    `I&#39;d like` in a comment is not a quoting bug to leave for the judge to
    puzzle over — it is an artifact of the transport. Unescape once here, at the
    boundary, so every downstream reader (retrieval, judge, report) sees the
    same text a human sees on the issue page.
    """
    return html.unescape(str(value or ""))


def login_of(value: Any) -> str:
    """A GitHub actor as a login string, however the server chose to send it.

    The MCP server is not consistent: `user` arrives as an object with a
    `login`, while `closed_by` on the same payload arrives as a bare string.
    Guessing wrong crashes the fetch, so accept both and one absence.
    """
    if isinstance(value, dict):
        return str(value.get("login", "") or "")
    if isinstance(value, str):
        return value
    return ""


def map_comments(raw: Any) -> list[dict[str, str]]:
    """GitHub comments in corpus comment shape: author, timestamp, body."""
    out = []
    for comment in raw or []:
        if not isinstance(comment, dict):
            continue
        out.append(
            {
                "author": login_of(comment.get("user")) or "unknown",
                "timestamp": str(comment.get("created_at", "") or ""),
                "body": text_of(comment.get("body")),
            }
        )
    return out


def map_resolution(detail: dict[str, Any]) -> str:
    """GitHub's closure facts as a sentence, or an explicit statement of absence.

    A judge asked for evidence of resolution needs to be told what the source
    actually holds. Both branches below are true statements about GitHub; the
    verdict they earn is the auditor's business, not this function's.
    """
    if str(detail.get("state", "")).lower() != "closed":
        return "Not resolved: the issue is still open in GitHub."
    closed_by = login_of(detail.get("closed_by"))
    reason = str(detail.get("state_reason", "") or "unspecified")
    when = str(detail.get("closed_at", "") or "unknown date")
    who = f" by @{closed_by}" if closed_by else ""
    return (
        f"Closed as {reason}{who} on {when}. GitHub Issues has no resolution "
        "field; this is the closure record the source holds."
    )


def map_issue(
    detail: dict[str, Any],
    comments: list[dict[str, Any]],
    *,
    owner: str,
    repo: str,
) -> tuple[dict[str, Any], list[str]]:
    """One GitHub issue as one contract-shaped ticket, plus its mapping notes."""
    number = detail.get("number")
    labels = label_names(detail.get("labels"))
    ticket_type, guessed_type = map_type(labels)
    priority, guessed_priority = map_priority(labels)
    mapped_comments = map_comments(comments)
    body = text_of(detail.get("body"))
    changes, emails = find_linked_ids(body, *(c["body"] for c in mapped_comments))
    assignees = detail.get("assignees") or []
    first = assignees[0] if assignees else {}
    assignee = login_of(first) or None

    notes: list[str] = []
    if guessed_type:
        notes.append(f"type defaulted to {DEFAULT_TYPE} (labels: {labels or 'none'})")
    if guessed_priority:
        notes.append(f"priority defaulted to {DEFAULT_PRIORITY} (labels: {labels or 'none'})")

    ticket = fill_ticket(
        {
            "ticket_id": f"GH-{number}",
            "title": text_of(detail.get("title")),
            "type": ticket_type,
            "priority": priority,
            "status": str(detail.get("state", "") or "open").lower(),
            "created": str(detail.get("created_at", "") or ""),
            "closed": detail.get("closed_at") or None,
            "reporter": login_of(detail.get("user")) or "unknown",
            "assignee": assignee,
            "description": body,
            "comments": mapped_comments,
            "linked_changes": changes,
            "linked_emails": emails,
            "resolution": map_resolution(detail),
            "signoff": None,  # GitHub has no approval record; see the module docstring
            # Extra keys survive `fill_ticket`, and this one is what lets a
            # reviewer open the evidence a verdict cites.
            "source_url": detail.get("html_url")
            or f"https://github.com/{owner}/{repo}/issues/{number}",
            "source_labels": labels,
        }
    )
    return ticket, notes


# ---------------------------------------------------------------------------
# The connector
# ---------------------------------------------------------------------------


class GitHubIssuesSource:
    """Read issues from one repository over the GitHub MCP server.

    `state` is None for both open and closed, or "OPEN"/"CLOSED" to narrow.
    `labels` narrows server-side, which matters on a busy repo: the cost here is
    two calls per ticket, so fetching only what the audit scope covers is the
    difference between a demo and a rate limit.
    """

    def __init__(
        self,
        owner: str,
        repo: str,
        *,
        token: str | None = None,
        state: str | None = None,
        labels: list[str] | None = None,
        url: str = GITHUB_MCP_URL,
    ) -> None:
        self.owner = owner
        self.repo = repo
        self.state = state
        self.labels = labels or []
        self.url = url
        self._token = token
        self.name = f"github:{owner}/{repo}"

    # -- public API ---------------------------------------------------------

    def fetch(self, limit: int | None = None) -> EvidenceRecords:
        """Synchronous `fetch`, because the rest of the engine is synchronous.

        The MCP client is async and the batch runner is threads; bridging with
        `asyncio.run` here keeps that asymmetry inside the one module that has
        an event loop at all, instead of colouring the whole call stack async.
        """
        try:
            return asyncio.run(self._fetch(limit))
        except ConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001 - one failure type crosses the seam
            raise ConnectorError(f"{self.name}: fetch failed: {type(exc).__name__}: {exc}") from exc

    # -- internals ----------------------------------------------------------

    async def _fetch(self, limit: int | None) -> EvidenceRecords:
        from langchain_mcp_adapters.client import MultiServerMCPClient

        token = github_token(self._token)
        client = MultiServerMCPClient(
            {
                "github": {
                    "transport": "streamable_http",
                    "url": self.url,
                    "headers": {"Authorization": f"Bearer {token}"},
                }
            }
        )
        tools = {tool.name: tool for tool in await client.get_tools()}
        for required in ("list_issues", "issue_read"):
            if required not in tools:
                raise ConnectorError(
                    f"the MCP server at {self.url} offers no {required!r} tool; "
                    f"available: {sorted(tools)[:8]} ..."
                )

        index = await self._list_issues(tools["list_issues"], limit)
        if not index:
            raise ConnectorError(
                f"{self.name}: no issues matched "
                f"(state={self.state or 'any'}, labels={self.labels or 'any'}). "
                "Wrong repo, or a token without access to it?"
            )

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_READS)
        results = await asyncio.gather(
            *(self._read_issue(tools["issue_read"], issue, semaphore) for issue in index)
        )

        tickets: list[dict[str, Any]] = []
        inferred: dict[str, list[str]] = {}
        for ticket, notes in results:
            tickets.append(ticket)
            if notes:
                inferred[ticket["ticket_id"]] = notes

        return EvidenceRecords(
            tickets=tickets,
            source=self.name,
            detail={
                "mcp_url": self.url,
                "repo": f"{self.owner}/{self.repo}",
                "state_filter": self.state,
                "label_filter": self.labels,
                "issues_indexed": len(index),
                "inferred": inferred,
                "contract_gaps": [
                    "signoff is always absent: GitHub Issues has no approval record",
                    "changes/emails are empty: they live in other systems "
                    "(see --artifacts-from)",
                ],
            },
        )

    async def _list_issues(self, tool: Any, limit: int | None) -> list[dict[str, Any]]:
        """Page through the issue index until `limit` or the cursor runs out."""
        collected: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            remaining = PAGE_SIZE if limit is None else min(PAGE_SIZE, limit - len(collected))
            if remaining <= 0:
                break
            args: dict[str, Any] = {
                "owner": self.owner,
                "repo": self.repo,
                "perPage": remaining,
            }
            if self.state:
                args["state"] = self.state
            if self.labels:
                args["labels"] = self.labels
            if cursor:
                args["after"] = cursor
            page = unwrap(await tool.ainvoke(args))
            if not isinstance(page, dict):
                raise ConnectorError(f"list_issues returned {type(page).__name__}, expected an object")
            issues = page.get("issues") or []
            collected.extend(i for i in issues if isinstance(i, dict))
            info = page.get("pageInfo") or {}
            cursor = info.get("endCursor")
            if not issues or not info.get("hasNextPage") or not cursor:
                break
        return collected if limit is None else collected[:limit]

    async def _read_issue(
        self, tool: Any, issue: dict[str, Any], semaphore: asyncio.Semaphore
    ) -> tuple[dict[str, Any], list[str]]:
        """Full record + comments for one issue.

        The index projection omits `closed_at` and `state_reason`, which is
        exactly the closure evidence an audit asks for, so each issue costs a
        second call. Comments cost a third — and a ticket without its comment
        thread is a ticket with its evidence removed.
        """
        number = issue.get("number")
        base = {"owner": self.owner, "repo": self.repo, "issue_number": number}
        async with semaphore:
            detail = unwrap(await tool.ainvoke({**base, "method": "get"}))
            comments: list[Any] = []
            if issue.get("comments"):
                fetched = unwrap(await tool.ainvoke({**base, "method": "get_comments"}))
                if isinstance(fetched, list):
                    comments = fetched
                elif isinstance(fetched, dict):
                    comments = fetched.get("comments") or []
        if not isinstance(detail, dict):
            raise ConnectorError(f"issue_read get returned {type(detail).__name__} for #{number}")
        return map_issue(detail, comments, owner=self.owner, repo=self.repo)
