# v2 - the Next.js front end

**Status (2026-09-07): started.** `api/` and `web/` exist, build clean, and are
tested offline. Decision taken the same day: build now, keep `assistant/app.py`
as the fallback until `web/` is demo-ready, and present on whichever is green
on Thursday. The section below records why this was *not* the original plan,
so the trade-off stays visible.

This document exists so the decision is written down rather than re-argued.

## The rule for this week

V2 review is **Thursday 10 September 2026**, and Friday is the last
day to book one. The original rule was: nothing here gets built until the review
is done. `V2_PLAN.md` put a React dashboard in **OUT**, the deck defended
that cut, and a UI rewrite three days out would have replaced a working demo
with a half-built one.

That rule was overridden on 2026-09-07, on purpose and with the trade-off
understood: CR-8 and `ARCHITECTURE.md` §14 always specified "FastAPI backend +
a light React front end", a React UI had been promised, and UI quality is a
scored rubric line. So the rule became narrower rather than gone:

- **The engine is untouched.** `core/`, `audit/`, the eval, the connectors -
  nothing under them changed for this. `api/` and `web/` are additions.
- **`assistant/app.py` stays until `web/` is demo-ready**, and is the fallback on
  the day. Whichever is green at 09:00 Thursday is what gets presented.
- **No live React call goes into the timed demo** unless it has been rehearsed
  against the clock the day before.

## The idea this replaces

The first sketch was "keep the Python work in a folder beside `audit/` and
then change it into Next.js". That does not work, and it is worth saying why
rather than discovering it later:

- **Streamlit and Next.js share no code.** Streamlit *is* the Python - `st.`
  calls interleaved with logic. There is nothing to port; a `.py` file does not
  become a `.tsx` file by moving directories.
- **Copying `assistant/` sideways creates two truths.** Two copies of the UI, one
  of them stale within a week, and `CLAUDE.md`'s rule that business logic lives
  in `core/` gets quietly broken in whichever copy is edited under pressure.
- **The actual missing piece is not a front end. It is an HTTP boundary.**
  Streamlit calls `core/` functions in-process. A browser cannot. Until
  something serves JSON, there is nothing for React to render.

So: nothing moves and nothing is copied. `assistant/` stays exactly where it is
and keeps working. v2 *adds* two directories.

## The shape

```
core/        unchanged - still framework-agnostic, still no LangChain
audit/    unchanged - agent, batch, connectors, eval
assistant/     unchanged - the Streamlit app keeps working, forever if it likes
api/         NEW  FastAPI over core/ + audit/. The HTTP boundary.
web/         NEW  Next.js app. Talks only to api/. No Python knowledge.
```

The one rule that makes this safe is the rule that already made v2
cheap: **`api/` holds no business logic.** It is the same constraint
`assistant/app.py` lives under, applied to a different transport. If a verdict is
computed in `api/`, the refactor has gone wrong.

## Done on 2026-09-07, after the structure review

- **Docker.** `compose.yaml` runs `web`, `api` and the Streamlit `auditready`
  from two images; `compose.local-llm.yaml` points the API at Ollama too.
  `web/Dockerfile` (Next standalone) **builds**. The Python image build is
  **unverified on this machine**: the host disk hit 100% mid-build and the
  daemon went read-only; after a Docker Desktop restart and a 9.9 GB cache prune
  there is ~9 GB free, which is not enough headroom to build a torch image
  safely. Verify with `docker compose build api` once the disk has room.
- **Runs persist.** Finished audits are written to `reports/` with
  `v2.batch.write_report`, the same files the CLI writes, and reloaded on
  startup. CLI runs appear in the browser; API runs appear on disk.
- **One contract.** Pydantic response models (`api/schemas.py`) →
  `api/openapi.json` → `web/lib/schema.d.ts` via `openapi-typescript`.
  `tests/test_openapi_contract.py` fails when the JSON is stale; `tsc` fails
  when the front end disagrees with it.
- **shadcn/ui (2026-09-08).** Adopted for primitives only - Select for the
  pack picker, Tabs for the report, Button everywhere - with its semantic
  variables bound to the existing deck tokens. Done before the review, against
  the earlier "after Thursday" call, because Gaurav wanted the team's feedback
  on it; scoped to an hour. The reviewer surface (dialogs, command menu) will
  pull the rest of the kit from the same install.
- **pgvector (2026-09-09).** v1's DP-1 said "pgvector in v2";
  it is now a second store behind `core/retrieval.search()`, selected by
  `AUDITREADY_VECTOR_STORE`, with a `db` service in compose. Parity smoke:
  identical rankings to Chroma on the corpus. Chroma remains the default for
  the review because the eval was frozen on it.
- **Not done, on purpose:** renaming `assistant/` and `audit/` to domain
  names. Too much blast radius three days before the review - imports, tests,
  docs, the deck, CI, the Dockerfile. After Thursday, as one commit.

## Order of work

Each step is independently useful, and each leaves the Streamlit app running.

1. **`api/` - read-only endpoints first.** `GET /packs`, `GET /records`,
   `GET /report/{run_id}`. No LLM calls, no writes. This is where the response
   schemas get pinned, and it is testable offline with the existing fixtures.
2. **`api/` - the two expensive operations.** `POST /chat` and `POST /audit`.
   Both are long-running: an audit is hundreds of model calls. They return a
   run id immediately and stream progress (SSE is enough; WebSockets are not
   needed for one-way progress). The Streamlit progress bar already proves the
   UX - a silent spinner reads as hung.
3. **Contract tests against `api/`.** The same discipline as
   `audit/connectors.py`: a response-shape gate, so a field rename fails in
   CI rather than in a browser.
4. **`web/` - the report surface only.** The batch report is the strongest
   screen and the least interactive: score, per-control table, open gaps,
   review queue, agent trace. Ship this before touching chat.
5. **`web/` - the reviewer surface.** This is the part that justifies the whole
   exercise, and it is the one thing Streamlit genuinely cannot do well: the
   parked queue with assignment, an audit trail of who accepted what, and a
   diff against the previous run. `08-Next` in the deck already promises it.
6. **`web/` - chat.** Last, because it is the screen Streamlit does adequately.
7. **Retire the Streamlit app, or don't.** It is a fine internal tool. Keeping
   both is only a cost if they diverge, and they cannot diverge if neither holds
   logic.

## Why the reviewer surface is the point

Streamlit is honest about being a demo surface. What it cannot be is a thing a
compliance manager uses on a Tuesday: no per-user state, no assignment, no
"who signed off on this finding", no diff between this quarter's run and last
quarter's. Those are not styling problems. They are the reason to build a real
front end, and they are worth more than making the existing screens prettier.

If v2 ends up being a nicer-looking version of the same three screens, it was
not worth building.

## What this does not include

- **Auth and multi-tenancy.** Needed before anything is hosted, out of scope
  for a front end. The BYO-endpoint work in `core/llm.py` is the self-host
  story; a hosted offering needs per-user key storage designed first
  (`README.md`, "What's next").
- **A marketing site.** Still out, for the reason already on the record: it
  sells something that is not meterable yet.
- **Mailbox evidence.** Unchanged - it needs a privacy review, not a UI.

## Rough size

Steps 1-4 are the honest minimum for something demonstrable: an API with pinned
schemas, contract tests, and one real screen. That is not an afternoon, and
pretending otherwise is how the Streamlit app ends up half-replaced. Steps 5-6
are where the actual product value is.

No estimate is written here on purpose. The last time this project put a number
next to a claim without measuring it, the number was wrong and had to be
withdrawn in public - see the Challenges slide.
