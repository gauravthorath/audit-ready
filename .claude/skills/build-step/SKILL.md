---
name: build-step
description: Build one numbered walking-skeleton step of AuditReady (a new module, node, tool, or feature). Use when the user says "next step", "build step N", "let's build X", or asks to implement anything in core/, assistant/, or audit/.
---

# Build step

One step = one unproven layer, proven live before the next layer goes on. The
step is done when every completion criterion below is met, not when the code runs.

## Steps

1. **Scope.** Find the deliverable in `docs/V2_PLAN.md` (IN table + build
   order) and the requirement ids it serves in `docs/REQUIREMENTS.md`. Read the
   last `## Step N` heading in `LEARNING.md`; this step is N+1. State scope,
   ids, and step number in one paragraph before writing code. Anything OUT in
   the plan stays out; say so if the request drifts there.

2. **Reuse check.** List the `core/` functions the step will call. New
   capability that is framework-free belongs in `core/`; graph, agent, UI, and
   connector plumbing belongs in `audit/` or `assistant/`. A prompt goes in the
   package's `prompts.py`.

3. **Ownership check.** If Gaurav said he writes this one, produce the skeleton
   (signatures, docstrings, TODOs, the smoke script stub) and the design
   reasoning, then stop. Otherwise continue.

4. **Write the module.** Module docstring opens with what it does, which
   requirement ids it serves, and the design notes (why this, what was rejected)
   in the style of `core/llm.py`. One exception type per module. Dependencies:
   `uv add <pkg>`, one at a time.

5. **Smoke script** `scripts/smoke_<name>.py`: runs live, prints `PASSED` or
   exits non-zero with `FAILED: <reason>`, docstring says how to run and what
   it needs. Run it. Done when it passes against the real LLM/vector store.

6. **Pytest** for the pure seams (parsing, validation, aggregation, ordering)
   in `tests/test_<name>.py`, offline, no API key, fake the LLM at the `chat()`
   seam. Run `uv run pytest`. Done when green.

7. **Core hygiene**: `grep -r "langchain\|langgraph" core/` returns nothing.
   `git status` shows no `.env`, `.chroma/`, `LEARNING.md`.

8. **Docs touched by the step**: the module table in `core/README.md` or the
   package README, and the README status checklist if a plan item closed.

9. **Journal.** Invoke the `learning-journal` skill for Step N+1.

10. **Commit.** One conventional commit with scope and requirement id, e.g.
    `feat(v2): langgraph per-control graph — retrieve→judge→verify (cr-1)`.
    Body: 2–4 lines on the design decision. Commit only when Gaurav asks.

## V2-specific notes

- LangGraph nodes wrap existing `core/` calls; the graph adds control flow
  (branching on verdict, self-verify loop), not new judgment logic.
- `audit/` gets its own smoke scripts (`scripts/smoke_graph.py`,
  `scripts/smoke_report.py`) and pytest files; the batch report is tested with a
  fake judge for ordering and aggregation, exactly as `coverage_report` is.
- Eval-affecting changes (rubric, judge prompt, model, retrieval) trigger the
  `eval-run` skill before the commit.
