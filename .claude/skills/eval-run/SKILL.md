---
name: eval-run
description: Run and interpret the AuditReady evaluation harness. Use when the user says "run the eval", "check the metrics", or after any change to a sufficiency rubric, judge or decomposition prompt, the model, retrieval, or guardrails.
---

# Eval run

Ground truth is frozen; the eval is the regression gate for every judgment-
affecting change. It costs real tokens (46 judgments plus 12 agent questions),
so pick the smallest run that answers the question.

## Steps

1. **Baseline.** Note the current headline numbers from `assistant/README.md`
   and `git log -1 -- assistant/eval/results.json`.

2. **Pick the run.**
   - Rubric / judge prompt / model change: `uv run python assistant/eval/run_eval.py --skip-rag`
   - Agent prompt / retrieval / guardrail change: full run, no flags.
   - Quick sanity: `--max-questions 3`.
   - Exploratory runs write elsewhere: `--out /tmp/results_try.json`; only a
     run you intend to keep overwrites `assistant/eval/results.json`.

3. **Read the numbers in this order.**
   - **gap recall** — headline; a drop means a planted deficiency now survives to audit day.
   - **false-positive rate** — the trust metric; a rise means clean tickets get flagged.
   - **citation validity** — a drop usually means a prompt or record-fencing regression.
   - confusion matrix — locate exactly which (ticket, control) pairs moved.
   - Part B (faithfulness, answer correctness, source recall, citation presence)
     — trend signal only; judge model equals system model, self-grading bias.

4. **Classify each moved pair** before touching code: provider
   nondeterminism (re-run the single pair with `check_ticket`; TCK-1018 is the
   known borderline), a rubric/spec defect (fix the YAML, re-run), or a code
   regression (fix, re-run). Five of the v1 bugs were rubric defects.

5. **Record.** When the run is the new baseline: commit `results.json`, update
   the dated results block in `assistant/README.md`, and add the observation to
   the `LEARNING.md` step it belongs to. Commit message `eval:` or `docs(eval):`.

6. Done when every moved pair has a classification and the README numbers
   match `results.json`.

## V2 extension

V2 harness (plan item 4) extends this file rather than replacing it.
New labelled pairs for a second pack go in a sibling ground-truth file under
`assistant/eval/` or `audit/eval/`; the metric definitions stay identical so
numbers compare across packs.
