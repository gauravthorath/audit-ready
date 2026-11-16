"""Evaluation harness (FR-18, AC-8): compliance metrics + RAG answer quality.

    uv run python assistant/eval/run_eval.py                    # everything
    uv run python assistant/eval/run_eval.py --skip-rag         # compliance only
    uv run python assistant/eval/run_eval.py --max-questions 3  # quick RAG pass
    uv run python assistant/eval/run_eval.py --judge-model openai/gpt-4o-mini

Two parts:

PART A - compliance metrics over ground_truth.yaml (46 labelled pairs):
  the system's real job is verdicts, so the headline numbers are
    - gap recall            of GT gaps, % predicted gap  (missed gap = the
                            costly error: a deficiency survives to audit day)
    - flagged recall        GT deficiencies (gap|partial) predicted gap|partial
    - false-positive rate   GT satisfied predicted deficient (trust killer:
                            auditors stop reading a system that cries wolf)
    - exact agreement       all pairs, plus the full confusion matrix
    - citation validity     % of verdicts whose citations resolve (FR-14)

PART B - RAG answer quality over questions.yaml (12 labelled questions),
  RAGAs-EQUIVALENT, implemented directly. Why not the ragas library: ragas
  0.4.3 imports langchain_community.chat_models.vertexai, which no longer
  exists in the langchain 1.x line our agent uses - irreconcilable without
  downgrading the agent stack. FR-18 says "RAGAs or equivalent"; the metrics
  below are the same ideas, and implementing them teaches what they mean:
    - faithfulness        LLM-as-judge: what fraction of the answer's claims
                          are supported by the tool results it saw?
                          (= the anti-hallucination metric)
    - answer_correctness  LLM-as-judge: does the answer agree with the
                          labelled ground-truth answer?
    - source_recall       deterministic: % of expected_sources ids that
                          appear in the turn's tool results/answer
    - citation_presence   deterministic: does the answer cite artifacts?

The Part B grader is pluggable: --judge-model, else AUDITREADY_JUDGE_MODEL,
else core.llm.DEFAULT_JUDGE_MODEL (a different family from the system model -
v1 graded itself with its own model, which flatters the numbers). The
grader model that produced a run is stamped into results.json next to the
metrics, because a faithfulness score is only readable if you know who graded.

Writes the full run to assistant/eval/results.json (committed as AC-8
evidence). A part that is skipped is carried forward from the previous file
and stamped `carried_from_run`, so re-running one part never silently drops
the other's evidence. LLM-as-judge caveats stay in LEARNING.md: a grader is
still one model's opinion, so trends are the signal.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

from core.llm import DEFAULT_JUDGE_MODEL, DEFAULT_MODEL, chat
from core.tools import check_ticket

EVAL_DIR = Path("assistant/eval")
VERDICTS = ["satisfied", "partial", "gap", "not_applicable"]


# ===========================================================================
# Part A - compliance metrics (deterministic scoring over labelled verdicts)
# ===========================================================================


def run_compliance_eval() -> dict:
    gt = yaml.safe_load((EVAL_DIR / "ground_truth.yaml").read_text())["tickets"]

    rows = []
    confusion: Counter[tuple[str, str]] = Counter()  # (expected, predicted)
    citation_ok = citation_checked = 0

    pairs = [
        (ticket_id, control_id, expected)
        for ticket_id, entry in gt.items()
        for control_id, expected in entry.get("verdicts", {}).items()
    ]
    print(f"Part A: judging {len(pairs)} labelled (ticket, control) pairs …")

    for i, (ticket_id, control_id, expected) in enumerate(pairs, 1):
        result = check_ticket(ticket_id, control_id)
        predicted = result["verdict"]
        confusion[(expected, predicted)] += 1
        if predicted != "not_applicable":
            citation_checked += 1
            citation_ok += result["citation_check"]["status"] == "ok"
        rows.append(
            {
                "ticket_id": ticket_id,
                "control_id": control_id,
                "expected": expected,
                "predicted": predicted,
                "match": expected == predicted,
                "rationale": result["rationale"],
            }
        )
        mark = "=" if expected == predicted else "!"
        print(f"  [{i:2}/{len(pairs)}] {mark} {ticket_id} {control_id}: "
              f"expected {expected}, got {predicted}")

    def rate(numer: int, denom: int) -> float | None:
        return round(numer / denom, 3) if denom else None

    gt_gap = [r for r in rows if r["expected"] == "gap"]
    gt_deficient = [r for r in rows if r["expected"] in ("gap", "partial")]
    gt_satisfied = [r for r in rows if r["expected"] == "satisfied"]

    metrics = {
        "pairs": len(rows),
        "exact_agreement": rate(sum(r["match"] for r in rows), len(rows)),
        "gap_recall": rate(
            sum(r["predicted"] == "gap" for r in gt_gap), len(gt_gap)
        ),
        "flagged_recall": rate(
            sum(r["predicted"] in ("gap", "partial") for r in gt_deficient),
            len(gt_deficient),
        ),
        "false_positive_rate": rate(
            sum(r["predicted"] in ("gap", "partial") for r in gt_satisfied),
            len(gt_satisfied),
        ),
        "citation_validity": rate(citation_ok, citation_checked),
        "confusion": {
            f"{exp}->{pred}": n for (exp, pred), n in sorted(confusion.items())
        },
    }

    print("\nPart A results")
    print(f"  exact agreement     : {metrics['exact_agreement']}")
    print(f"  gap recall          : {metrics['gap_recall']}   (missed gaps are the costly error)")
    print(f"  flagged recall      : {metrics['flagged_recall']}")
    print(f"  false-positive rate : {metrics['false_positive_rate']}   (trust metric - lower is better)")
    print(f"  citation validity   : {metrics['citation_validity']}")
    print("  confusion (expected->predicted):")
    for key, n in metrics["confusion"].items():
        print(f"    {key}: {n}")
    mismatches = [r for r in rows if not r["match"]]
    if mismatches:
        print("  disagreements:")
        for r in mismatches:
            print(f"    {r['ticket_id']} {r['control_id']}: "
                  f"{r['expected']} -> {r['predicted']}")

    return {"metrics": metrics, "rows": rows}


# ===========================================================================
# Part B - RAG answer quality (RAGAs-equivalent)
# ===========================================================================

_JUDGE_FAITHFULNESS = """\
You are grading whether an AI assistant's answer is FAITHFUL to the evidence
it retrieved (its tool results). Faithful = every factual claim in the answer
is supported by the evidence; general phrasing and hedges are fine.

Respond with ONLY a JSON object:
{"claims": <total factual claims>, "unsupported": <claims not backed by the
evidence>, "examples": ["<unsupported claim>", ...]}"""

_JUDGE_CORRECTNESS = """\
You are grading whether an AI assistant's answer AGREES with a ground-truth
reference answer. Grade the audit substance (verdict, key evidence, what's
missing) not the wording.

Respond with ONLY a JSON object:
{"score": <0.0-1.0>, "reasoning": "<one sentence>"}"""


def _judge_json(system: str, user: str, model: str) -> dict:
    """One LLM-as-judge call -> parsed JSON (same tolerant slice as core).

    `model` is a required argument, not a default: a grader that silently
    falls back to the system model is the self-grading bias creeping back in
    without anything in the output looking different.
    """
    reply = chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        model=model,
    ).content
    start, end = reply.find("{"), reply.rfind("}")
    return json.loads(reply[start : end + 1])


def run_rag_eval(max_questions: int | None, judge_model: str) -> dict:
    from assistant.agent import AuditAgent  # deferred: pulls langchain

    questions = yaml.safe_load((EVAL_DIR / "questions.yaml").read_text())["questions"]
    if max_questions:
        questions = questions[:max_questions]
    print(f"\nPart B: {len(questions)} labelled questions through the agent …")
    print(f"        answers by {DEFAULT_MODEL}, graded by {judge_model}")

    rows = []
    for i, q in enumerate(questions, 1):
        agent = AuditAgent()  # fresh conversation per question: no leakage
        reply = agent.ask(q["question"])
        evidence = json.dumps([c["result"] for c in reply.tool_calls])
        haystack = evidence + reply.content

        judge_errors: list[str] = []
        try:
            f = _judge_json(
                _JUDGE_FAITHFULNESS,
                f"EVIDENCE (tool results):\n{evidence[:12000]}\n\n"
                f"ANSWER:\n{reply.content}",
                judge_model,
            )
            faithfulness = (
                round((f["claims"] - f["unsupported"]) / f["claims"], 3)
                if f.get("claims")
                else None
            )
        except Exception as exc:  # a dead grader must be diagnosable, not silent
            f, faithfulness = {}, None
            judge_errors.append(f"faithfulness: {type(exc).__name__}: {exc}")
        try:
            c = _judge_json(
                _JUDGE_CORRECTNESS,
                f"GROUND TRUTH:\n{q['ground_truth']}\n\nANSWER:\n{reply.content}",
                judge_model,
            )
            correctness = round(float(c["score"]), 3)
        except Exception as exc:
            c, correctness = {}, None
            judge_errors.append(f"correctness: {type(exc).__name__}: {exc}")

        expected = q.get("expected_sources", [])
        source_recall = (
            round(sum(s in haystack for s in expected) / len(expected), 3)
            if expected
            else None
        )
        cites = bool(re.search(r"\b(?:TCK|EML|CHG)-\d+\b|ISO9001-", reply.content))

        rows.append(
            {
                "id": q["id"],
                "question": q["question"],
                "answer": reply.content,
                "tools_used": [c["name"] for c in reply.tool_calls],
                "faithfulness": faithfulness,
                "unsupported_examples": f.get("examples", []),
                "answer_correctness": correctness,
                "correctness_reasoning": c.get("reasoning", ""),
                "source_recall": source_recall,
                "citation_presence": cites,
                "judge_errors": judge_errors,
            }
        )
        print(f"  [{i:2}/{len(questions)}] {q['id']}: faithfulness={faithfulness} "
              f"correctness={correctness} source_recall={source_recall} cites={cites}")

    def avg(key: str) -> float | None:
        vals = [r[key] for r in rows if isinstance(r[key], (int, float))]
        return round(sum(vals) / len(vals), 3) if vals else None

    metrics = {
        "questions": len(rows),
        "judge_model": judge_model,
        "system_model": DEFAULT_MODEL,
        "faithfulness": avg("faithfulness"),
        "answer_correctness": avg("answer_correctness"),
        "source_recall": avg("source_recall"),
        "citation_presence_rate": round(
            sum(r["citation_presence"] for r in rows) / len(rows), 3
        ),
    }
    print("\nPart B results")
    for key, value in metrics.items():
        print(f"  {key:24}: {value}")
    if metrics["faithfulness"] is None and metrics["answer_correctness"] is None:
        errors = {e.split(":")[1].strip() for r in rows for e in r["judge_errors"]}
        print(f"  WARNING: every judge call failed ({', '.join(errors) or 'unknown'}) "
              f"- is {judge_model} a valid model for this key?")
    return {"metrics": metrics, "rows": rows}


# ===========================================================================


def carry_forward(results: dict, previous: dict) -> dict:
    """Keep a skipped part's numbers, stamped with the run they came from.

    Re-running one part used to overwrite the file with half the evidence.
    Carrying the other part forward keeps results.json whole; the stamp keeps
    it honest, so nobody reads two runs as one.
    """
    for part in ("compliance", "rag"):
        if part not in results and part in previous:
            results[part] = {**previous[part], "carried_from_run": previous.get("ran_at")}
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-compliance", action="store_true")
    parser.add_argument("--skip-rag", action="store_true")
    parser.add_argument("--max-questions", type=int, default=None)
    parser.add_argument(
        "--judge-model",
        default=None,
        help="grader model for Part B's LLM-as-judge metrics; default "
             f"{DEFAULT_JUDGE_MODEL} (env AUDITREADY_JUDGE_MODEL). The system "
             "model itself is set by AUDITREADY_MODEL.",
    )
    parser.add_argument("--out", default=str(EVAL_DIR / "results.json"))
    args = parser.parse_args()

    out = Path(args.out)
    previous = json.loads(out.read_text()) if out.exists() else {}

    results: dict = {"ran_at": datetime.now(timezone.utc).isoformat()}
    if not args.skip_compliance:
        results["compliance"] = run_compliance_eval()
    if not args.skip_rag:
        results["rag"] = run_rag_eval(
            args.max_questions, args.judge_model or DEFAULT_JUDGE_MODEL
        )

    out.write_text(json.dumps(carry_forward(results, previous), indent=2))
    print(f"\nfull results -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
