"""AuditReady as an MCP server - the engine, reachable from someone else's client (Step 42, CR-3).

Until now the Model Context Protocol appeared in this project in one
direction: `audit/mcp_evidence.py` *consumes* GitHub's MCP server to read
issues as evidence. That is a real use of the protocol and it is live-tested,
but it is half of it. The reviewer's note - *if MCP is not used, remove it;
otherwise use it fully* - has an answer better than either: publish the engine
as a server, so a compliance question can be asked from wherever the person
asking it already works.

Concretely, that is Microsoft Teams. Approvals and "yes, go ahead" happen in a
channel, and Microsoft 365 Copilot agents built in Copilot Studio consume MCP
servers. Pointing one at this server gives a compliance officer "is TCK-1007
compliant with 8.5.6?" in the place the conversation is already happening,
with the citation attached - and it costs us one module rather than a bot
framework, a messaging endpoint and a second deployment to keep alive.

What is exposed, and what is deliberately not
---------------------------------------------
Four tools, all read-only, all already the engine's public surface:

* `list_packs` - which standards this deployment can judge against.
* `find_evidence` - semantic search over the corpus. The cheap one.
* `check_ticket` - the judgement: one record against one control, verdict plus
  the citations it rests on. This is the tool worth having in Teams.
* `audit_readiness` - the rollup, with a hard cap on how much it may spend.

`coverage_report` over the whole corpus is 180 model calls, so the MCP tool
that wraps it refuses an unscoped sweep rather than quietly spending five
minutes and eleven cents on behalf of a client that asked casually. The same
rule the HTTP API follows (refuse before the thread, with the estimate in the
sentence), applied to a transport where the caller is a model.

Nothing that writes is exposed. An MCP client is an agent holding someone
else's credential; the blast radius of a read-only server is a leak, and the
blast radius of a writable one is a forged audit record.

Two transports, one server
--------------------------
* **stdio** - `uv run python -m api.mcp_server`. What Claude Desktop, Cursor
  and the other local clients speak.
* **streamable HTTP** - mounted on the FastAPI app at `/mcp` when
  `AUDITREADY_MCP_HTTP=1`. What a remote client (Copilot Studio) needs. It is
  off by default because an unauthenticated MCP endpoint on a port someone
  forgot about is exactly the hole this project spent Step 40 closing; turning
  it on is a deliberate act, and it is refused unless `AUDITREADY_AUTH` is set
  to something other than `none`.

Req: CR-3, CR-8.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from audit.reporting import discover_packs, estimate_run_cost_usd
from core import tools as engine
from core.tracing import configure_logging, log_event

log = logging.getLogger("auditready.mcp")

# An unscoped sweep is 180 model calls. A client that means it passes ids.
MAX_UNSCOPED_PAIRS = 40

INSTRUCTIONS = """AuditReady judges service records (tickets, emails, change
records) against compliance checklist packs and returns a verdict with the
citations it rests on.

Use `list_packs` first to see which standards and controls exist. Use
`check_ticket` to judge one record against one control - that is the tool that
answers "is this compliant?". Use `find_evidence` to look for supporting
artifacts. Use `audit_readiness` only for a scoped set of records and controls.

Every verdict is a draft for a human auditor. The tool prepares and cites
evidence; it does not certify, and its output should not reach a certifying
body without a person having reviewed it. A verdict of `needs_review` means
the engine declined to stand behind an answer - report that rather than
guessing on its behalf."""


def build_server(name: str = "auditready") -> FastMCP:
    """The server, built fresh so a test can hold one without touching a global."""
    mcp = FastMCP(name, instructions=INSTRUCTIONS)

    @mcp.tool()
    def list_packs() -> list[dict[str, Any]]:
        """List the checklist packs this deployment can judge against, with their controls.

        Call this first: control ids are not guessable, and `check_ticket`
        needs one. Packs that failed to load are listed with their error
        rather than hidden, so a misconfigured deployment says so.
        """
        out = []
        for entry in discover_packs():
            pack = entry["pack"]
            if pack is None:
                out.append({"id": None, "label": entry["label"], "error": entry["error"]})
                continue
            out.append({
                "id": pack.pack.id,
                "standard": pack.pack.standard,
                "controls": [
                    {"control_id": c.control_id, "clause": c.clause, "title": c.title,
                     "severity": c.severity}
                    for c in pack.controls
                ],
            })
        return out

    @mcp.tool()
    def find_evidence(query: str, ticket_id: str | None = None,
                      doc_type: str | None = None, k: int = 5) -> list[dict[str, Any]]:
        """Search the evidence corpus for records matching a natural-language query.

        Filters AND together: `ticket_id` scopes to one record and everything
        linked to it; `doc_type` is one of ticket, ticket_comment, email,
        change. Returns chunk ids that are valid citations, with similarity
        scores. This makes no model call and costs nothing.
        """
        return engine.find_evidence(query, ticket_id=ticket_id, doc_type=doc_type,
                                    k=max(1, min(int(k), 20)))

    @mcp.tool()
    def check_ticket(ticket_id: str, control_id: str | None = None) -> dict[str, Any]:
        """Judge one record against one control and return the verdict with its citations.

        `control_id` comes from `list_packs`; omitting it judges the record
        against every control in the default pack, which costs one model call
        per control. The verdict is one of satisfied, partial, gap,
        not_applicable, and `citations` are the exact record fields it rests
        on. A result whose `citation_check` is not `ok` has not been verified
        and must be reported as such.
        """
        result = engine.check_ticket(ticket_id, control_id)
        log_event(log, "mcp.check_ticket", ticket_id=ticket_id, control_id=control_id)
        return result

    @mcp.tool()
    def audit_readiness(ticket_ids: list[str] | None = None,
                        control_ids: list[str] | None = None) -> dict[str, Any]:
        """Readiness rollup over a *scoped* set of records and controls.

        Judges every record against every control and returns the
        severity-weighted readiness score, the per-control counts and the open
        gaps. Pass ids: an unscoped sweep of the whole corpus is refused,
        because it is hundreds of model calls and several minutes, and a
        client that asked casually should not spend them.
        """
        tickets = ticket_ids or sorted(engine.load_tickets())
        controls = control_ids or [c.control_id for c in engine._pack().controls]
        pairs = len(tickets) * len(controls)
        if (ticket_ids is None or control_ids is None) and pairs > MAX_UNSCOPED_PAIRS:
            usd, _ = estimate_run_cost_usd(pairs, None)
            raise ValueError(
                f"That would judge {pairs} record-control pairs (~{2 * pairs}-{3 * pairs} model "
                f"calls, roughly ${usd:.2f}). Pass ticket_ids and control_ids to scope it to at "
                f"most {MAX_UNSCOPED_PAIRS} pairs, or use the batch report for a full sweep."
            )
        report = engine.coverage_report(ticket_ids=ticket_ids, control_ids=control_ids)
        log_event(log, "mcp.audit_readiness", pairs=pairs)
        return report

    return mcp


def http_enabled(env: dict[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return env.get("AUDITREADY_MCP_HTTP", "").strip().lower() in ("1", "true", "yes", "on")


def check_http_is_safe(auth_mode: str) -> None:
    """Refuse to publish the engine over HTTP on an open deployment.

    Over stdio the client is a process the user started, and the operating
    system is the access control. Over HTTP it is anyone who can reach the
    port. Serving compliance judgements there without a credential is the hole
    Step 40 exists to close, so this raises at startup rather than letting a
    deployment discover it later.
    """
    if (auth_mode or "none").strip().lower() == "none":
        raise ValueError(
            "AUDITREADY_MCP_HTTP=1 needs AUDITREADY_AUTH=token or oidc: an MCP endpoint "
            "without a credential publishes the engine to anyone who can reach the port."
        )


def main() -> int:
    """Run over stdio - the transport local MCP clients speak.

    Logs go to stderr because stdout *is* the protocol channel: a stray print
    on stdout corrupts the message stream, which is the classic way an MCP
    server appears broken for no visible reason.
    """
    import sys

    configure_logging(stream=sys.stderr)
    build_server().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
