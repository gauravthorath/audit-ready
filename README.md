# AuditReady

**A grounded, cited AI assistant for compliance audit-readiness.**


Point it at a project's records - tickets, change records, emails - and it checks
them against a compliance checklist (ISO 9001 and ISO 27001 ship with it), says
which controls are **satisfied**, which have **gaps**, and drafts the missing
evidence. Every claim carries a citation back to a real source artifact, and a
verdict with no citation is not returned at all. A human reviews and signs.

*Built in two passes on one shared core: a conversational assistant first,
then the batch audit engine and the web application on top of it.*

---

## The problem

An ISO audit is mostly an evidence hunt. Someone senior spends a week before the
auditor arrives opening tickets one at a time, asking *does this show we do what
our procedure says?*, and writing it up. The records exist; the reading is the
work. It is slow, it is inconsistent between reviewers, and it is done under time
pressure by the person who can least afford the week.

The temptation is to have an LLM summarise it all. That is the wrong shape: an
audit finding that cannot be traced to a specific record is worth nothing to an
auditor, and a confident wrong verdict is worse than no verdict. So the design
constraint came first - **no citation, no verdict** - and the product was built
behind it.

## How it works

```
records ──► retrieve ──► judge ──► verify ──► satisfied / gap / park for a human
 (tickets,   (vector DB   (per-      (second        │
  changes,    + id join)   control)   opinion)      └─► every verdict cites the
  emails)                                                artifact it rests on
```

- **Retrieval** - the corpus is chunked and embedded (`all-MiniLM-L6-v2`, run
  locally) into a vector store: **Chroma** (embedded, the default) or
  **Postgres + pgvector** (`AUDITREADY_VECTOR_STORE=pgvector`; one database to
  self-host, HNSW index, a hosted Postgres works with just its DSN). Same
  chunks, same vectors, same similarity numbers - `scripts/smoke_pgvector.py`
  checks the two return identical rankings. A control is decomposed into several
  queries rather than one, and records the ticket *links* to (change records,
  emails) are joined **by id**, not left to similarity search.
- **Judging** - one control at a time, against a rubric written as data
  (`assistant/data/checklists/*.yaml`). Temperature 0.
- **The agent** - `audit/graph.py` is a **LangGraph** state machine:
  retrieve → judge → verify → finalize, with one re-judge if the verifier
  objects and a **park-for-human-review** exit if the verdict flips under
  review. v1's conversational agent (`assistant/agent.py`) is LangChain
  tool-calling over the same `core/` functions.
- **Evidence connectors** - `audit/connectors.py` defines one interface
  (`TicketSource`) and one shape gate (`validate_ticket`); three live sources
  sit behind it over three different transports, which is what makes the
  abstraction claim checkable rather than asserted:
  **GitHub Issues over MCP** (`audit/mcp_evidence.py`, via
  `langchain-mcp-adapters` against GitHub's official MCP server),
  **Jira Cloud over REST** (`audit/jira_evidence.py`), and
  **Linear over GraphQL** (`audit/linear_evidence.py`).
  Jira and Linear use their REST/GraphQL APIs rather than their official MCP
  servers for one reason: both MCP servers are OAuth-only, so they need a
  browser and a human and cannot be smoke-tested unattended. The MCP claim is
  carried by GitHub, where it is true. CSV/JSON import works offline, and
  **exported mail** (`.eml` / `.mbox`, `audit/email_evidence.py`) is imported
  as correspondence and linked to the tickets it names - any provider, because
  every provider exports one of those two formats. Live mailbox connectors are
  deferred for the reason in the ethics section below.
- **Deployment** - `docker compose up` self-hosts the whole thing. The
  embedding model is baked into the image, so the vector store is built with no
  network at all. The LLM is a **BYO endpoint**: point `AUDITREADY_LLM_BASE_URL`
  at vLLM, Ollama, or an internal gateway and no record ever leaves the
  customer's boundary.

## Quick start

```bash
cp .env.example .env      # add OPENROUTER_API_KEY
uv sync
uv run streamlit run assistant/app.py     # chat + batch report, two pages
```

The same two surfaces as a React app (CR-8) - the engine unchanged, an HTTP
boundary in front of it:

```bash
uv run uvicorn api.main:app --reload --port 8000     # api/: FastAPI over core/ + audit/
cd web && cp .env.local.example .env.local && pnpm install && pnpm dev   # http://localhost:3000
```

Self-hosted, or with a local model instead of a hosted API:

```bash
docker compose up                                              # web :3000, api :8000, streamlit :8501
docker compose up web api                                      # just the React app and its API
docker compose -f compose.yaml -f compose.local-llm.yaml up     # + Ollama, every judging call stays inside
```

One Python image serves both `api` and the Streamlit `auditready` service (the
command differs); `web/` builds its own from `web/Dockerfile`. Finished audits
are written to `./reports/` on the host, and the API lists them on startup - a
run survives a container restart, and a report made from the CLI shows up in
the browser.

Pull evidence from a live source instead of the synthetic corpus
(`scripts/setup_connectors.sh` walks you through Jira/Linear credentials the
first time):

```bash
bash scripts/setup_connectors.sh                      # first time only
uv run python audit/connectors.py --source jira --project AUD \
    --out assistant/data/live-jira --artifacts-from assistant/data/corpus \
    --emails-from ~/exports/northwind.mbox          # exported mail, any provider
```

A batch report from the command line:

```bash
uv run python audit/batch.py --pack assistant/data/checklists/iso27001.yaml
```

Tests and evaluation:

```bash
uv run pytest                          # 297 offline tests, no API key, <6s
uv run python audit/eval/run_eval.py       # the agent vs both answer keys
uv run python audit/eval/gate.py           # the regression gate CI runs
```

## Layout

| Path | What lives there |
|---|---|
| `core/` | The engine: retrieval, RAG, the three domain tools, guardrails, checklist loading, the single LLM client. **Framework-agnostic** - no LangChain or LangGraph import anywhere in it. |
| `assistant/` | LangChain tool-calling agent + the Streamlit UI. The v1 deliverable. Also holds the synthetic corpus, the checklist packs, and the frozen answer key. |
| `audit/` | LangGraph agent, batch report, evidence connectors, the eval harness and its regression gate, the report UI page. Reuses `core/` unchanged. |
| `api/` | FastAPI over `core/` + `audit/`: packs, records, chat sessions, background audit runs with SSE progress, runs persisted to `reports/`. One router per concern, every seam on `app.state`. Authentication is `none`, a bearer token or the customer's OIDC issuer (`api/auth.py`); conversations are records owned by an actor, in memory or Postgres (`api/sessions.py`); the engine is also published as an MCP server (`api/mcp_server.py`). Every request carries a correlation id (`X-Request-ID`) from the browser to each model call; the two routes that spend money are rate-limited and budget-capped, with today's spend at `GET /spend`. Typed responses (`api/schemas.py`) → `api/openapi.json` → TypeScript types in `web/`; `tests/test_openapi_contract.py` fails when the committed document is stale. Holds no business logic - the same rule as `app.py`. |
| `web/` | Next.js front end (report, chat, records). Talks only to `api/`. Design tokens match the deck. See `web/README.md`. |
| `docs/` | `ARCHITECTURE.md` (concept and design), `REQUIREMENTS.md` (numbered FR/NFR/CR ids), `V2_PLAN.md` (scope cut and build order), `V2_PLAN.md` (the React front end: why, what's built, what's next), `PRODUCTION_PLAN.md` (the post-review hardening: tracing, limits, auth, sessions, MCP - decisions and order), `DEMO.md` (the demo script). |
| `tests/`, `scripts/` | Offline unit tests; one live smoke script per module. |

**The one rule that makes this work:** `core/` stays framework-agnostic, so
v1's LangChain and v2's LangGraph call the *same* plain
functions. That is what let v2 extend v1 instead of rewriting it.
(REQUIREMENTS.md FR-7 / NFR-3, and CI enforces it.)

## Evaluation

Two harnesses, both with committed results. Numbers below: **2026-09-11**,
`openai/gpt-4o-mini` as the system model, temperature 0, stamped into each
results file. Where a range is given it is the spread observed over repeated
runs of the same code, because a single run turned out not to be a baseline.

**V2 agent vs both answer keys** (`audit/eval/results.json`, 68
labelled ticket × control pairs):

| | ISO 9001 (46) | ISO 27001 (22) | pooled (68) |
|---|---|---|---|
| exact agreement | 0.935 | 0.955 | 0.941 |
| gap recall | 1.0 | 1.0 | 1.0 in both runs |
| flagged recall (any deficiency noticed) | 1.0 | 1.0 | 1.0 in both runs |
| false-positive rate | 0.032 | 0.067 | 0.043 (0.043-0.065) |
| citation validity | 0.935 | 0.909 | 0.926 (0.926-0.941) |
| parked for a human | - | - | 0.103 (0.088-0.103) |
| agreement on what it finalized | - | - | 0.984 (0.952-0.984) |
| deficiencies finalized as compliant | 0 | 0 | 0 in both runs |

**Why these are lower than this file used to claim.** The previous numbers,
0.956 pooled agreement and 0.022 false positives, came from one run on
2026-09-03. Re-running the identical code on 2026-09-11 gave 0.926 and 0.065.
Seven runs across the two dates found the cause: one pair, TCK-1004 against
A.8.32, was satisfied on 09-03 and deficient in all four runs on 09-11. The
corpus, the rubrics and the answer key are frozen and committed. The model is
not, and cannot be - it is served, not installed. The 09-03 run was the most
favourable of the seven, which is not something to publish as a point estimate.

**A deficiency reached a report.** In three of those seven runs a planted
deficiency was *finalized* as compliant, with the verifier agreeing and nothing
in the review queue. Both causes were rubrics that said what counts without
saying what does not: A.5.27 accepted a fleet-wide patch rollout as a
preventive measure when the rollout is the remedy, and 8.2.1 accepted an
outbound "the work is done" email as client acknowledgement. Both are closed in
the rubric text (iso27001 pack 0.1.2, iso9001 pack 0.1.1). Both post-fix runs
show zero such misses and identical exact agreement. The fix was YAML, not
Python, which is the first time the config-as-data claim earned its keep on a
real defect.

**What the verify loop is and is not worth.** On ISO 9001 the plain judge
scores 0.957 and the agent 0.935. Across earlier runs the ordering reversed
more than once, and an earlier draft of this README claimed a +0.021 win for
the verify loop that a clean re-run did not support. That claim is withdrawn,
and repeated runs have since shown why it could never have been safe: the agent
takes three to five model calls per pair against the plain judge's one, so it
carries more variance, not less. v1's single-call judge reproduced to
three decimals across eight days while the agent moved nine pairs.

What survives is narrower and more useful. The agent declines to finalize about
a tenth of pairs, and **agreement over the pairs it does finalize is 0.984**
against 0.941 raw. It is not a more accurate judge; it is a judge that knows
which of its verdicts to stand behind, which is the property an audit needs.

**Where gap recall actually sits.** It is 1.0 in the committed run and in both
post-fix runs, and it was 0.944 pooled in one of seven runs, on TCK-1007
against change control: the judge saw that approval came after deployment and
then graded it partial rather than gap, the verifier said gap, and the pair was
parked for a human. Nothing slipped through, and the metric still counts it as
a miss. The floor stays at 1.0 with no tolerance band.

**Answer quality** (`assistant/eval/results.json`, 12 labelled questions,
RAGAs-equivalent metrics computed directly): faithfulness **0.977**, answer
correctness **0.65**, source recall **0.945**, citation presence **1.0**.

The caveats, because they matter more than the numbers:

- **The grader is a different model from the system.** Answer quality is graded
  by `google/gemini-2.5-flash` while the system runs on `gpt-4o-mini`. When
  `gpt-4o-mini` graded its own answers, correctness read 0.792; a foreign grader
  put it at 0.683 on the same answers, and reads 0.65 today. That ~0.11 gap was
  self-grading bias, and the lower number is the honest one. The grader is a config seam (`AUDITREADY_JUDGE_MODEL`), not a
  hard-coded choice.
- **Temperature 0 is not determinism.** Between two otherwise identical runs,
  ISO 9001 agreement moved by 0.021 in one direction for the agent and the same
  amount the other way for the plain judge. Any single-run difference smaller
  than about 0.03 on these set sizes should be read as noise, which is why the
  gate below sets floors rather than comparing runs to each other.
- **Gap recall of 1.0 is the metric that must not move.** A missed gap is a
  false assurance handed to an auditor; `audit/eval/gate.py` floors it at
  1.0 with no tolerance band, and gives every other metric a small one.
- **68 labelled pairs is a small set,** hand-labelled by one person over a
  synthetic corpus. It catches regressions; it does not establish accuracy in
  the field.

## Ethics and data handling

**The corpus is synthetic.** Every ticket, change record and email in
`assistant/data/corpus/` was written for this project. No client, employer or
third-party record appears anywhere in the repository or its history - including
the deficiencies, which are planted deliberately so the evaluation has gaps to
find. Checklist rubrics are own-worded paraphrases of what a control asks for,
not copied standard text; ISO standards are copyrighted and are not redistributed
here.

**A human decides.** The system produces a draft finding and never a signature.
Verdicts that fail the verifier's second opinion are *parked* rather than
finalized (8.8% of pairs in the last run), and the report UI has a review queue
for exactly those. The agent's whole trace - what it retrieved, what it judged,
what the verifier said - is exportable, so a reviewer can disagree with the
reasoning and not just the answer.

**Records are treated as untrusted input.** A ticket comment is data, never an
instruction. `core/guardrails.py` sanitises every retrieved record, quarantines
injection attempts, and the UI raises a banner when it fires; the corpus
contains a planted attack (TCK-1010, an "IGNORE ALL PREVIOUS INSTRUCTIONS"
payload in a pasted vendor changelog) so this path is exercised on every eval
run. Chat input is validated and control characters stripped before it reaches
the model.

**Data stays where the customer puts it.** The self-hosted image makes no
outbound call except to the configured LLM endpoint, and that endpoint is
swappable to something inside the customer's own network. Live snapshots pulled
from a connector are written to disk with a provenance stamp and are
**gitignored** - third-party content is not committed to this repository.

**Mailbox connectors are deliberately not built; mail import is.** Gmail and
Outlook would be the richest evidence source. They are also a mailbox: unrelated
personal correspondence, other people's data, and an OAuth scope that grants far
more than an audit needs. A live connector needs a data-protection review and a
scope-minimising design, not an afternoon, so it sits in "What's next". What is
built is the consent-shaped version of the same idea: `audit/email_evidence.py`
imports exported mail - `.eml` files or an `.mbox`, which is what Gmail Takeout,
Outlook, Apple Mail and Thunderbird all produce - so a human chooses exactly which
threads constitute evidence and nothing else ever enters the system. Imported
threads are joined to the tickets they name and handed to the judge as linked
records, the same as any other correspondence in the corpus.

## Status

- [x] `core/` - retrieval, RAG, tools, guardrails, checklists, LLM client
- [x] `assistant/` - LangChain agent + Streamlit chat, evaluated (`assistant/README.md`)
- [x] `audit/` - LangGraph agent, batch report, second standard, eval harness + CI gate, three evidence connectors (GitHub/MCP, Jira/REST, Linear/GraphQL), self-hosted Docker with BYO endpoint (`audit/README.md`)
- [x] `api/` + `web/` - FastAPI boundary and a Next.js front end over the same core, types generated from the OpenAPI document (CR-8)
- [x] Production hardening (`docs/PRODUCTION_PLAN.md`): correlation-id tracing end to end, per-caller rate limits and a spend ledger, authentication (none / token / OIDC), conversations owned by a user and stored in Postgres, the engine published as an MCP server

## What's next

Deliberately out of scope for this build, and why:

- **Gmail / Outlook evidence.** The privacy work above has to come first.
- **Approval records from a source that has them.** Jira Service Management
  and ServiceNow carry real approval workflows; GitHub, base Jira and Linear do
  not, so `signoff` is None on every live ticket and the audit reports that as
  an evidence gap. Reading a source that *has* approvals is the next thing that
  would change a verdict rather than just widen coverage.
- **Hybrid BM25 retrieval.** pgvector is in (2026-09-09); a lexical leg
  beside it is the next retrieval change, and it needs the eval re-run.
- **ISO 20000 / CMMI packs.** Two standards already prove the config-as-data
  claim; a third is a YAML file and a domain expert's afternoon.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security issues: [SECURITY.md](SECURITY.md).

## Licence

Licensed under the [Apache License, Version 2.0](LICENSE). Copyright 2026
Gaurav Thorat.

[`NOTICE`](NOTICE) records what the copyright does *not* cover: the checklist
packs are own-worded paraphrases of clause intent written for this project, not
the text of ISO 9001 or ISO 27001, and the corpus is synthetic. AuditReady
drafts findings for a human to verify and sign - it does not certify compliance.
