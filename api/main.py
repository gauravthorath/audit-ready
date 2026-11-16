"""FastAPI over core/ + audit/ - the wire shape, never the verdicts (CR-8).

The rule this package lives under is the one that made v2 cheap:
**no business logic here.** Every number a browser sees is computed by
`run_batch`, `AuditAgent`, `load_pack` or `load_tickets`. This package decides
which fields cross the wire and what HTTP status a failure earns. If a verdict
is ever computed under `api/`, the refactor has gone wrong.

Where things are:

    settings.py   every AUDITREADY_* the API reads, parsed once
    schemas.py    the response models - the contract web/ generates its types from
    middleware.py the request id: minted or accepted, bound, echoed, logged once
    ratelimit.py  token buckets per caller for the two routes that spend money
    auth.py       who is calling: none | bearer token | the customer's OIDC issuer
    mcp_server.py the engine published as an MCP server (stdio, or /mcp over HTTP)
    runs.py       Run, RunRegistry: the worker thread and the reports/ files
    sessions.py   SessionStore: whose conversation it is, and where it lives
    deps.py       how a route reaches those (FastAPI dependencies over app.state)
    routers/      one module per concern: health, packs, records, chat, audit

This module only assembles them. Everything a deployment or a test might want
to swap hangs on `app.state` - the runner, the agent factory, the pricing
fetch, the session store, the run registry, the settings - so swapping is an
assignment, not a patch of a private name.

Req: CR-8; carries FR-15 (no stack traces to the client), FR-16, FR-17.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.auth import OidcSettings, build_authenticator, parse_roles
from api.deps import cached_pricing
from api.mcp_server import build_server, check_http_is_safe
from api.mcp_server import http_enabled as mcp_http_enabled
from api.middleware import REQUEST_ID_HEADER, RequestContextMiddleware
from api.ratelimit import RateLimits
from api.routers import ALL as ROUTERS
from api.runs import RunRegistry
from api.sessions import build_session_store
from api.settings import Settings
from audit.graph import run_control
from core.spend import SpendLedger
from core.tracing import configure_logging
from assistant.agent import AuditAgent


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. Tests pass their own `Settings`; production reads the env."""
    settings = settings or Settings.from_env()
    configure_logging()  # AUDITREADY_LOG_FORMAT=json|text, AUDITREADY_LOG_LEVEL

    app = FastAPI(
        title="AuditReady API",
        version="0.1.0",
        description="HTTP boundary over the AuditReady engine. Holds no business logic.",
    )
    # Order matters: the last middleware added is the outermost. CORS must be
    # outside so its preflight answers carry the request id header too, and so
    # a rejected origin is still logged as a request.
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.web_origins,
        allow_origin_regex=settings.private_origin_regex,
        allow_methods=["*"],
        allow_headers=["*"],
        # The browser may read the id off any response, so an error it shows
        # can quote the reference that finds the server's line.
        expose_headers=[REQUEST_ID_HEADER],
        # The id header makes every fetch "non-simple", so the browser
        # preflights each URL once and then caches the answer for this long.
        max_age=600,
    )

    # The seams. Each is what deps.py hands to a route, and what a test replaces.
    app.state.settings = settings
    app.state.auth = build_authenticator(          # none | token | oidc (Step 40)
        settings.auth_mode,
        tokens=settings.api_tokens,
        oidc=OidcSettings(
            issuer=settings.oidc_issuer,
            audience=settings.oidc_audience,
            roles_claim=settings.oidc_roles_claim,
            default_roles=parse_roles(settings.oidc_default_roles),
        ),
    )
    app.state.runner = run_control          # the per-pair judge (audit/graph.py)
    app.state.agent_factory = AuditAgent    # what makes a chat agent (assistant/agent.py)
    app.state.pricing = cached_pricing      # model id -> live rate, or None
    app.state.sessions = build_session_store(settings.session_store, settings.pg_dsn)
    app.state.runs = RunRegistry(settings.reports_dir)
    app.state.runs.load_from_disk()         # CLI reports are runs too
    # Spend and limits (Step 38): the ledger lives beside the reports it pays for.
    app.state.ledger = SpendLedger(settings.reports_dir / "spend.jsonl")
    app.state.limits = RateLimits.build(
        settings.chat_per_minute, settings.audits_per_hour, settings.max_pairs_per_run
    )

    for router in ROUTERS:
        app.include_router(router)

    # The engine as an MCP server, for clients that are not this browser
    # (Claude Desktop, Cursor, a Microsoft 365 Copilot agent in Teams). Off
    # unless asked for, and refused on an open deployment - see api/mcp_server.py.
    if mcp_http_enabled():
        check_http_is_safe(settings.auth_mode)
        app.mount("/mcp", build_server().streamable_http_app())

    return app


app = create_app()
