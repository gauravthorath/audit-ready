---
name: ship-check
description: Pre-release checklist for AuditReady (fresh clone, tests, README, ethics, secrets).
disable-model-invocation: true
---

# Ship check

Run before cutting a release or sharing the repository, and before the
review. Report every item as pass / fail with the evidence; fix only what the
user asks to fix.

## Steps

1. **Fresh clone install.** `git clone` into the scratchpad, `cp .env.example .env`
   with the real key, `uv sync`, `uv run pytest`, `uv run python scripts/smoke_llm.py`.
   Pass when all succeed from a clean tree.

2. **Every smoke script passes** live, including v2 ones. List each with its result.

3. **Eval is current.** `results.json` commit date is after the last change to
   any prompt, rubric, model default, or retrieval code
   (`git log -1 --format=%cd -- core/prompts.py core/judge.py core/rag.py assistant/data/checklists`
   versus `-- assistant/eval/results.json`). README numbers match the file.

4. **Secrets and derived data absent from git.** `git ls-files | grep -E "^\.env$|\.chroma|LEARNING\.md"` returns nothing;
   `git grep -i "sk-or-"` returns nothing.

5. **README rubric items** (reviewers score these directly):
   - One concise goal / problem / how-it-works paragraph.
   - Architecture description naming LangGraph, vector DB, MCP connector, BYO endpoint.
   - Ethics and data-handling section: synthetic data, injection defense,
     human-in-the-loop, mailbox ingestion deferred and why.
   - Evaluation section with dated metrics and their caveats.
   - Status checklist matches reality; "What's next" lists the OUT items from `docs/V2_PLAN.md`.

6. **Docker path** (plan item 7): `docker compose up` from the clean clone
   reaches the UI; the BYO endpoint env vars are documented in `.env.example`.

7. **Demo readiness.** A demo script exists with the four demo questions
   (single ticket, planted deficiency, TCK-1010 injection banner, coverage
   report) plus the pack-switch and endpoint-swap moments; a fallback screencast
   is recorded.

8. **Core hygiene.** `grep -r "langchain\|langgraph" core/` returns nothing.

9. Done when the table of items and evidence is in the reply, with the failing
   items first.
