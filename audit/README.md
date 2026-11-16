# audit/

**Same repo, second layer.** Reuses `core/` unchanged and adds what makes the
engine production-scale. Requirement ids are in `docs/REQUIREMENTS.md`
(CR-*); build order and scope cut are in `docs/V2_PLAN.md`.

## Built

| Module | What it is | Req |
|---|---|---|
| `graph.py` | LangGraph per-control agent: retrieve → judge → verify → finalize \| flag. One re-judge with verifier feedback, then a human-review flag. | CR-1 |
| `prompts.py` | Verifier prompt (wording only; logic lives in `graph.py`). | CR-1 |
| `batch.py` | Batch audit-readiness report over every ticket × control in a pack, 4-worker pool, error isolation, Markdown + JSON. CLI: `uv run python audit/batch.py --help`. | CR-2, FR-6 |
| `report_view.py` | Streamlit page: pack picker, scope, progress, per-control table, review queue, agent trace, export. Mounted by `assistant/app.py`. | CR-2, FR-6 |
| `eval/run_eval.py` | V2 eval harness: both packs through the agent; v1 metrics + review rate, review precision, error catch rate, finalized agreement. Writes `eval/results.json`. | CR-7, FR-18 |
| `connectors.py` | The evidence-source layer: the record contract and its validator, `JsonFileSource`/`CsvFileSource` file import, the source registry, the `.eml` stub, snapshot writer with provenance. CLI: `uv run python audit/connectors.py --help`. | CR-3 |
| `jira_evidence.py` | Jira Cloud over the REST API: Basic auth with an API token, `/search/jql` token paging, ADF → prose, comments read per issue. | CR-3 |
| `linear_evidence.py` | Linear over the GraphQL API: personal API key, Relay cursor paging, comments inline in one query. | CR-3 |
| `email_evidence.py` | Exported mail as evidence: `.eml` files, a directory of them, or an `.mbox`; HTML→text, stable `EML-` ids, ticket references found in subject/body and the reverse `linked_emails` edge written onto the ticket. `--emails-from` on the CLI. | CR-3 |
| `mcp_evidence.py` | `GitHubIssuesSource` - live GitHub Issues over the official MCP server (`langchain-mcp-adapters`), label→type/priority mapping with every guess recorded. | CR-3 |
| `../Dockerfile`, `../compose.yaml` | Self-hosted image: locked venv, embedding weights baked in, non-root, healthcheck; `compose.local-llm.yaml` overlays a bundled Ollama. | CR-4 |
| `../docker/entrypoint.sh` | First-boot corpus ingest into the `.chroma` volume, then exec Streamlit. | CR-4 |
| `eval/ground_truth_iso27001.yaml` | Answer key for the ISO 27001 pack (22 labelled pairs). The ISO 9001 key stays frozen in `assistant/eval/`. | CR-5 |

Second checklist pack: `assistant/data/checklists/iso27001.yaml`. Adding it
needed zero Python changes; the picker and the batch CLI found it by path.

## Evidence connectors (CR-3)

One interface, `TicketSource`: `name` and `fetch(limit) -> EvidenceRecords`.
Everything downstream - retrieval, the agent, the batch report - reads the
snapshot on disk, so a new source is a new `fetch`, not a new engine.

Three live sources over three transports, which is what makes the abstraction
claim checkable rather than asserted:

| Source | Transport | Auth | Module |
|---|---|---|---|
| `github` | MCP | token (or `gh auth token`) | `mcp_evidence.py` |
| `jira` | REST v3 | email + API token, Basic | `jira_evidence.py` |
| `linear` | GraphQL | personal API key | `linear_evidence.py` |
| `eml` | file (`.eml` / `.mbox`) | none - the human exported it | `email_evidence.py` |
| `teams` | **stubbed** | Entra app registration + admin consent | `connectors.py` (`STUBS`) |

```bash
# live: GitHub Issues over MCP, artifacts file-imported from the synthetic corpus
uv run python audit/connectors.py --source github --repo owner/name \
    --state CLOSED --limit 20 --out assistant/data/live \
    --artifacts-from assistant/data/corpus --ingest

# live: a Jira Cloud project (or any JQL)
uv run python audit/connectors.py --source jira --project AUD \
    --limit 20 --out assistant/data/live-jira --artifacts-from assistant/data/corpus

# live: a Linear team
uv run python audit/connectors.py --source linear --team AUD \
    --limit 20 --out assistant/data/live-linear --artifacts-from assistant/data/corpus

# file import: JSON or CSV in the same contract shape
uv run python audit/connectors.py --source csv --path tickets.csv --out assistant/data/live

# exported mail added to any snapshot: Gmail Takeout .mbox, Outlook/Apple Mail .eml
uv run python audit/connectors.py --source json --path assistant/data/corpus \
    --emails-from assistant/data/mail-sample --out assistant/data/live-mail
```

Don't have Jira or Linear credentials yet? One script walks the whole thing -
site, project, token, workspace, key - and verifies each as it goes:

```bash
bash scripts/setup_connectors.sh
```

Fresh tenants are empty. Seed both with the same kind of synthetic evidence the
corpus has - an approval in a comment, a linked `CHG-`/`EML-` id in prose, a
request with no approval at all, and the planted injection string - so a live
snapshot exercises the same paths the eval does. Idempotent by title; issues
are created closed, because open tickets fail "approved before closure" for the
wrong reason:

```bash
uv run python scripts/seed_demo_evidence.py --jira KAN --linear MER
```

Or prove existing credentials before spending a fetch:

```bash
uv run python scripts/smoke_mcp.py --repo owner/name
uv run python scripts/smoke_jira.py --project AUD
uv run python scripts/smoke_linear.py --team AUD
```

**Why Jira and Linear are not over MCP.** Both ship official MCP servers and
both are OAuth-only, which means a browser consent flow against a provisioned
tenant. A connector that needs a human at a keyboard cannot run in CI, cannot
run unattended, and cannot be smoke-tested - so these two use the REST and
GraphQL APIs, which take a token. The MCP claim is carried by GitHub, where it
is true; asserting it here as well would be false.

The contract gate is the point. Every source's output passes `validate_records`
before a byte is written, so a mapping drift fails at the boundary with the
offending field named, not three layers later inside a judge prompt.

**What GitHub cannot supply, and what this does about it.** Issues have no
priority field, no ticket type, no resolution statement and no approval
record. So: type and priority are inferred from labels and every fallback is
listed in `provenance.json` under `inferred`; `resolution` carries GitHub's own
closure facts (`state_reason`, who closed it, when) and says plainly that the
source has no narrative; `signoff` stays `None`, because closing an issue is
not an approval. Nothing is invented to fill a field.

Run against `langchain-ai/langgraph` (4 closed issues × 2 ISO 9001 controls),
the report scored 0% readiness and every gap cited the missing `signoff` or the
absent linked records - the honest answer for a source with no approval trail.
That is the connector working, not the agent failing.

Snapshots are gitignored (`assistant/data/live/`): they are third-party content,
regenerable, and the frozen evaluation runs on the synthetic corpus only.

## Self-hosted deployment (CR-4)

```bash
cp .env.example .env          # fill in a key, or point at your own endpoint
docker compose up             # http://localhost:8501
```

One container and one named volume. The image is Python 3.12-slim with the
locked venv copied from a builder stage, runs as uid 10001, and has a
healthcheck on Streamlit's own `/_stcore/health`. The entrypoint ingests the
corpus on first boot if the `.chroma` volume is empty, then execs Streamlit -
so a fresh `docker compose up` reaches a working UI with no manual step.

**Nothing is fetched at run time.** The embedding model
(`all-MiniLM-L6-v2`, ~90 MB) is baked into the image at `/opt/hf` and
`HF_HUB_OFFLINE=1` is set, so embedding works with the network unplugged.
That matters for CR-4: the only outbound request the container makes is the
LLM call, and that address is yours to set.

**BYO endpoint.** `core/llm.py` speaks one wire protocol - OpenAI's - so any
compatible server works without a code change:

```bash
AUDITREADY_LLM_BASE_URL=http://ollama:11434/v1
AUDITREADY_MODEL=llama3.1:8b
docker compose -f compose.yaml -f compose.local-llm.yaml up   # bundles Ollama
```

Off OpenRouter no key is needed (the client sends a placeholder) and the live
cost display goes quiet, because OpenRouter's pricing API does not know your
model. Both are deliberate: a missing price is honest, an invented one is not.

**Verified 2026-09-03** on the built image (`auditready:0.1.0`, 2.08 GB):
healthy in ~35s from cold, `/` served 200, ingested 89 chunks offline, one
real ticket × control pair judged end to end inside the container
(3,200 prompt + 259 completion tokens), `AUDITREADY_LLM_BASE_URL` resolving to
a non-OpenRouter host with no key present, and neither `.env` nor
`LEARNING.md` in the image.

## Evaluation (2026-09-11, gpt-4o-mini, temperature 0)

`uv run python audit/eval/run_eval.py` - every labelled pair of both packs
through the LangGraph agent. Committed evidence: `eval/results.json`, which
stamps the model that produced it.

Committed run, with the range observed over the two runs on these packs:

| | ISO 9001 (46 pairs) | ISO 27001 (22 pairs) | pooled (68) |
|---|---|---|---|
| exact agreement | 0.935 | 0.955 | 0.941 |
| gap recall | 1.0 | 1.0 | **1.0 in both runs** |
| flagged recall (any deficiency noticed) | 1.0 | 1.0 | **1.0 in both runs** |
| false-positive rate | 0.032 (0.032-0.065) | 0.067 | 0.043 (0.043-0.065) |
| citation validity | 0.935 (0.913-0.935) | 0.909 (0.909-1.0) | 0.926 (0.926-0.941) |
| review rate (parked for a human) | - | - | 0.103 (0.088-0.103) |
| **agreement on what it finalized** | - | - | **0.984 (0.952-0.984)** |
| deficiencies finalized as compliant | 0 | 0 | **0 in both runs** |

**Why a range, and why these numbers are lower than the ones this file used to
carry.** The committed figures were 0.956 pooled exact agreement and 0.022
false positives from a single run on 2026-09-03. Re-running on 2026-09-11 with
no code change produced 0.926 and 0.065. Seven runs across the two dates showed
why: one pair, TCK-1004 against A.8.32, was satisfied on 09-03 and deficient in
all four runs on 09-11. The corpus, the rubrics and the answer key are frozen.
`gpt-4o-mini` is not, and it moved. The 09-03 run turned out to be the most
favourable of the seven, which is not a claim worth publishing as a point
estimate.

v1's single-call judge reproduced to three decimals over the same window,
so the harness is sound; the difference is call count. This agent runs
decompose, judge, verify and sometimes re-judge, three to five model calls per
pair against v1's one, and every extra call is another chance to diverge.
The verify loop does not buy accuracy. It buys the review queue, and it buys
variance.

**Two rubric holes came out of the same exercise, and both were the worst kind
of failure**: a planted deficiency *finalized* as compliant, with the verifier
agreeing and nothing in the review queue. It happened in three of the seven
runs. A.5.27 accepted a fleet-wide patch rollout as a preventive measure when
the rollout is the remedy; 8.2.1 accepted an outbound "the work is done" email
as client acknowledgement. Both are now closed in the rubric text (iso27001
pack 0.1.2, iso9001 pack 0.1.1) and both post-fix runs show zero such misses,
with exact agreement identical across them. One paragraph of YAML each, no
Python - the config-as-data claim doing real work on a real defect.

**Read the last three rows, not the first.** On ISO 9001 the plain judge scores
0.957 against this agent's 0.935. Earlier runs reversed that ordering more than
once, and an earlier version of this file claimed a +0.021 win from the verify
loop that a clean re-run did not reproduce. Withdrawn - and seven runs have
since supplied the mechanism. The agent spends three to five model calls per
pair where the plain judge spends one, so it inherits more variance; v1
reproduced to three decimals over the same eight days that moved nine of this
agent's pairs.

What holds is the thing the loop was actually built for. The agent declines to
finalize about a tenth of pairs, and **agreement over the pairs it does
finalize is 0.984** against 0.941 raw. It is not a more accurate judge than the
plain one; it is a judge that knows which of its own verdicts to stand behind,
which is the property an audit needs.

Gap recall is 1.0 in the committed run and in both post-fix runs. In one of
seven runs it was 0.944 pooled, on TCK-1007 against change control, where the
judge correctly saw the approval postdate the deployment and then graded it
partial instead of gap; the verifier disagreed and the pair was parked, so the
deficiency reached a human rather than a report. The floor stays at 1.0 with no
band, because a planted deficiency surviving to audit day is the failure this
whole system exists to prevent.

Residual disagreements: one on ISO 9001 finalized as a verdict (TCK-1017,
partial labelled where the agent said gap - v1 misses it too), the rest
parked rather than answered wrongly. Temperature 0 is not determinism; treat
any single-run swing under ~0.03 as noise, which is why `eval/thresholds.yaml`
sets floors instead of comparing runs.

What the eval changed (see the three `fix`/`data` commits before it):
verifier states its own level with per-condition checks; a verdict that
flips under review is parked, never finalized; linked change/email records
are joined by id, not left to similarity retrieval.

## Planned (see V2_PLAN.md)

- A recorded walkthrough of the batch report
- Live Gmail / Microsoft Graph mailbox connectors - the `.eml`/`.mbox` import
  is built; a live connector waits on the consent and scope design the README's
  ethics section describes
- A source that carries real approval records (Jira Service Management,
  ServiceNow), since `signoff` is `None` on every live ticket today

Out of scope this round: Gmail/Outlook, pgvector. The React dashboard (CR-8)
landed on 2026-09-07 as `api/` + `web/` - see `docs/V2_PLAN.md` for why it was
OUT, why that changed, and the rule that kept the engine untouched.
