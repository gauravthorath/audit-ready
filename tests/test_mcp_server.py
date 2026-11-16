"""The engine published as an MCP server (Step 42). Offline - the engine is faked.

What is pinned: the four tools exist with the argument names a client will
send; every description says what the tool costs and what its result means,
because an MCP tool description is the only documentation the calling model
gets; an unscoped rollup is refused with the price in the sentence; and the
HTTP transport refuses to start on a deployment with no authentication.
"""

from __future__ import annotations

import asyncio

import pytest

from api import mcp_server
from api.mcp_server import MAX_UNSCOPED_PAIRS, build_server, check_http_is_safe, http_enabled


@pytest.fixture()
def server():
    return build_server("auditready-test")


def tools(server) -> dict:
    return {t.name: t for t in asyncio.run(server.list_tools())}


def described(server, name) -> str:
    """A tool's description with the source wrapping flattened - clients see one string."""
    return " ".join((tools(server)[name].description or "").split())


def call(server, name, **kwargs):
    return asyncio.run(server.call_tool(name, kwargs))


# ---------------------------------------------------------------------------
# The surface a client sees
# ---------------------------------------------------------------------------


def test_the_four_read_only_tools_are_exposed_and_nothing_that_writes(server):
    names = set(tools(server))
    assert names == {"list_packs", "find_evidence", "check_ticket", "audit_readiness"}
    assert not any(v in n for n in names for v in ("write", "delete", "update", "start"))


def test_every_tool_describes_what_it_costs_and_what_the_result_means(server):
    """The description is the only documentation the calling model ever reads."""
    assert "costs nothing" in described(server, "find_evidence")
    assert "one model call per control" in described(server, "check_ticket")
    assert "citation_check" in described(server, "check_ticket")
    assert "refused" in described(server, "audit_readiness")
    assert "Call this first" in described(server, "list_packs")


def test_the_arguments_are_the_engines_own(server):
    described = tools(server)
    assert set(described["check_ticket"].inputSchema["properties"]) == {"ticket_id", "control_id"}
    assert described["check_ticket"].inputSchema["required"] == ["ticket_id"]
    assert set(described["find_evidence"].inputSchema["properties"]) == {
        "query", "ticket_id", "doc_type", "k"}


def test_the_instructions_say_it_drafts_and_never_certifies(server):
    assert "does not certify" in server.instructions
    assert "needs_review" in server.instructions


# ---------------------------------------------------------------------------
# The tools delegate, and the expensive one refuses
# ---------------------------------------------------------------------------


def test_check_ticket_returns_the_engines_verdict_unchanged(server, monkeypatch):
    verdict = {"ticket_id": "TCK-1001", "control_id": "ISO9001-8.6-release-signoff",
               "verdict": "satisfied", "citations": ["signoff.approver"],
               "citation_check": {"status": "ok"}}
    monkeypatch.setattr(mcp_server.engine, "check_ticket", lambda t, c=None: verdict)
    _, structured = call(server, "check_ticket", ticket_id="TCK-1001",
                         control_id="ISO9001-8.6-release-signoff")
    assert structured == verdict  # the wrapper adds nothing and hides nothing


def test_find_evidence_caps_how_much_it_will_return(server, monkeypatch):
    seen = {}
    monkeypatch.setattr(mcp_server.engine, "find_evidence",
                        lambda q, ticket_id=None, doc_type=None, k=5: seen.update(k=k) or [])
    call(server, "find_evidence", query="approval", k=500)
    assert seen["k"] == 20
    call(server, "find_evidence", query="approval", k=0)
    assert seen["k"] == 1


def test_an_unscoped_rollup_is_refused_with_the_price_in_the_sentence(server, monkeypatch):
    monkeypatch.setattr(mcp_server.engine, "load_tickets",
                        lambda *a, **k: {f"TCK-{i}": {} for i in range(20)})
    monkeypatch.setattr(mcp_server.engine, "_pack",
                        lambda: type("P", (), {"controls": [type("C", (), {"control_id": f"c{i}"})()
                                                            for i in range(9)]})())
    monkeypatch.setattr(mcp_server.engine, "coverage_report",
                        lambda **k: pytest.fail("should never have run"))

    with pytest.raises(Exception) as exc:  # FastMCP wraps the ValueError
        call(server, "audit_readiness")
    message = str(exc.value)
    assert "180 record-control pairs" in message
    assert "$0." in message and str(MAX_UNSCOPED_PAIRS) in message


def test_a_scoped_rollup_runs(server, monkeypatch):
    monkeypatch.setattr(mcp_server.engine, "coverage_report",
                        lambda ticket_ids=None, control_ids=None: {"readiness_score": 0.5,
                                                                   "pairs": len(ticket_ids or [])})
    _, structured = call(server, "audit_readiness", ticket_ids=["TCK-1001"], control_ids=["c1"])
    assert structured["readiness_score"] == 0.5


# ---------------------------------------------------------------------------
# The HTTP transport is opt-in and refuses an open deployment
# ---------------------------------------------------------------------------


def test_http_is_off_unless_asked_for():
    assert http_enabled({}) is False
    assert http_enabled({"AUDITREADY_MCP_HTTP": "0"}) is False
    assert http_enabled({"AUDITREADY_MCP_HTTP": "1"}) is True
    assert http_enabled({"AUDITREADY_MCP_HTTP": "true"}) is True


def test_publishing_over_http_without_authentication_is_refused():
    with pytest.raises(ValueError, match="AUDITREADY_AUTH=token or oidc"):
        check_http_is_safe("none")
    check_http_is_safe("token")  # fine
    check_http_is_safe("oidc")   # fine
