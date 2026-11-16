"""Live smoke for Step 42: a real MCP client gets a cited verdict from this engine.

Speaks the protocol properly rather than calling the functions: it launches
`python -m api.mcp_server` as a subprocess over stdio, the way Claude Desktop,
Cursor or a Copilot Studio connector would, completes the MCP handshake, lists
the tools, and calls `check_ticket` for real - a live model call whose verdict
comes back with the record fields it rests on.

    uv run python scripts/smoke_mcp_server.py

Needs OPENROUTER_API_KEY (or a BYO endpoint) and an ingested vector store.
Costs one judged pair (~3,500 tokens).
"""

from __future__ import annotations

import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

TICKET, CONTROL = "TCK-1001", "ISO9001-8.6-release-signoff"


async def run() -> int:
    params = StdioServerParameters(command=sys.executable, args=["-m", "api.mcp_server"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            print(f"  connected: {info.serverInfo.name} {info.serverInfo.version}")

            listed = await session.list_tools()
            names = sorted(t.name for t in listed.tools)
            print(f"  tools: {', '.join(names)}")

            packs = await session.call_tool("list_packs", {})
            pack_ids = [p["id"] for p in packs.structuredContent["result"]]
            print(f"  packs: {pack_ids}")

            result = await session.call_tool(
                "check_ticket", {"ticket_id": TICKET, "control_id": CONTROL})
            verdict = result.structuredContent

            refused = await session.call_tool("audit_readiness", {})

    print(f"  verdict: {verdict.get('verdict')} ({verdict.get('status', 'n/a')})")
    print(f"  citations: {verdict.get('citations')}")
    print(f"  citation_check: {(verdict.get('citation_check') or {}).get('status')}")
    print(f"  rationale: {str(verdict.get('rationale'))[:110]}")
    print(f"  unscoped rollup refused: {refused.isError}")

    problems = []
    if names != ["audit_readiness", "check_ticket", "find_evidence", "list_packs"]:
        problems.append(f"unexpected tool list: {names}")
    if "iso9001-sample" not in pack_ids:
        problems.append(f"packs not listed: {pack_ids}")
    if verdict.get("verdict") not in ("satisfied", "partial", "gap", "not_applicable"):
        problems.append(f"no verdict: {json.dumps(verdict)[:200]}")
    if not verdict.get("citations"):
        problems.append("a verdict came back with no citation - the one thing this must never do")
    if not refused.isError:
        problems.append("an unscoped rollup was not refused")

    if problems:
        print("FAILED: " + "; ".join(problems))
        return 1
    print("PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
