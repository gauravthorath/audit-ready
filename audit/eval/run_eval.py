"""V2 eval: the LangGraph agent against every labelled pair, both packs.

    uv run python audit/eval/run_eval.py                  # both packs, ~68 pairs
    uv run python audit/eval/run_eval.py --packs iso27001 # one pack
    uv run python audit/eval/run_eval.py --max-pairs 6 --out /tmp/try.json

Plan item 4 (CR-7 evaluation at scale; FR-18 compliance metrics). Writes
audit/eval/results.json, committed as the evidence behind the README
numbers, exactly as assistant/eval/results.json is for v1.

Why a second harness instead of a flag on assistant/eval/run_eval.py
------------------------------------------------------------------
v1 scores `check_ticket`: one judge call, one verdict, no notion of
"the system declined to decide". V2 agent (audit/graph.py) can
end in `needs_review`, and that outcome is the whole point of CR-1. Scoring it
needs metrics v1 has no column for:

* **review rate** - how much of the pack a human still has to look at;
* **review precision** - of the pairs parked for a human, how many the judge
  actually got wrong (the verifier's false alarms are the cost of the loop);
* **error catch rate** - of the judge's wrong verdicts, how many the verifier
  stopped from being finalized (the verifier's recall);
* **finalized agreement / false-positive rate** - the accuracy of the verdicts
  the readiness score is actually built on.

The v1 metric *definitions* (gap recall, flagged recall, false-positive
rate, exact agreement, citation validity, confusion) are reproduced here
verbatim over the judge's final verdict regardless of status, so the ISO 9001
numbers are directly comparable with the committed v1 baseline; the
harness prints that delta. Same corpus, same 46 labelled pairs, different
runner - the delta isolates what the graph adds.

Two packs, one harness: the ISO 9001 answer key stays frozen in assistant/eval/;
the ISO 27001 key lives beside this file. Per-pack and pooled numbers are
both reported because 22 pairs is small enough that one flipped verdict moves
a rate by 4-5 points.

Design choices
--------------
* Pairs run on a 4-worker thread pool, same width as the batch runner and for
  the same reason (OpenRouter rate limits observed at 8).
* A pair that raises becomes an `error` row and is excluded from every rate's
  denominator but counted, so a flaky provider cannot silently inflate a score.
* `runner` is injectable so the scoring is unit-tested with no network. The
  metric function is pure: rows in, dict out.
* Temperature 0 is not determinism (v1 lesson): a single moved pair
  between runs is noise until re-run; the committed results.json is one run,
  dated, and says so.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from audit.graph import VERIFY_MODEL, run_control  # noqa: E402
from core.checklists import ChecklistPack, load_pack  # noqa: E402
from core.llm import DEFAULT_MODEL  # noqa: E402
from core.tools import load_tickets  # noqa: E402

EVAL_DIR = Path("audit/eval")
BASELINE_RESULTS = Path("assistant/eval/results.json")

# pack name -> (checklist pack, answer key). Add a pack: add a line.
PACKS: dict[str, tuple[Path, Path]] = {
    "iso9001": (Path("assistant/data/checklists/iso9001.yaml"),
                Path("assistant/eval/ground_truth.yaml")),
    "iso27001": (Path("assistant/data/checklists/iso27001.yaml"),
                 EVAL_DIR / "ground_truth_iso27001.yaml"),
}

DEFICIENT = ("gap", "partial")
Runner = Callable[[dict, Any], dict[str, Any]]


# ---------------------------------------------------------------- loading


def load_pairs(gt_path: str | Path) -> list[tuple[str, str, str]]:
    """Answer key -> [(ticket_id, control_id, expected_verdict)] in file order."""
    gt = yaml.safe_load(Path(gt_path).read_text())["tickets"]
    return [
        (ticket_id, control_id, expected)
        for ticket_id, entry in gt.items()
        for control_id, expected in (entry.get("verdicts") or {}).items()
    ]


# ---------------------------------------------------------------- running


def _row(ticket_id: str, control_id: str, expected: str, result: dict) -> dict:
    ver = result.get("verification") or {}
    cc = result.get("citation_check") or {}
    predicted = result["verdict"]
    return {
        "ticket_id": ticket_id,
        "control_id": control_id,
        "expected": expected,
        "predicted": predicted,
        "match": predicted == expected,
        "status": result["status"],
        "verifier_verdict": ver.get("verdict"),
        "verifier_issues": ver.get("issues", []),
        "attempts": result.get("attempts"),
        "citation_status": cc.get("status"),
        "rationale": result.get("rationale"),
        "prompt_tokens": result.get("prompt_tokens", 0),
        "completion_tokens": result.get("completion_tokens", 0),
    }


def _error_row(ticket_id: str, control_id: str, expected: str, exc: Exception) -> dict:
    return {
        "ticket_id": ticket_id, "control_id": control_id, "expected": expected,
        "predicted": None, "match": False, "status": "error",
        "verifier_verdict": None, "verifier_issues": [], "attempts": 0,
        "citation_status": None, "rationale": None,
        "prompt_tokens": 0, "completion_tokens": 0,
        "error": f"{type(exc).__name__}: {exc}",
    }


def run_pairs(
    pack: ChecklistPack,
    tickets: dict[str, dict],
    pairs: list[tuple[str, str, str]],
    *,
    runner: Runner = run_control,
    max_workers: int = 4,
    progress: Callable[[int, int, str], None] | None = None,
) -> list[dict]:
    """Run labelled pairs through `runner`; rows come back in answer-key order."""
    controls = {c.control_id: c for c in pack.controls}
    for t, c, _ in pairs:
        if t not in tickets:
            raise ValueError(f"answer key names unknown ticket {t}")
        if c not in controls:
            raise ValueError(f"answer key names unknown control {c}")

    results: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(runner, tickets[t], controls[c]): (i, t, c, e)
            for i, (t, c, e) in enumerate(pairs)
        }
        done = 0
        for fut in as_completed(futures):
            i, t, c, e = futures[fut]
            try:
                results[i] = _row(t, c, e, fut.result())
            except Exception as exc:  # noqa: BLE001 - isolate, record, continue
                results[i] = _error_row(t, c, e, exc)
            done += 1
            if progress:
                progress(done, len(pairs), f"{t} × {c}")
    return [results[i] for i in range(len(pairs))]


# ---------------------------------------------------------------- scoring


def _rate(numer: int, denom: int) -> float | None:
    return round(numer / denom, 3) if denom else None


def score(rows: list[dict]) -> dict[str, Any]:
    """Pure: rows -> metrics. v1 definitions first, then the agent's."""
    scored = [r for r in rows if r["status"] != "error"]
    errors = [r for r in rows if r["status"] == "error"]

    gt_gap = [r for r in scored if r["expected"] == "gap"]
    gt_deficient = [r for r in scored if r["expected"] in DEFICIENT]
    gt_satisfied = [r for r in scored if r["expected"] == "satisfied"]
    cited = [r for r in scored if r["predicted"] != "not_applicable"
             and r["citation_status"] is not None]

    finalized = [r for r in scored if r["status"] == "verdict"]
    parked = [r for r in scored if r["status"] == "needs_review"]
    wrong = [r for r in scored if not r["match"]]
    fin_satisfied = [r for r in finalized if r["expected"] == "satisfied"]

    confusion: Counter[str] = Counter(
        f"{r['expected']}->{r['predicted']}" for r in scored
    )

    return {
        "pairs": len(rows),
        "scored": len(scored),
        "errors": len(errors),
        # --- v1 definitions, over the judge's final verdict
        "exact_agreement": _rate(sum(r["match"] for r in scored), len(scored)),
        "gap_recall": _rate(sum(r["predicted"] == "gap" for r in gt_gap), len(gt_gap)),
        "flagged_recall": _rate(
            sum(r["predicted"] in DEFICIENT for r in gt_deficient), len(gt_deficient)
        ),
        "false_positive_rate": _rate(
            sum(r["predicted"] in DEFICIENT for r in gt_satisfied), len(gt_satisfied)
        ),
        "citation_validity": _rate(
            sum(r["citation_status"] == "ok" for r in cited), len(cited)
        ),
        "confusion": dict(sorted(confusion.items())),
        # --- what the graph adds
        "review_rate": _rate(len(parked), len(scored)),
        "review_precision": _rate(sum(not r["match"] for r in parked), len(parked)),
        "error_catch_rate": _rate(
            sum(r["status"] == "needs_review" for r in wrong), len(wrong)
        ),
        "finalized_pairs": len(finalized),
        "finalized_exact_agreement": _rate(
            sum(r["match"] for r in finalized), len(finalized)
        ),
        "finalized_false_positive_rate": _rate(
            sum(r["predicted"] in DEFICIENT for r in fin_satisfied), len(fin_satisfied)
        ),
        "rejudged_pairs": sum((r["attempts"] or 0) > 1 for r in scored),
        "prompt_tokens": sum(r["prompt_tokens"] for r in rows),
        "completion_tokens": sum(r["completion_tokens"] for r in rows),
    }


BASELINE_KEYS = ("exact_agreement", "gap_recall", "flagged_recall",
                 "false_positive_rate", "citation_validity")


def compare_to_baseline(metrics: dict, baseline: dict) -> dict[str, dict]:
    """ISO 9001 only: the graph vs the committed v1 check_ticket numbers."""
    out = {}
    for k in BASELINE_KEYS:
        a, b = baseline.get(k), metrics.get(k)
        out[k] = {
            "assistant": a, "audit": b,
            "delta": round(b - a, 3) if a is not None and b is not None else None,
        }
    return out


# ---------------------------------------------------------------- reporting


def _print_metrics(name: str, m: dict) -> None:
    print(f"\n{name}: {m['scored']} scored / {m['pairs']} pairs"
          + (f" ({m['errors']} errored)" if m["errors"] else ""))
    print(f"  exact agreement        : {m['exact_agreement']}")
    print(f"  gap recall             : {m['gap_recall']}   (missed gaps are the costly error)")
    print(f"  flagged recall         : {m['flagged_recall']}")
    print(f"  false-positive rate    : {m['false_positive_rate']}   (trust metric - lower is better)")
    print(f"  citation validity      : {m['citation_validity']}")
    print(f"  review rate            : {m['review_rate']}   (share parked for a human)")
    print(f"  review precision       : {m['review_precision']}   (parked pairs the judge got wrong)")
    print(f"  error catch rate       : {m['error_catch_rate']}   (judge errors the verifier stopped)")
    print(f"  finalized agreement    : {m['finalized_exact_agreement']} over {m['finalized_pairs']} finalized")
    print(f"  finalized FP rate      : {m['finalized_false_positive_rate']}")
    print(f"  re-judged pairs        : {m['rejudged_pairs']}")
    print("  confusion (expected->predicted):")
    for key, n in m["confusion"].items():
        print(f"    {key}: {n}")


def _print_disagreements(rows: list[dict]) -> None:
    bad = [r for r in rows if r["status"] == "error" or not r["match"]]
    if not bad:
        return
    print("  disagreements / errors:")
    for r in bad:
        tag = "ERROR" if r["status"] == "error" else (
            "parked" if r["status"] == "needs_review" else "FINALIZED")
        detail = r.get("error") or f"{r['expected']} -> {r['predicted']}"
        print(f"    [{tag:9}] {r['ticket_id']} {r['control_id']}: {detail}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--packs", default=",".join(PACKS),
                    help=f"comma list from {sorted(PACKS)} (default all)")
    ap.add_argument("--max-pairs", type=int, default=None,
                    help="per pack, first N labelled pairs (sanity runs)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default=str(EVAL_DIR / "results.json"))
    a = ap.parse_args(argv)

    names = [n.strip() for n in a.packs.split(",") if n.strip()]
    unknown = [n for n in names if n not in PACKS]
    if unknown:
        ap.error(f"unknown pack(s) {unknown}; choose from {sorted(PACKS)}")

    tickets = load_tickets()
    # Who produced these numbers. assistant/eval stamps its models for the same
    # reason: a metric read without knowing which model answered is unreadable,
    # and a hosted model is the one eval input that cannot be frozen - it moved
    # under us between 2026-09-03 and 2026-09-11 with no code change.
    results: dict[str, Any] = {
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "runner": "audit.graph.run_control",
        "system_model": DEFAULT_MODEL,
        "verify_model": VERIFY_MODEL or DEFAULT_MODEL,
        "packs": {},
    }
    all_rows: list[dict] = []

    for name in names:
        pack_path, gt_path = PACKS[name]
        pack = load_pack(pack_path)
        pairs = load_pairs(gt_path)
        if a.max_pairs:
            pairs = pairs[: a.max_pairs]
        print(f"\n== {name} ({pack.pack.standard}): {len(pairs)} labelled pairs, "
              f"{a.workers} workers ==")

        def progress(done, total, label):
            print(f"  [{done:2}/{total}] {label}")

        rows = run_pairs(pack, tickets, pairs, max_workers=a.workers, progress=progress)
        metrics = score(rows)
        _print_metrics(name, metrics)
        _print_disagreements(rows)
        entry: dict[str, Any] = {
            "pack": str(pack_path), "ground_truth": str(gt_path),
            "metrics": metrics, "rows": rows,
        }
        if name == "iso9001" and not a.max_pairs and BASELINE_RESULTS.exists():
            base = json.loads(BASELINE_RESULTS.read_text())["compliance"]["metrics"]
            entry["vs_assistant"] = compare_to_baseline(metrics, base)
            print("  vs v1 check_ticket baseline (same 46 pairs):")
            for k, v in entry["vs_assistant"].items():
                print(f"    {k:22}: {v['assistant']} -> {v['audit']}  ({v['delta']:+})"
                      if v["delta"] is not None else f"    {k:22}: n/a")
        results["packs"][name] = entry
        all_rows.extend(rows)

    if len(names) > 1:
        results["pooled"] = score(all_rows)
        _print_metrics("pooled", results["pooled"])

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
