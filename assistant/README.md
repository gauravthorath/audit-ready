# assistant/ - the v1 deliverable

Streamlit chatbot (LangChain + OpenRouter) over the ISO 9001 knowledge base,
wrapping `core/`. Answers audit-readiness questions with grounded, cited
verdicts.

## Run it

```bash
cp .env.example .env          # add your OPENROUTER_API_KEY
uv sync
uv run streamlit run assistant/app.py
```

First question triggers a one-time embedding-model download (~90 MB). If the
sidebar warns the vector index is missing, click **Rebuild vector index**.

Try: *"Is TCK-1002 audit-ready?"* · *"Was the freight-cost hotfix in
TCK-1007 deployed under an approved change?"* · *"Check TCK-1010 against
release sign-off"* (watch the security banner) · *"What is our overall
readiness for release sign-off?"*

## What's here

- `app.py` - Streamlit chat UI: sources/tool-results expander with verdict
  badges, RAG-trail visualisation, security banners, token + cost display,
  conversation export (JSON). **No business logic** - presentation only.
- `agent.py` - LangChain tool-calling agent: `@tool`-wrapped core functions,
  hardened system prompt (control catalog injected, grounding + security
  rules), hand-written tool loop, sliding-window memory.
- `data/checklists/iso9001.yaml` - 9 own-worded ISO 9001 controls, each with
  a machine-judgeable sufficiency rubric (no copyrighted ISO text).
- `data/checklists/iso27001.yaml` - 6 own-worded ISO/IEC 27001:2022 Annex A
  controls over the same corpus (CR-5). Loaded by the same code path;
  its answer key is `audit/eval/ground_truth_iso27001.yaml`.
- `data/corpus/` - synthetic corpus: 20 tickets, 10 emails, 7 change records
  with planted deficiencies, including a prompt-injection payload (TCK-1010).
- `eval/` - ground truth (46 labelled verdicts), 12 labelled questions,
  `run_eval.py` harness, `results.json` (committed evidence).

## Evaluation results (answers gpt-4o-mini, temperature 0)

Both parts re-run 2026-09-11 against iso9001 pack 0.1.1. Part B answer quality
is graded by an independent judge (`google/gemini-2.5-flash`), not by the model
that wrote the answers.

Compliance verdicts (46 labelled ticket×control pairs):
**gap recall 1.0** · flagged recall 1.0 · exact agreement 0.957 ·
false-positive rate 0.032 · citation validity 0.935

Answer quality (12 labelled questions through the full agent):
**faithfulness 0.977** · answer correctness 0.65 · source recall 0.945 ·
citation presence 1.0

Agreement moved from 0.978 to 0.957 and false positives from 0.0 to 0.032
because the 8.2.1 rubric was tightened on 2026-09-11: condition (c) now has to
originate with the client, so an outbound "the work is done" email no longer
counts as acknowledgement. A stricter rubric calls more things deficient, which
is the trade it is supposed to make - a false positive gets reviewed, a missed
deficiency reaches an auditor.

Compliance agreement on this pack has read 0.957, 0.978, 0.978 and 0.957 across
four runs at temperature 0, twice moving with no code change at all - the honest
reading is that it sits around 0.95-0.98 and single-run differences under ~0.03
are noise. Gap recall stayed at 1.0 in every one of them, which is the number
that matters. V2 harness tells the sharper version of this story: its
multi-call agent moved nine pairs over the same window in which this single-call
judge reproduced to three decimals.

Grading the answers with a different model family costs about 0.11 of answer
correctness (0.792 self-graded → 0.683 on the same answers, 0.65 today) and
leaves faithfulness roughly where it was (0.98 → 0.977). The judge is a flag, not a fixture:
`AUDITREADY_JUDGE_MODEL`, or `--judge-model` per run, swaps it; every
`results.json` stamps `judge_model` and `system_model` so a number is never
read without knowing who graded it. `--skip-compliance` / `--skip-rag`
carry the untouched part forward with a `carried_from_run` timestamp.

Each row keeps the judge's one-sentence reasoning, so the three low scorers
are diagnosable rather than mysterious: two are real answer defects
(Q08 contradicts the answer key on TCK-1004's change control; Q11 declines
to summarise the quarantined vendor note at all instead of summarising the
legitimate part and flagging the injection), and Q07 loses half its score
for answering with a control-by-control table where the question asked how
one complaint was handled. These three are why correctness sits near 0.68
while faithfulness is near 1.0: the answers are grounded in the corpus, they
just do not always answer the question that was asked.

Metrics are RAGAs-equivalent, implemented directly (`eval/run_eval.py`
documents why: ragas 0.4.3 is incompatible with the langchain 1.x line used
by the agent; FR-18 permits "RAGAs or equivalent").

## Known limitations & future work (AC-10)

- **Temperature 0 ≠ determinism.** One borderline ticket flipped verdicts
  between eval runs (provider-side nondeterminism). Single-run numbers have
  error bars; v2's CI gate (CR-7) should run repeats.
- **Self-grading bias - closed 2026-09-02.** Part B was graded by the model
  under test; correctness was 0.12 optimistic. Now graded by a different
  family (`google/gemini-2.5-flash`) and the grader is configurable. Not
  fully solved, only made honest: one model still grades another, and
  `gemini-2.5-pro` needs OpenRouter's privacy guardrail relaxed to run at
  all (`.env.example` says so).
- **Portfolio answers inherit per-pair variance.** A 20-ticket coverage
  sweep multiplies borderline-judgment noise (see Q12 in results.json).
- **Injection defense is a tripwire, not a proof.** Regex patterns catch
  known payload shapes; a novel phrasing could pass layers the prompt and
  citation checks would then have to catch. Red-teamed once, not
  continuously.
- **Rubrics are the spec, and specs drift.** Five of the bugs found during
  the build were rubric/spec defects, not code defects. Rubric changes need
  the eval re-run (frozen ground truth caught every one).
- **Scale.** 89 chunks; naive-vs-multi-query RAG differences barely show at
  this size. coverage_report now judges pairs on a small thread pool (4
  workers, ~4x faster) and streams per-judgment progress to the UI, but an
  unscoped sweep is still one LLM call per (ticket, control) pair. Real
  corpora need true batching, verdict caching, and pgvector (v2
  CR-2/CR-3).
