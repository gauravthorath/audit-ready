"""The Jira and Linear connectors, offline (CR-3).

Same bargain as `tests/test_connectors.py`: no API key, no network, no LLM.
The mapping functions are pure, and the fetch loops are driven through
`httpx.MockTransport`, so pagination, GraphQL error handling and the auth
failure messages are all exercised against recorded payloads rather than a
live site. A connector whose paging only works against one real project is a
connector nobody can change safely.

Fixtures are trimmed real response shapes - Jira's ADF description and
`nextPageToken` paging, Linear's Relay connection and 0-4 priority integer.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from audit import jira_evidence as jira
from audit import linear_evidence as linear
from audit.connectors import ConnectorError, build_source, validate_records, validate_ticket

# ==========================================================================
# Jira
# ==========================================================================


def _adf(*paragraphs: str) -> dict:
    """A minimal Atlassian Document Format document, as the v3 API serves it."""
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": p}]}
            for p in paragraphs
        ],
    }


def _jira_issue(key: str = "AUD-12", **field_overrides) -> dict:
    fields = {
        "summary": "Mail relay outage for Northwind",
        "issuetype": {"name": "Bug"},
        "priority": {"name": "Highest"},
        "status": {"name": "Done"},
        "resolution": {"name": "Fixed"},
        "resolutiondate": "2026-03-04T16:40:00.000+0000",
        "created": "2026-03-03T09:12:00.000+0000",
        "reporter": {"displayName": "Priya Nair"},
        "assignee": {"displayName": "Daniel Okafor"},
        "description": _adf("Root cause traced to CHG-3001.", "Client told in EML-2002."),
        "labels": ["sev1"],
    }
    fields.update(field_overrides)
    return {"key": key, "fields": fields}


# -- ADF -------------------------------------------------------------------


def test_adf_becomes_prose_not_json():
    text = jira.flatten_adf(_adf("first line", "second line"))
    assert text == "first line\nsecond line"
    assert "{" not in text


def test_a_plain_string_description_passes_through():
    # v2 responses and hand-written fixtures are plain; both must work.
    assert jira.flatten_adf("already prose") == "already prose"
    assert jira.flatten_adf(None) == ""


def test_adf_keeps_the_parts_an_auditor_would_cite():
    doc = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Approved by "},
                    {"type": "mention", "attrs": {"text": "@Priya Nair"}},
                    {"type": "text", "text": " see "},
                    {
                        "type": "text",
                        "text": "the change",
                        "marks": [{"type": "link", "attrs": {"href": "https://x/CHG-3001"}}],
                    },
                ],
            }
        ],
    }
    text = jira.flatten_adf(doc)
    assert "@Priya Nair" in text
    assert "https://x/CHG-3001" in text


# -- config and auth -------------------------------------------------------


@pytest.mark.parametrize(
    "written",
    ["acme.atlassian.net", "https://acme.atlassian.net", "https://acme.atlassian.net/jira/x"],
)
def test_a_site_is_accepted_however_it_was_pasted(written):
    assert jira.jira_site(written) == "https://acme.atlassian.net"


def test_a_missing_site_names_the_variable_to_set(monkeypatch):
    monkeypatch.delenv("AUDITREADY_JIRA_SITE", raising=False)
    with pytest.raises(ConnectorError, match="AUDITREADY_JIRA_SITE"):
        jira.jira_site()


def test_basic_auth_is_email_and_token_not_a_password():
    header = jira.jira_auth("fred@example.com", "tok123")
    assert header.startswith("Basic ")
    decoded = base64.b64decode(header.split(" ", 1)[1]).decode()
    assert decoded == "fred@example.com:tok123"


def test_missing_jira_credentials_point_at_the_token_page(monkeypatch):
    monkeypatch.delenv("AUDITREADY_JIRA_EMAIL", raising=False)
    monkeypatch.delenv("AUDITREADY_JIRA_TOKEN", raising=False)
    with pytest.raises(ConnectorError, match="api-tokens"):
        jira.jira_auth()


# -- mapping ---------------------------------------------------------------


def test_a_jira_issue_maps_to_a_contract_valid_ticket():
    comments = [
        {
            "author": {"displayName": "Daniel Okafor"},
            "created": "2026-03-03T14:30:00.000+0000",
            "body": _adf("Cert renewed, monitoring added."),
        }
    ]
    ticket, notes = jira.map_issue(_jira_issue(), comments, site="https://acme.atlassian.net")

    validate_ticket(ticket)  # the point of the whole mapping
    assert ticket["ticket_id"] == "AUD-12"
    assert (ticket["type"], ticket["priority"]) == ("incident", "high")
    assert ticket["status"] == "done"
    assert ticket["reporter"] == "Priya Nair"
    assert ticket["assignee"] == "Daniel Okafor"
    assert ticket["comments"][0]["author"] == "Daniel Okafor"
    assert ticket["comments"][0]["body"] == "Cert renewed, monitoring added."
    # Links are recovered from ADF prose, since Jira has no field for them.
    assert ticket["linked_changes"] == ["CHG-3001"]
    assert ticket["linked_emails"] == ["EML-2002"]
    assert ticket["source_url"] == "https://acme.atlassian.net/browse/AUD-12"
    assert notes == []  # nothing was guessed


def test_what_jira_cannot_supply_is_reported_not_invented():
    issue = _jira_issue(
        issuetype={"name": "Spike"},          # not a type we know
        priority={"name": "Whenever"},        # not a priority we know
        resolution=None,
        resolutiondate=None,
        status={"name": "In Progress"},
        assignee=None,
    )
    ticket, notes = jira.map_issue(issue, [], site="https://acme.atlassian.net")

    validate_ticket(ticket)
    assert ticket["signoff"] is None      # base Jira has no approval record
    assert ticket["closed"] is None
    assert ticket["assignee"] is None
    assert "not resolved" in ticket["resolution"].lower()
    assert len(notes) == 2
    assert any(n.startswith("type defaulted") for n in notes)
    assert any(n.startswith("priority defaulted") for n in notes)


def test_an_issue_without_a_key_is_refused_loudly():
    with pytest.raises(ConnectorError, match="no key"):
        jira.map_issue({"fields": {}}, [], site="https://acme.atlassian.net")


# -- fetch -----------------------------------------------------------------


def _jira_transport(pages: list[dict], comments: dict[str, list] | None = None):
    """A MockTransport that serves /search/jql pages and per-issue comments."""
    comments = comments or {}
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        path = request.url.path
        if path.endswith("/search/jql"):
            token = request.url.params.get("nextPageToken")
            index = 0 if token is None else int(token)
            return httpx.Response(200, json=pages[index])
        if "/comment" in path:
            key = path.split("/issue/")[1].split("/")[0]
            return httpx.Response(200, json={"comments": comments.get(key, []), "total": len(comments.get(key, []))})
        return httpx.Response(404, json={"errorMessages": ["nope"]})

    return httpx.MockTransport(handler), calls


def test_jira_fetch_follows_nextpagetoken_to_the_end():
    pages = [
        {"issues": [_jira_issue("AUD-1")], "nextPageToken": "1", "isLast": False},
        {"issues": [_jira_issue("AUD-2")], "isLast": True},
    ]
    transport, calls = _jira_transport(pages)
    client = httpx.Client(transport=transport, base_url="https://acme.atlassian.net")
    source = jira.JiraIssuesSource("AUD", site="acme.atlassian.net", client=client)

    records = source.fetch()
    validate_records(records)  # the gate every source passes

    assert [t["ticket_id"] for t in records.tickets] == ["AUD-1", "AUD-2"]
    assert records.detail["pages"] == 2
    assert records.detail["transport"] == "rest"
    # /search/jql, not the removed /search
    assert any("/search/jql" in c for c in calls)
    assert not any(c.endswith("/search") for c in calls)


def test_jira_comments_are_fetched_per_issue_because_search_truncates_them():
    pages = [{"issues": [_jira_issue("AUD-1")], "isLast": True}]
    transport, calls = _jira_transport(
        pages,
        comments={
            "AUD-1": [
                {
                    "author": {"displayName": "Priya Nair"},
                    "created": "2026-03-04T15:05:00.000+0000",
                    "body": _adf("Approved for release."),
                }
            ]
        },
    )
    client = httpx.Client(transport=transport, base_url="https://acme.atlassian.net")
    source = jira.JiraIssuesSource("AUD", site="acme.atlassian.net", client=client)

    records = source.fetch()
    assert any("/issue/AUD-1/comment" in c for c in calls)
    assert records.tickets[0]["comments"][0]["body"] == "Approved for release."


def test_jira_fetch_respects_a_limit():
    pages = [{"issues": [_jira_issue("AUD-1"), _jira_issue("AUD-2")], "isLast": True}]
    transport, _ = _jira_transport(pages)
    client = httpx.Client(transport=transport, base_url="https://acme.atlassian.net")
    source = jira.JiraIssuesSource("AUD", site="acme.atlassian.net", client=client)

    assert len(source.fetch(limit=1).tickets) == 1


def test_a_jira_401_says_which_variable_is_wrong():
    def handler(request):
        return httpx.Response(401, json={"errorMessages": ["Unauthorized"]})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://acme.atlassian.net")
    source = jira.JiraIssuesSource("AUD", site="acme.atlassian.net", client=client)
    with pytest.raises(ConnectorError, match="AUDITREADY_JIRA_TOKEN"):
        source.fetch()


def test_jira_needs_a_project_or_a_query():
    with pytest.raises(ConnectorError, match="project key or a jql"):
        jira.JiraIssuesSource(site="acme.atlassian.net")


# ==========================================================================
# Linear
# ==========================================================================


def _linear_issue(identifier: str = "AUD-12", **overrides) -> dict:
    issue = {
        "identifier": identifier,
        "title": "Mail relay outage for Northwind",
        "description": "Root cause traced to CHG-3001. Client told in EML-2002.",
        "priority": 2,
        "priorityLabel": "High",
        "createdAt": "2026-03-03T09:12:00.000Z",
        "completedAt": "2026-03-04T16:40:00.000Z",
        "canceledAt": None,
        "url": f"https://linear.app/acme/issue/{identifier}",
        "state": {"name": "Done", "type": "completed"},
        "creator": {"displayName": "Priya Nair"},
        "assignee": {"displayName": "Daniel Okafor"},
        "labels": {"nodes": [{"name": "Bug"}]},
        "comments": {
            "nodes": [
                {
                    "createdAt": "2026-03-03T14:30:00.000Z",
                    "body": "Cert renewed, monitoring added.",
                    "user": {"displayName": "Daniel Okafor"},
                }
            ]
        },
    }
    issue.update(overrides)
    return issue


def test_a_linear_token_is_sent_without_a_bearer_prefix(monkeypatch):
    monkeypatch.setenv("AUDITREADY_LINEAR_TOKEN", "lin_api_xyz")
    assert linear.linear_token() == "lin_api_xyz"


def test_a_missing_linear_token_names_where_to_mint_one(monkeypatch):
    monkeypatch.delenv("AUDITREADY_LINEAR_TOKEN", raising=False)
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    with pytest.raises(ConnectorError, match="Personal API keys"):
        linear.linear_token()


@pytest.mark.parametrize(
    "level,expected,guessed",
    [(1, "high", False), (2, "high", False), (3, "medium", False), (4, "low", False),
     (0, "medium", True), (None, "medium", True)],
)
def test_linear_priority_integers_map_and_zero_is_an_admitted_absence(level, expected, guessed):
    assert linear.map_priority(level) == (expected, guessed)


def test_a_linear_issue_maps_to_a_contract_valid_ticket():
    ticket, notes = linear.map_issue(_linear_issue())

    validate_ticket(ticket)
    assert ticket["ticket_id"] == "AUD-12"
    assert (ticket["type"], ticket["priority"]) == ("incident", "high")
    assert ticket["status"] == "done"
    assert ticket["reporter"] == "Priya Nair"
    assert ticket["assignee"] == "Daniel Okafor"
    assert ticket["closed"] == "2026-03-04T16:40:00.000Z"
    assert ticket["comments"][0]["author"] == "Daniel Okafor"
    assert ticket["linked_changes"] == ["CHG-3001"]
    assert ticket["linked_emails"] == ["EML-2002"]
    assert "completed on" in ticket["resolution"].lower()
    assert notes == []


def test_what_linear_cannot_supply_is_reported_not_invented():
    issue = _linear_issue(
        priority=0,
        labels={"nodes": []},
        state={"name": "In Progress", "type": "started"},
        completedAt=None,
        assignee=None,
    )
    ticket, notes = linear.map_issue(issue)

    validate_ticket(ticket)
    assert ticket["signoff"] is None      # Linear has no approval record
    assert ticket["closed"] is None
    assert ticket["assignee"] is None
    assert "not resolved" in ticket["resolution"].lower()
    assert len(notes) == 2
    assert any("no priority set" in n for n in notes)


def test_a_cancelled_linear_issue_is_not_reported_as_completed():
    issue = _linear_issue(
        completedAt=None,
        canceledAt="2026-03-05T10:00:00.000Z",
        state={"name": "Cancelled", "type": "canceled"},
    )
    ticket, _ = linear.map_issue(issue)
    assert "cancelled" in ticket["resolution"].lower()
    assert ticket["closed"] == "2026-03-05T10:00:00.000Z"


def _linear_transport(pages: list[dict]):
    """MockTransport serving Relay pages; records the variables it was sent."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body.get("variables", {}))
        index = len(seen) - 1
        return httpx.Response(200, json=pages[index])

    return httpx.MockTransport(handler), seen


def test_linear_fetch_follows_the_relay_cursor():
    pages = [
        {"data": {"issues": {
            "pageInfo": {"hasNextPage": True, "endCursor": "cur1"},
            "nodes": [_linear_issue("AUD-1")]}}},
        {"data": {"issues": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [_linear_issue("AUD-2")]}}},
    ]
    transport, seen = _linear_transport(pages)
    client = httpx.Client(transport=transport)
    source = linear.LinearIssuesSource("AUD", client=client)

    records = source.fetch()
    validate_records(records)

    assert [t["ticket_id"] for t in records.tickets] == ["AUD-1", "AUD-2"]
    assert records.detail["transport"] == "graphql"
    assert seen[0]["after"] is None
    assert seen[1]["after"] == "cur1"          # the cursor was actually used
    assert seen[0]["filter"] == {"team": {"key": {"eq": "AUD"}}}


def test_a_graphql_error_is_not_read_as_an_empty_audit():
    """200 with an errors array must fail, not return zero evidence."""
    pages = [{"errors": [{"message": "Authentication required"}]}]
    transport, _ = _linear_transport(pages)
    client = httpx.Client(transport=transport)
    source = linear.LinearIssuesSource(client=client)

    with pytest.raises(ConnectorError, match="Authentication required"):
        source.fetch()


def test_a_linear_400_points_at_the_header_shape():
    def handler(request):
        return httpx.Response(400, text="authentication failed")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    source = linear.LinearIssuesSource(client=client)
    with pytest.raises(ConnectorError, match="Bearer"):
        source.fetch()


# ==========================================================================
# The registry
# ==========================================================================


def test_jira_and_linear_are_no_longer_stubs(monkeypatch):
    monkeypatch.setenv("AUDITREADY_JIRA_SITE", "acme.atlassian.net")
    monkeypatch.setenv("AUDITREADY_JIRA_EMAIL", "a@b.c")
    monkeypatch.setenv("AUDITREADY_JIRA_TOKEN", "tok")
    monkeypatch.setenv("AUDITREADY_LINEAR_TOKEN", "lin")

    assert build_source("jira", project="AUD").name == "jira"
    assert build_source("linear", team="AUD").name == "linear"


def test_the_registry_lists_the_new_sources():
    with pytest.raises(ConnectorError, match="jira, linear"):
        build_source("sharepoint")
