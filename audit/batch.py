"""Batch audit-readiness report: every ticket × every control in a pack (CR-2).

This is the step that turns a chatbot into a product. v1's
`coverage_report` already sweeps ticket × control on a thread pool; the batch
runner keeps that shape and changes three things that matter at report scale:

1. **Pairs go through the LangGraph agent** (`audit.graph.run_control`), so
   every verdict arrives either *verified* or *flagged for human review*. A
   contested verdict never leaks into the score as if it were settled.
2. **One failing pair does not kill the run.** `coverage_report` propagates the
   first exception - fine for a 4-pair chat question, unacceptable for a
   180-pair overnight sweep. Here a pair that raises becomes a row with
   status "error" and the reason; the report says how many, and which.
3. **The output is an artifact, not a dict.** `render_markdown` writes the
   report a compliance manager would actually hand to an auditor: score,
   per-control table, open gaps with citations, the review queue, errors,
   cost. JSON stays available for the UI and for diffing runs.

Scoring policy is *imported* from core.tools (VERDICT_CREDIT, SEVERITY_WEIGHT),
not restated: one rule, one place. Pairs whose status is "needs_review" or
"error" drop out of the score's denominator - the score is a statement about
verified verdicts only, and the report prints coverage ("N of M pairs
verified") right next to it so the number can't be read out of context.

Why not just call coverage_report with the graph swapped in?
  coverage_report's contract is the chat tool's contract (FR-5.3): small,
  scoped, raise-on-failure, no review queue. Bolting flags and error rows
  onto it would change what the v1 agent sees mid-conversation. A
  separate module with the same scoring import is the smaller change.

Why thread pool, not asyncio (again)?
  Same reason as Step 15: the stack is synchronous end to end, and the work
  is network wait on the LLM. `max_workers=4` was measured to be the point
  where OpenRouter stopped rate-limiting a full sweep. The graph does 2-3
  LLM calls per pair (judge, verify, maybe re-judge), so a 20 × 9 pack is
  roughly 400-500 calls; expect 6-10 minutes, ~$0.10 on gpt-4o-mini.

The batch runner takes a pack and a ticket map as ARGUMENTS. That is what
makes "new standard = new YAML file" (plan item 3) and the Streamlit pack
picker (plan item 5) free: nothing in here knows which standard it is scoring.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from audit.graph import run_control
from core.checklists import ChecklistPack, load_pack
from core.tools import (
    DEFAULT_CORPUS_DIR,
    DEFAULT_PACK_PATH,
    SEVERITY_WEIGHT,
    VERDICT_CREDIT,
    load_tickets,
)
from core.tracing import bind, elapsed_ms, log_event

log = logging.getLogger("auditready.batch")

ProgressFn = Callable[[int, int, str], None]
RunnerFn = Callable[[dict, Any], dict[str, Any]]

# One pair's outcome. "verdict" = verified by the graph; "needs_review" =
# verifier still objects after the re-judge (or the verifier itself failed);
# "error" = the pair raised before producing anything.
STATUSES = ("verdict", "needs_review", "error")


@dataclass
class BatchReport:
    pack_id: str
    standard: str
    generated_at: str
    ticket_ids: list[str]
    control_ids: list[str]
    rows: list[dict[str, Any]]  # one per (ticket, control), requested order
    per_control: dict[str, dict[str, Any]] = field(default_factory=dict)
    readiness_score: float | None = None
    verified_pairs: int = 0
    total_pairs: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_ms: int = 0  # wall time of the run, all workers included

    # -- derived views the renderers share ---------------------------------
    @property
    def open_gaps(self) -> list[dict[str, Any]]:
        return [
            r for r in self.rows
            if r["status"] == "verdict" and r["verdict"] in ("gap", "partial")
        ]

    @property
    def review_queue(self) -> list[dict[str, Any]]:
        return [r for r in self.rows if r["status"] == "needs_review"]

    @property
    def errors(self) -> list[dict[str, Any]]:
        return [r for r in self.rows if r["status"] == "error"]

    def to_dict(self) -> dict[str, Any]:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__}
        d["open_gaps"] = self.open_gaps
        d["review_queue"] = self.review_queue
        d["errors"] = self.errors
        return d


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def _error_row(ticket_id: str, control, exc: Exception) -> dict[str, Any]:
    return {
        "ticket_id": ticket_id,
        "control_id": control.control_id,
        "control_title": control.title,
        "severity": control.severity,
        "status": "error",
        "error": f"{type(exc).__name__}: {exc}",
        "verdict": None,
        "rationale": None,
        "citations": [],
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }


def _timed_pair(runner: RunnerFn, ticket: dict, control) -> dict[str, Any]:
    """Run one pair under its own trace context and stamp how long it took.

    The timer wraps the *call*, not the graph, so an error row and a fake
    runner in a test are timed the same way as the real agent. `pair` is bound
    here so every `llm.call` line the graph emits names the ticket and control
    it was judging.
    """
    label = f"{ticket['ticket_id']}x{control.control_id}"
    started = time.perf_counter()
    with bind(pair=label):
        try:
            row = dict(runner(ticket, control))
        except Exception as exc:
            log_event(log, "batch.pair", logging.WARNING, elapsed_ms=elapsed_ms(started),
                      status="error", error=f"{type(exc).__name__}: {exc}")
            raise
        row["elapsed_ms"] = elapsed_ms(started)
        log_event(log, "batch.pair", elapsed_ms=row["elapsed_ms"], status=row.get("status"),
                  verdict=row.get("verdict"), attempts=row.get("attempts"),
                  prompt_tokens=row.get("prompt_tokens", 0),
                  completion_tokens=row.get("completion_tokens", 0))
        return row


def run_batch(
    pack: ChecklistPack,
    tickets: dict[str, dict],
    ticket_ids: list[str] | None = None,
    control_ids: list[str] | None = None,
    progress: ProgressFn | None = None,
    max_workers: int = 4,
    runner: RunnerFn = run_control,
) -> BatchReport:
    """Judge every requested ticket against every requested control.

    `runner` is injectable so tests (and a future cheaper "fast mode") can
    swap the LangGraph agent for something else with the same return shape.
    Unknown ids fail HERE, before any tokens are spent.
    """
    # None = "all"; an explicit empty list is a real (and refused) selection.
    ticket_ids = list(tickets.keys()) if ticket_ids is None else list(ticket_ids)
    control_ids = (
        [c.control_id for c in pack.controls] if control_ids is None else list(control_ids)
    )
    controls = {cid: pack.get_control(cid) for cid in control_ids}
    for tid in ticket_ids:
        if tid not in tickets:
            raise KeyError(f"unknown ticket {tid!r}; known: {sorted(tickets)}")

    pairs = [(t, c) for c in control_ids for t in ticket_ids]
    if not pairs:
        # A 100% score over zero pairs is a lie the report must never tell.
        raise ValueError("nothing to audit: no ticket/control pairs selected")
    results: dict[tuple[str, str], dict[str, Any]] = {}
    started = time.perf_counter()
    log_event(log, "batch.start", pairs=len(pairs), tickets=len(ticket_ids),
              controls=len(control_ids), workers=max_workers)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        # Each task runs in a copy of the caller's context, so the request id
        # and run id bound by the API reach the worker threads' log lines. One
        # copy per task: a Context cannot be entered twice at the same time.
        futures = {
            pool.submit(contextvars.copy_context().run, _timed_pair, runner,
                        tickets[t], controls[c]): (t, c)
            for t, c in pairs
        }
        for done, future in enumerate(as_completed(futures), start=1):
            t, c = futures[future]
            try:
                results[(t, c)] = future.result()
            except Exception as exc:  # noqa: BLE001 - one bad pair must not sink the run
                results[(t, c)] = _error_row(t, controls[c], exc)
            if progress is not None:
                progress(done, len(pairs), f"{t} × {c}")

    report = _aggregate(pack, ticket_ids, control_ids, controls, results,
                        elapsed=elapsed_ms(started))
    log_event(log, "batch.end", pairs=report.total_pairs, verified=report.verified_pairs,
              errors=len(report.errors), review=len(report.review_queue),
              readiness=report.readiness_score, elapsed_ms=report.elapsed_ms,
              prompt_tokens=report.prompt_tokens, completion_tokens=report.completion_tokens)
    return report


def _aggregate(pack, ticket_ids, control_ids, controls, results, elapsed: int = 0) -> BatchReport:
    rows: list[dict[str, Any]] = []
    per_control: dict[str, dict[str, Any]] = {}
    weighted_credit = weighted_total = 0.0
    prompt_tokens = completion_tokens = 0
    verified = 0

    for cid in control_ids:
        control = controls[cid]
        counts = {"satisfied": 0, "partial": 0, "gap": 0, "not_applicable": 0,
                  "needs_review": 0, "error": 0}
        for tid in ticket_ids:
            r = results[(tid, cid)]
            rows.append(r)
            prompt_tokens += r.get("prompt_tokens", 0)
            completion_tokens += r.get("completion_tokens", 0)

            if r["status"] != "verdict":
                counts[r["status"]] += 1
                continue

            verified += 1
            verdict = r["verdict"]
            counts[verdict] += 1
            if verdict != "not_applicable":
                w = SEVERITY_WEIGHT[control.severity]
                weighted_credit += VERDICT_CREDIT[verdict] * w
                weighted_total += w

        applicable = counts["satisfied"] + counts["partial"] + counts["gap"]
        per_control[cid] = {
            "title": control.title,
            "clause": control.clause,
            "severity": control.severity,
            "counts": counts,
            "satisfied_rate": (
                round(counts["satisfied"] / applicable, 3) if applicable else None
            ),
        }

    return BatchReport(
        pack_id=pack.pack.id,
        standard=pack.pack.standard,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ticket_ids=ticket_ids,
        control_ids=control_ids,
        rows=rows,
        per_control=per_control,
        readiness_score=(
            round(weighted_credit / weighted_total, 3) if weighted_total else None
        ),
        verified_pairs=verified,
        total_pairs=len(rows),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        elapsed_ms=elapsed,
    )


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x:.0%}"


def render_markdown(report: BatchReport) -> str:
    """The artifact a compliance manager hands to the auditor.

    Order is deliberate: the number, then what the number is made of, then
    the work list (gaps), then what a human still has to decide (review
    queue), then what the tool could not do (errors), then what it cost.
    """
    r = report
    out: list[str] = []
    out.append(f"# Audit-readiness report - {r.standard}")
    out.append("")
    out.append(f"Pack `{r.pack_id}` · generated {r.generated_at} · "
               f"{len(r.ticket_ids)} records × {len(r.control_ids)} controls")
    out.append("")
    out.append(f"## Readiness score: {_pct(r.readiness_score)}")
    out.append("")
    out.append(
        f"Based on **{r.verified_pairs} of {r.total_pairs}** record-control pairs "
        f"that reached a verified verdict. Severity-weighted (major ×3, minor ×1); "
        f"partial counts half; not-applicable pairs are excluded. "
        f"{len(r.review_queue)} pair(s) await human review and {len(r.errors)} "
        f"failed to run - neither is in the score."
    )
    out.append("")

    out.append("## Per control")
    out.append("")
    out.append("| Control | Clause | Sev. | Satisfied | Partial | Gap | N/A | Review | Error | Satisfied rate |")
    out.append("|---|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for cid in r.control_ids:
        pc = r.per_control[cid]; c = pc["counts"]
        out.append(
            f"| {pc['title']} | {pc['clause']} | {pc['severity']} | {c['satisfied']} | "
            f"{c['partial']} | {c['gap']} | {c['not_applicable']} | {c['needs_review']} | "
            f"{c['error']} | {_pct(pc['satisfied_rate'])} |"
        )
    out.append("")

    out.append(f"## Open gaps ({len(r.open_gaps)})")
    out.append("")
    if not r.open_gaps:
        out.append("None - every verified pair is satisfied or not applicable.")
    for g in r.open_gaps:
        cites = ", ".join(g["citations"]) or "no citation"
        out.append(f"- **{g['ticket_id']} × {g['control_id']}** - {g['verdict'].upper()} "
                   f"({g['severity']}). {g['rationale']} _[{cites}]_")
    out.append("")

    out.append(f"## Needs human review ({len(r.review_queue)})")
    out.append("")
    if not r.review_queue:
        out.append("None - the verifier agreed with every verdict.")
    for q in r.review_queue:
        ver = q.get("verification") or {}
        issues = f"second auditor says {ver.get('verdict')}"
        if ver.get("issues"):
            issues += ": " + "; ".join(ver["issues"])
        out.append(f"- **{q['ticket_id']} × {q['control_id']}** - judge said "
                   f"{q['verdict']!s} after {q.get('attempts', '?')} attempt(s); "
                   f"{issues}")
    out.append("")

    if r.errors:
        out.append(f"## Errors ({len(r.errors)})")
        out.append("")
        for e in r.errors:
            out.append(f"- **{e['ticket_id']} × {e['control_id']}** - {e['error']}")
        out.append("")

    out.append("## Cost")
    out.append("")
    out.append(f"{r.prompt_tokens:,} prompt + {r.completion_tokens:,} completion tokens.")
    if r.elapsed_ms:
        out.append(f"Wall time {r.elapsed_ms / 1000:.1f}s for {r.total_pairs} pairs.")
    out.append("")
    return "\n".join(out)


def write_report(report: BatchReport, out_dir: str | Path) -> tuple[Path, Path]:
    """Write <out_dir>/<pack_id>-<timestamp>.{md,json}; returns both paths."""
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    stamp = report.generated_at.replace(":", "").replace("+0000", "Z")
    stem = out_dir / f"{report.pack_id}-{stamp}"
    md, js = stem.with_suffix(".md"), stem.with_suffix(".json")
    md.write_text(render_markdown(report))
    js.write_text(json.dumps(report.to_dict(), indent=2, default=str))
    return md, js


# ---------------------------------------------------------------------------
# CLI:  uv run python -m audit.batch [--pack P] [--tickets a,b] [--controls x,y]
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pack", default=str(DEFAULT_PACK_PATH))
    ap.add_argument("--corpus", default=str(DEFAULT_CORPUS_DIR))
    ap.add_argument("--tickets", help="comma-separated ticket ids (default: all)")
    ap.add_argument("--controls", help="comma-separated control ids (default: all)")
    ap.add_argument("--out", default=os.getenv("AUDITREADY_REPORT_DIR", "reports"))
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args(argv)

    pack = load_pack(a.pack)
    tickets = load_tickets(a.corpus)
    split = lambda s: [x.strip() for x in s.split(",") if x.strip()] if s else None  # noqa: E731

    n = len(split(a.tickets) or tickets) * len(split(a.controls) or pack.controls)
    print(f"{n} pairs → roughly {2 * n}-{3 * n} LLM calls (judge + verify, maybe re-judge)")

    def show(done: int, total: int, label: str) -> None:
        print(f"\r  {done}/{total}  {label:<60}", end="", flush=True)

    report = run_batch(pack, tickets, split(a.tickets), split(a.controls),
                       progress=show, max_workers=a.workers)
    print()
    md, js = write_report(report, a.out)
    print(render_markdown(report))
    print(f"written: {md}\n         {js}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
