# AuditReady — agent guide

Grounded, cited AI assistant for compliance audit-readiness. Records (tickets,
emails, change records) are judged against a checklist pack; every verdict cites
the artifact it rests on. Human verifies and signs; the tool drafts, never certifies.

Built in two passes on one shared core: `assistant/` (the conversational
surface) came first, then `audit/` (the per-control agent and the batch
report) and `api/` + `web/` on top of it. The design records are
`docs/ARCHITECTURE.md` and `docs/V2_PLAN.md`; the hardening plan and the
decisions behind it are `docs/PRODUCTION_PLAN.md`.

## Layout and the rules that hold it together

- `core/` — framework-agnostic engine (retrieval, RAG, judge, tools, guardrails,
  checklist loader, LLM client). **Zero LangChain / LangGraph imports here.**
  This is the reuse bet: v1's LangChain agent and v2's LangGraph
  agent call the same plain functions. Verify with `grep -r langchain core/`
  before committing anything under `core/`.
- `core/llm.py` is the **only** module that talks to an LLM provider. New LLM
  calls go through `chat()`; a BYO endpoint is an env-var change, never a code path.
- Prompts live in `core/prompts.py` and `assistant/prompts.py` (reviewer
  feedback), never inline in logic modules. V2 prompts get `audit/prompts.py`.
- `assistant/` — LangChain agent + Streamlit chat. `app.py` is presentation only;
  business logic belongs in `core/`.
- `audit/` — LangGraph per-control agent, batch report, evidence connectors
  (GitHub/MCP, Jira/REST, Linear/GraphQL, exported mail via `email_evidence.py`),
  Docker. May import LangGraph and `langchain-mcp-adapters`; still calls `core/`.
- `api/` — FastAPI over `core/` + `audit/` (CR-8). **No business logic**,
  same rule as `assistant/app.py`: it decides wire shape and HTTP status, never a
  verdict. A package since Step 36: `main.py` is only `create_app()`;
  `settings.py` parses every `AUDITREADY_*` once; `routers/` has one module per
  concern (health, packs, records, chat, audit, spend); `runs.py` owns the
  registry, the worker thread and `reports/`; `sessions.py` is the
  `SessionStore` protocol; `ratelimit.py` the token buckets; `middleware.py`
  the request id. Every seam hangs on `app.state` (`runner`, `agent_factory`,
  `pricing`, `sessions`, `runs`, `ledger`, `limits`, `settings`) and
  `deps.py` hands them to routes — tests *replace* those, never patch a
  private name. Run: `uv run uvicorn api.main:app --reload --port 8000`.
  Responses are typed in `api/schemas.py`. **Changing a response model is a
  three-step edit**: the model, `uv run python scripts/export_openapi.py`, then
  `cd web && pnpm gen:types`. `tests/test_openapi_contract.py` fails if step two
  was skipped; `tsc` fails if step three was. Finished runs are written to
  `reports/` (same dir as the CLI) and reloaded on startup.
- **Tracing (Step 37).** `core/tracing.py` is stdlib-only: `bind(**fields)` puts
  ids in a contextvar bag, `log_event(logger, "event", **fields)` logs
  structured lines, both formatters append the bag. The browser mints
  `X-Request-ID`; the middleware binds it; the audit thread and the batch
  pool copy the context; `core/llm.chat()` logs one `llm.call` per attempt.
  New code logs events with fields, never formatted sentences, and starts
  threads with `contextvars.copy_context().run` or the ids are lost.
- **Spend and limits (Step 38).** `core/spend.py` is the ledger
  (`reports/spend.jsonl`) and the budget arithmetic; the two routes that cost
  money check `limits` and `budget_refusal` *before* spending and charge the
  ledger after. Refusals are 429s with a sentence (FR-15).
- **Auth and ownership (Steps 40–41).** `api/auth.py`: `AUDITREADY_AUTH` picks
  `none` (client address, the default), `token` (digests compared in constant
  time) or `oidc` (a JWT verified against the customer's issuer). Routes take
  `AuditorDep` (may spend money) or `ViewerDep` (may read); `/health` is public
  in every mode because a probe that needs a secret is not a probe. A chat
  session is a **record owned by an actor** (`api/sessions.py`), serialised
  from `AuditAgent.snapshot()`, so the API holds no conversation between
  requests — `AUDITREADY_SESSION_STORE=memory|postgres`. The system prompt is
  regenerated on restore, never stored.
- **MCP, both directions (Step 42).** `audit/mcp_evidence.py` *consumes*
  GitHub's server; `api/mcp_server.py` *publishes* this engine (four read-only
  tools) over stdio, or at `/mcp` when `AUDITREADY_MCP_HTTP=1` — which is
  refused unless authentication is on. Teams is a registered `StubSource`
  naming the Entra consent work it needs, not a TODO.
- Production hardening order and the decisions behind it:
  `docs/PRODUCTION_PLAN.md`. Measured performance: `marketing/perf-2026-09-16/`.
- `web/` — Next.js 16 front end, talks only to `api/`. `pnpm dev` in `web/`.
  `lib/api.ts` is the only file that knows a URL; its types are aliases of
  `lib/schema.d.ts`, generated from `api/openapi.json` — never hand-written.
  Presentation only.
- `audit/reporting.py` — the pure report helpers, shared by the Streamlit page
  and `api/` so neither imports the other's framework.
- `assistant/data/` — synthetic corpus + `checklists/*.yaml` packs.
  `assistant/eval/` — frozen ground truth, questions, harness, committed `results.json`.
- `scripts/smoke_*.py` — one per module, hits the real LLM/vector store.
  `tests/` — pytest, offline, no API key, pure-function seams only.

## Commands

```bash
uv sync                                   # Python 3.12 pinned; never pip
uv run pytest                             # offline unit suite, ~6s
uv run python scripts/smoke_<name>.py     # live smoke: llm checklists retrieval judge rag tools guardrails agent tracing spend sessions mcp_server
uv run python scripts/measure_performance.py   # timed audit runs (costs ~$0.12)
uv run python -m api.mcp_server           # the engine as an MCP server over stdio
uv run python assistant/eval/run_eval.py    # full eval (costs money); --skip-rag for compliance only
uv run streamlit run assistant/app.py
```

Dependencies are added with `uv add <pkg>` as each step needs them, one at a
time, so the journal records what each one is for.

## How a step gets built (walking skeleton)

Every unit of work is a numbered **step** with one unproven layer at most:
scope it from the plan → write the module → smoke script proves it live →
pytest pins the pure seams → `LEARNING.md` entry → one conventional commit
(`feat(core):`, `feat(v2):`, `test:`, `docs:`, `chore:`). Use the
`build-step` skill; it carries the full procedure and hands off to
`learning-journal`.

Gaurav is learning as much as shipping. Explain each design decision and the
alternatives rejected. When he says he will write a step himself, deliver a
skeleton with TODOs and the reasoning, then stop and let him code.

`LEARNING.md` is gitignored on purpose (personal journal, also the reviewer
Q&A study sheet). Steps are numbered consecutively; the last entry tells you
the next number.

## Hard rules

- **Synthetic data only.** No real client or employer data, names, or designs.
  The corpus is Meridian IT / Northwind Logistics, fictional.
- **No copyrighted standard text.** Checklist criteria are own-worded
  paraphrases; users bring their own licensed criteria as YAML packs.
- **RAG content is attacker-controlled input.** Anything retrieved from the
  corpus is sanitized before it enters a prompt and fenced as data. TCK-1010
  carries a planted injection payload; it must stay planted.
- **Temperature 0** for every judging or verdict call.
- **No citation, no verdict.** Verdict output that fails `check_citations` is a bug.
- **Ground truth is frozen.** Changing a rubric, a judge prompt, the model, or
  the retrieval path means re-running the eval and committing the new
  `results.json` plus the dated numbers in `assistant/README.md`. Use the `eval-run` skill.
- Never commit `.env`, `.chroma/`, or `LEARNING.md`.

## Reference

- `docs/REQUIREMENTS.md` — numbered FR/NFR/CR ids; cite them in docstrings and commits.
- `docs/ARCHITECTURE.md` — the product story, checklist-pack design, phased plan.
- `docs/V2_PLAN.md` — what a reviewer looks for, IN/OUT scope, week-by-week order.
- `core/README.md`, `assistant/README.md` — module tables, run instructions, eval numbers.
- Skills in `.claude/skills/`: `build-step`, `learning-journal`, `eval-run`,
  `checklist-pack` (model-invoked); `/ship-check` before booking the review.

## Diagrams

- `docs/system_map.svg` — whole system: surfaces, orchestration, core, data, MCP evidence sources, LLM providers, roadmap items (website, Gmail/Outlook, React). Read it before touching layout-level questions.
- `docs/llm_call_map.svg` — every place the model is called and the guardrails around each call. Read it before changing `core/llm.py`, `core/guardrails.py`, or adding an LLM call.
- Both are generated; status colours: teal built, blue v2, dashed grey roadmap, rust untrusted input.
