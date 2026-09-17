# Production plan — the eight review findings, mapped to the code

**Status (2026-09-16): Steps 36–42 are built and verified. The white paper is
drafted and waits on facts only Gaurav can supply.** What each step proves is
recorded in the table at the end; the measured performance lives in
`marketing/perf-2026-09-16/`. The findings below came out of a design review on
2026-09-15. None of them were about correctness; together they are the list of
things a working prototype needs before a second team can run it. This document turns that list into
numbered steps in the same walking-skeleton discipline as everything before
it (`CLAUDE.md`, "How a step gets built"): one unproven layer per step, tests
on the pure seams, a journal entry, one commit.

Each section below says **what was asked**, **what is true today** (so the gap
is concrete), **the decision** and what it rejects, and **the step** that
carries it. Steps are ordered so that each one gives the next its footing:
the refactor first, because tracing, limits and auth all attach to the
skeleton it produces; identity before per-user limits and per-user sessions,
because both are keyed by it.

| # | Suggestion | Step | Depends on | Size |
|---|---|---|---|---|
| 7 | Thin `api/main.py`, add abstraction | **36** ✅ | — | half day |
| 4 | Project-level tracing with correlation ids | **37** ✅ | 36 | half day |
| 5 | Rate limiting and spend monitoring | **38** ✅ | 36, 37 | half day |
| 1 | Performance and failure-pattern information, public | **39** ✅ | 37 (needs measured latency) | half day |
| 6 | Authentication | **40** ✅ | 36 | one day |
| 2 | Session identity and storage for many users | **41** ✅ | 40 | half day |
| 3 | MCP: keep it and use it fully; Teams | **42** ✅ | 40 | one day |
| 8 | White paper with the origin story | **drafted** | 39 for the numbers | writing |

---

## 7. `api/main.py` is huge — thin it and add abstraction (Step 36)

**Today.** One 565-line module holds the app object, CORS, two request models,
the `Run` dataclass, two module-level dicts (`_runs`, `_sessions`), report
persistence, pricing cache, pack lookup, JSON shaping helpers, and eleven
routes across four concerns. Tests reach into it with
`monkeypatch.setattr(api, "_pricing", …)`, `api._runs.clear()`,
`api._sessions.clear()` — the test suite knows the module's private names,
which is the surest sign the module has no seams.

**Decision.** Split by *concern*, not by *layer*:

```
api/
  main.py          create_app(): settings, middleware, routers. ~80 lines.
  settings.py      every AUDITREADY_* the API reads, parsed once into a dataclass
  schemas.py       unchanged — the wire contract
  runs.py          Run, RunRegistry (the dict + its lock + disk persistence + the thread)
  sessions.py      SessionStore protocol; MemorySessionStore today, Postgres in Step 41
  deps.py          FastAPI dependencies: registry, session store, pricing, pack_by_id
  routers/
    health.py  packs.py  records.py  chat.py  audit.py
```

Everything the tests used to patch becomes a thing on `app.state` that a test
*replaces* rather than a private name it *reaches into*: `app.state.runs`
(a `RunRegistry` pointed at a temp dir), `app.state.sessions`,
`app.state.agent_factory`, `app.state.pricing`, `app.state.runner`. That is
the abstraction the reviewer asked for, and it is the same one that makes
Step 41 (swap the session store) and Step 40 (a dependency that yields the
current user) one-file changes.

**Rejected.** *A service layer with classes per use case* — the business
logic already lives in `core/` and `audit/`; a second layer of classes in
`api/` would hold nothing but forwarding calls. *Leaving it and adding
`# region` comments* — the tests would still know private names. *Splitting
by layer (`models/`, `services/`, `controllers/`)* — a reader looking for
"how does chat work" would open three directories.

**Invariants the refactor must not move.** The OpenAPI document is unchanged
byte for byte (`tests/test_openapi_contract.py`), every route keeps its
path, status codes and error mapping (FR-15), and `api/` still computes no
verdict.

## 4. Tracing the full flow — backend, front end, AI, Docker — by correlation id (Step 37)

**Today.** Nothing in `core/`, `audit/` or `api/` logs. Uvicorn's access
log is the only trace, and it has no id. A slow audit cannot be followed from
the browser's request to the model call that took eleven seconds. No report
records how long anything took — `elapsed` appears nowhere in
`reports/*.json`, which is also why suggestion 1 cannot be answered from
existing data.

**Decision.** One id, born in the browser, carried everywhere, printed on
every line.

- `core/tracing.py` (stdlib only, so `core/` stays framework-free): a
  `contextvars` bag (`request_id`, `run_id`, `session_id`, `pair`), a
  `bind(**fields)` context manager, a JSON line formatter that appends the
  bag to every record, and `span(name)` that logs `elapsed_ms`.
  `configure_logging()` reads `AUDITREADY_LOG_FORMAT=json|text` and
  `AUDITREADY_LOG_LEVEL`.
- `core/llm.chat()` logs one `llm.call` event per attempt: model,
  `elapsed_ms`, prompt/completion tokens, attempt number, outcome. This is the
  AI leg. It is the only module that talks to a model, so it is the only
  place that needs the line.
- `audit/batch.run_batch` times each pair around the runner call and
  writes `elapsed_ms` onto the row, and the wall time onto the report.
  Worker threads run under `contextvars.copy_context()` so the run id and
  the pair name reach the model-call log lines. (Fake runners in tests get
  timing for free because the timer wraps the call, not the graph.)
- `api/`: a middleware reads `X-Request-ID` (or mints one), binds it, logs
  one `http.request` line with method, path, status and `elapsed_ms`, and
  echoes the id in the response header. The audit thread is started inside
  the request's copied context, so a background run carries the request id
  that started it plus its own run id. Uvicorn's access log is turned off:
  ours has the id, theirs does not.
- `web/lib/api.ts` mints the id with `crypto.randomUUID()`, sends it, and
  puts it into every `ApiError` message, so the sentence a user sees on a
  failure ends with a reference that finds the exact server line.
- Docker: containers already write stdout; `docker compose logs api | grep
  <id>` is the trace. Compose gets a `logging` block with the service name
  as a tag so the same grep works across `api` and `web`.

**Rejected.** *OpenTelemetry now* — the right end state, but it adds a
collector, an exporter and a UI to a stack that has none; the contextvar bag
is the same shape OTel baggage has, so moving to it later is additive. Noted
as the Step 37 follow-up, not the step. *structlog* — one more dependency for
what a 40-line formatter does. *Logging inside the LangGraph nodes* — the
model call is the thing that costs time and money; log where the cost is.

## 5. Rate limiting and spend monitoring (Step 38)

**Today.** `/chat` and `/audit` are unauthenticated and unlimited. A
20-ticket × 9-control audit is 180 pairs, roughly 400 model calls; a browser
tab in a loop could start one per second. Cost is *displayed* (FR-17) per
session and per run, but nothing *adds it up* across runs or *refuses*
anything.

**Decision.** Two mechanisms, one seam.

- `core/spend.py` — a `SpendLedger`: `record(actor, kind, prompt_tokens,
  completion_tokens, usd)`, `today(actor)`, `remaining(actor)`. Append-only
  JSONL in the reports directory (`spend.jsonl`), so it survives a restart
  and a person can read it with `tail`. Budgets from the env:
  `AUDITREADY_DAILY_BUDGET_USD` (the deployment) and
  `AUDITREADY_ACTOR_DAILY_BUDGET_USD` (per user, or per client address until
  Step 40 gives us users). The Postgres-backed ledger in Step 41 implements
  the same three methods.
- `api/ratelimit.py` — a token bucket per actor, in process:
  `AUDITREADY_CHAT_PER_MINUTE` (default 30) on `/chat`,
  `AUDITREADY_AUDITS_PER_HOUR` (default 6) and
  `AUDITREADY_MAX_PAIRS_PER_RUN` (default 400) on `POST /audit`. Refusals
  are `429` with `Retry-After` and a plain sentence (FR-15). A run that would
  exceed the remaining budget by the *estimate* is refused before a thread
  or a token is spent — the same "fail here, synchronously" rule
  `start_audit` already follows for unknown ids.
- `GET /spend` returns today's spend, the budgets and what remains; the web
  nav shows it beside the model name.

**Rejected.** *A gateway (Kong, Vercel Firewall, nginx `limit_req`)* — right
for the edge, but it cannot see tokens or dollars, which is the limit that
matters here; the in-process bucket is also what makes the offline test
possible. *Redis-backed limits* — needed when there is more than one API
process; there is one, and the ledger interface is where Redis would go.

## 1. Performance information and what fails, on the site and the deck (Step 39)

**Today.** The deck and the site carry accuracy, recall, false-positive rate
and calibration, and a limits section. They carry *no latency, no throughput,
no cost per pair*, and the failure story is told as "things I retracted"
rather than "here is the pattern of what this system gets wrong".

**What the committed eval already says about failure patterns**
(`audit/eval/results.json`, 68 pairs, 2026-09-11 run):

- **Every disagreement is in the strict direction.** Four wrong verdicts:
  two `partial → gap`, two `satisfied → gap`. Zero `gap → satisfied`. The
  system over-calls deficiencies; it does not miss them. That is the
  pattern, and it is the safe one for an audit tool.
- **Five of seven parked pairs were parked for a missing citation**
  (`citation_status: missing`), not for a wrong verdict — on all five the
  verifier agreed with the verdict. The review queue is mostly a
  *citation-discipline* queue, which is why review precision is 0.43: more
  than half of what it parks was right.
- **The model moves under us.** TCK-1004 × A.8.32 was `satisfied` on 09-03
  and `gap` in all four runs on 09-11 with no code change. A hosted model is
  the one eval input that cannot be pinned (marketing/eval-spread-2026-09-11).
- **Injection is contained, not absent.** TCK-1010's planted payload is
  quarantined and reported as a security finding on every run.

**What has to be measured** (blocked on Step 37): p50/p95 latency per pair,
wall time for a 9-pair and a 180-pair run, model calls per pair, tokens per
pair (already derivable: 206,483 prompt + 19,590 completion over 68 pairs ≈
3,040 + 290 per pair ≈ $0.0006 per pair on gpt-4o-mini list price), and
throughput at 4 workers. Then: a "Performance and failure patterns" section
on the site (after 07, before the open-source section), a matching backup
slide, and the numbers into `marketing/NUMBERS.md` with their caveats.

**Rule.** Nothing goes public without the run it came from being kept beside
`marketing/eval-spread-2026-09-11/`, the same way every other number is.

## 6. Authentication (Step 40)

**Today.** None. The API's own CORS comment says so: "this API has no auth, and
a wildcard would let any website a user visits call their local engine."
That was correct for one reviewer on one laptop; it is the first thing a
second user makes false.

**Decision (recommended, to confirm before building).** A pluggable
authenticator behind one FastAPI dependency, `current_actor()`, selected by
`AUDITREADY_AUTH`:

- `none` — today's behaviour; the actor is the client address. Default in
  development so `uv run pytest` and the demo keep working.
- `token` — bearer tokens from `AUDITREADY_API_TOKENS` (`name:token,…`),
  compared in constant time, stored hashed in memory. Enough for a team
  sharing one deployment, for CI, and for the MCP server in Step 42.
- `oidc` — validate a JWT against the customer's identity provider
  (`AUDITREADY_OIDC_ISSUER`, `AUDITREADY_OIDC_AUDIENCE`, JWKS fetched and
  cached). This is the mode a compliance department actually wants: their
  Entra ID or Okta, their MFA policy, their offboarding. The web app gets a
  sign-in page that runs the standard authorization-code flow with PKCE and
  keeps the token in memory, never `localStorage`.

Roles come later and stay small: `auditor` (run, chat, sign) and `viewer`
(read reports). The actor is what Steps 38 and 41 key on.

**Rejected.** *Clerk / Auth0 / Supabase Auth* — hosted identity contradicts
the "your evidence never leaves your network" claim the deployment story
rests on, and adds a vendor to a self-hosted product. *Local username +
password* — means owning password storage, reset flows and MFA; the `token`
mode covers the small-team case without any of that, and `oidc` covers the
enterprise case by delegating all of it. *Sessions in cookies* — the API is
consumed by a browser *and* by MCP clients and CI; a bearer token serves all
three, cookies serve one.

## 2. Session id, data storage, local or remote, many users (Step 41)

**Today.** A chat session is a `uuid4` minted by the API on the first turn,
mapped to an `AuditAgent` in a process dict. It is not tied to a user, it
dies with the process, it does not exist on a second worker, and the browser
holds the id in React state, so a reload loses the conversation. Audit runs
are the opposite: written to `reports/` as files and reloaded on start, but
owned by nobody.

**Decision.** Sessions become *records that belong to an actor*, stored where
the deployment already keeps durable state.

- `SessionStore` protocol (introduced in Step 36 with the in-memory
  implementation): `get(actor, session_id)`, `put(session)`,
  `delete(actor, session_id)`, `list(actor)`. A session is the serialized
  LangChain message list (`messages_to_dict`), the token counters, and
  timestamps; the `AuditAgent` is rebuilt from it per request, which is what
  makes the API stateless across workers.
- `PostgresSessionStore` in the `db` service compose already runs (pgvector
  image is Postgres 17): one table, `jsonb` messages, indexed by actor.
  Selected by `AUDITREADY_SESSION_STORE=memory|postgres` with the existing
  `AUDITREADY_PG_DSN`. Runs get an `actor` column the same way, and
  `GET /audit` lists only yours.
- The browser keeps the session id in `sessionStorage` (per tab, survives
  reload, gone when the tab closes) and offers "your conversations" from
  `list(actor)`.

**Rejected.** *Redis* — a second stateful service for data that Postgres holds
fine at this scale; Redis earns its place when limits need sharing across
processes (Step 38's note). *Sticky sessions at the load balancer* — hides
the problem until the pod restarts. *LangGraph checkpointer for chat* — the
v1 agent is LangChain, not LangGraph; the message list is the state,
and serialising it is a solved problem.

## 3. MCP — Microsoft Teams, and "if the GitHub MCP is not used, remove it; else use it fully" (Step 42)

**Today, the honest reading.** The GitHub connector *is* MCP and is live-tested
(`audit/mcp_evidence.py`, `scripts/smoke_mcp.py`, Step 22). Jira and Linear
are REST and GraphQL on purpose: their official MCP servers are OAuth-only and
cannot run headless (`audit/jira_evidence.py` docstring). So MCP is used,
but only as an *inbound* transport for one source. "Use it fully" has a
stronger meaning that the current build does not have.

**Decision.** Keep the GitHub connector, and add the missing direction:
**AuditReady as an MCP server.**

- `api/mcp_server.py` exposes the three engine tools (`check_ticket`,
  `find_evidence`, `coverage_report`) plus `list_packs` over MCP using the
  `mcp` package already in the tree (a dependency of
  `langchain-mcp-adapters`). Any MCP client can then ask "is TCK-1007
  compliant with 8.5.6?" and receive the cited verdict — Claude Desktop,
  Cursor, and — the point of the exercise — **Microsoft 365 Copilot agents in
  Teams**, which consume MCP servers through Copilot Studio. That is how the
  engine reaches the channel where approvals actually happen, without a
  Teams bot of our own.
- Authenticated with the Step 40 `token` mode; the same actor, the same
  limits, the same ledger.
- **Teams as an evidence source** (approvals said in a channel are
  evidence): a `TeamsSource` over Microsoft Graph, mapped onto the record
  contract like the other three. Needs an Entra app registration and a tenant
  to test against, so it starts as a `StubSource` with the reason string
  saying exactly that, the way `.eml` started.

**Rejected.** *Removing MCP* — it is live, tested and the one connector that
proves the protocol claim in the architecture story. *Rewriting Jira and
Linear onto MCP via community servers* — trades a headless connector we own
for an unofficial one we do not, for no new capability. *A Teams bot
(Bot Framework)* — a separate hosted app with its own registration and
messaging endpoint; an MCP server reaches Teams through Copilot and every
other client at once.

## 8. White paper (draft in `marketing/whitepaper/`, finish after Step 39)

The story that opens it is the one the reviewer asked for: audit preparation
done by hand — reading mailboxes for the client acknowledgement, matching SLA
timestamps against ticket closures, hunting for the approval that preceded a
change — and what it cost in weeks and in confidence. The paper then makes
the case the product makes: a summary is not evidence; a verdict without a
citation is not a finding; a tool that certifies is a liability and a tool
that drafts for a human is an asset.

Two constraints it keeps. **No employer or client data, names of projects,
or internal documents**: the story is about the *work*, told in the first
person, and `marketing/NUMBERS.md`'s rule stands — the experience informed
the design, the data did not. And **every number traces to a committed
file**, with its caveat carried beside it.

Structure: abstract · the manual audit (the story) · why summaries fail an
auditor · the evidence contract · the architecture in one page · what the
evaluation says and what it cannot · failure patterns and performance
(Step 39) · deployment and data boundary · what it is not · roadmap.

---

## Order of work and what each step must prove

| Step | What was proved, and how |
|---|---|
| 36 ✅ | `api/main.py` is 93 lines; the OpenAPI document came out byte-identical; the tests assign to `app.state` and name no private attribute. |
| 37 ✅ | `scripts/smoke_tracing.py`: one request id on the http line, the run line, the pair line and all three `llm.call` lines of the audit it started. Every row and report carries `elapsed_ms`. |
| 38 ✅ | Offline: the 31st chat turn in a minute is a 429 with `Retry-After`; an audit over budget is refused with the estimate in the sentence. Live: `scripts/smoke_spend.py` charges a real turn, then refuses the next one. |
| 39 ✅ | `scripts/measure_performance.py` on a warm process: 180 pairs in 5m29s, $0.118, p50 5.0s. In `marketing/NUMBERS.md`, a new site section and deck slide 17. It also **corrected a published number** — see below. |
| 40 ✅ | `AUDITREADY_AUTH=token` refuses a call without a token, names the actor with one, and gives a viewer a 403 on `/chat`. OIDC is verified offline against a key pair generated in the test: signature, issuer, audience and expiry each refused independently. The browser flow against a real issuer is the one piece still unproven. |
| 41 ✅ | `scripts/smoke_sessions.py` against the compose `db`: the store is **thrown away and rebuilt from the DSN** mid-conversation and the next turn still answers "TCK-1001" to "what did I just ask about?". |
| 42 ✅ | `scripts/smoke_mcp_server.py` launches `python -m api.mcp_server` as a subprocess, completes the MCP handshake and receives a cited verdict; an unscoped rollup comes back as an error. The Teams stub names the Entra consent work, not effort. |

### What Step 39 corrected

The measurement did what a measurement is for. **"Parks about 1 in 10" is only
true of the labelled evaluation set.** On a full unfiltered sweep of 20 records
against 9 controls the system parks 46 of 180 pairs — **25.6%** — because most
of those pairs are combinations nobody would have asked about. Both numbers are
real and they answer different questions, so both now appear wherever one did:
the headline tile on the marketing site carries the qualifier, and deck slide 17
answers the question before it is asked.

The second finding is that the slow tail is not ours. Three of 501 model calls
took 63–66s, all first attempts, no retries — a hosted endpoint stalling. p95
per pair is 12.1s with them and 11.6s without, and both numbers are published.

### What is still not done

- **The OIDC browser flow.** The API verifies a JWT from any issuer today, which
  is what an MCP client, CI or a script needs. A person signing in through a
  browser needs the authorization-code flow with PKCE, and that needs a client
  registration and an issuer to test against. The sign-in panel accepts a token
  in the meantime and says where it comes from.
- **A Teams connector.** Registered as a stub naming its prerequisite: an Entra
  app registration with admin consent, a tenant, and a retention decision.
- **Rate limits across processes.** The token bucket is per process. With two
  workers the limit doubles. The ledger and the session store are already
  shared; the bucket moves the same way when there is a second worker to share it with.

Commit policy is unchanged: one conventional commit per step, Gaurav decides
when to push. Everything above is in the working tree, uncommitted.
