"""`/audit` — start a batch run, list runs, read one, export it, stream its progress (CR-2, FR-16).

*Why audit is asynchronous.* A batch audit is hundreds of model calls over
minutes, and a request that long is a request that times out somewhere
between here and the browser. So `POST /audit` returns a run id immediately,
the work runs on a thread (`api.runs.RunRegistry.start`), and progress streams
over SSE — the same "never show a silent spinner" rule the Streamlit page
follows (FR-16), carried across a transport that would otherwise lose it.

*Where validation happens.* Unknown ids fail HERE, synchronously, before a
thread or a token is spent. `run_batch` checks them too, but doing it up front
turns a 500 five seconds later into a 404 now.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from api.auth import Actor
from api.deps import (
    AuditorDep, BudgetsDep, LedgerDep, LimitsDep, PricingDep, RunnerDep, RunsDep, ViewerDep,
    pack_by_id,
)
from api.runs import Run, RunRegistry
from api.schemas import RunOut, StartAuditOut
from audit.batch import render_markdown
from audit.reporting import estimate_calls, estimate_cost_usd, estimate_run_cost_usd
from core.spend import Ledger, budget_refusal
from core.tools import DEFAULT_CORPUS_DIR, load_tickets
from assistant.agent import DEFAULT_MODEL

router = APIRouter()


class AuditRequest(BaseModel):
    pack_id: str
    ticket_ids: list[str] | None = None   # None = every ticket in the corpus
    control_ids: list[str] | None = None  # None = every control in the pack


def visible_to(run: Run, actor: Actor) -> bool:
    """Whose runs you can see (Step 41).

    With `AUDITREADY_AUTH=none` the deployment is single-user by definition —
    everyone sees everything, which is today's behaviour and the demo's. With
    authentication on you see your own runs, plus runs with no owner: those
    were loaded from the reports directory, written by the CLI, and that
    directory is the operator's shared record. Hiding them would make the API
    disagree with the filesystem it reads.
    """
    return actor.auth == "none" or run.actor in (actor.id, "")


def _run_or_404(runs: RunRegistry, run_id: str, actor: Actor) -> Run:
    """A run you may not see is *not found*, never forbidden: a 403 would confirm it exists."""
    run = runs.get(run_id)
    if run is None or not visible_to(run, actor):
        raise HTTPException(status_code=404, detail=f"unknown run {run_id!r}")
    return run


def _charge(ledger: Ledger, actor: str, rate) -> Callable[[Run], None]:
    """The run-finished hook: put what the run actually cost on the ledger."""
    def finished(run: Run) -> None:
        with run.lock:
            report = run.report
        if report is None:
            return
        usd, _ = estimate_cost_usd(report.prompt_tokens, report.completion_tokens, rate)
        ledger.record(actor, "audit", report.prompt_tokens, report.completion_tokens, usd,
                      ref=run.run_id)
    return finished


@router.post("/audit", status_code=202, response_model=StartAuditOut)
def start_audit(
    req: AuditRequest,
    runs: RunsDep,
    runner: RunnerDep,
    pricing: PricingDep,
    actor: AuditorDep,
    limits: LimitsDep,
    ledger: LedgerDep,
    budgets: BudgetsDep,
) -> dict[str, Any]:
    """Start a batch audit and return immediately with a run id.

    Unknown ids fail HERE, synchronously, before a thread or a token is spent —
    `run_batch` already checks them, and doing it up front turns a 500 five
    seconds later into a 404 now. The same rule covers the limits: too many
    runs, too many pairs, or a run the day's budget cannot pay for are all
    refused here, with the estimate in the sentence.
    """
    limits.audit.check(actor.id, "audit runs")
    pack = pack_by_id(req.pack_id)
    tickets = load_tickets(DEFAULT_CORPUS_DIR)

    ticket_ids = req.ticket_ids if req.ticket_ids is not None else sorted(tickets)
    control_ids = (
        req.control_ids if req.control_ids is not None
        else [c.control_id for c in pack.controls]
    )
    unknown_t = [t for t in ticket_ids if t not in tickets]
    if unknown_t:
        raise HTTPException(status_code=404, detail=f"unknown ticket(s): {unknown_t}")
    known_c = {c.control_id for c in pack.controls}
    unknown_c = [c for c in control_ids if c not in known_c]
    if unknown_c:
        raise HTTPException(status_code=404, detail=f"unknown control(s): {unknown_c}")
    if not ticket_ids or not control_ids:
        raise HTTPException(status_code=400, detail="nothing to audit: empty selection")

    total = len(ticket_ids) * len(control_ids)
    if limits.max_pairs_per_run and total > limits.max_pairs_per_run:
        raise HTTPException(
            status_code=400,
            detail=f"{total} record–control pairs is over the limit of {limits.max_pairs_per_run} "
                   "per run. Narrow the selection or run it in parts.",
        )
    rate = pricing(DEFAULT_MODEL)
    expected_usd, _ = estimate_run_cost_usd(total, rate)
    refusal = budget_refusal(ledger, budgets, actor.id, expected_usd)
    if refusal:
        raise HTTPException(status_code=429, detail=refusal)

    run = runs.start(pack, tickets, ticket_ids, control_ids, runner,
                     actor=actor.id, finished=_charge(ledger, actor.id, rate))
    lo, hi = estimate_calls(run.total)
    return {"run_id": run.run_id, "total_pairs": run.total, "estimated_calls": [lo, hi]}


@router.get("/audit", response_model=list[RunOut])
def list_audits(runs: RunsDep, pricing: PricingDep, actor: ViewerDep) -> list[dict[str, Any]]:
    runs.load_from_disk()  # a CLI run finished since startup shows up here
    rate = pricing(DEFAULT_MODEL)
    out = []
    for run in runs.all():
        if not visible_to(run, actor):
            continue
        snap = run.snapshot(rate)
        snap.pop("report", None)  # the list is for picking; the report is per-run
        out.append(snap)
    return sorted(out, key=lambda s: s["started_at"], reverse=True)


@router.get("/audit/{run_id}", response_model=RunOut)
def get_audit(run_id: str, runs: RunsDep, pricing: PricingDep, actor: ViewerDep) -> dict[str, Any]:
    return _run_or_404(runs, run_id, actor).snapshot(pricing(DEFAULT_MODEL))


@router.get("/audit/{run_id}/report.md", response_class=PlainTextResponse)
def audit_markdown(run_id: str, runs: RunsDep, actor: ViewerDep) -> str:
    run = _run_or_404(runs, run_id, actor)
    with run.lock:
        report = run.report
    if report is None:
        raise HTTPException(status_code=409, detail=f"run {run_id} is {run.status}, not done")
    return render_markdown(report)


@router.get(
    "/audit/{run_id}/events",
    response_class=EventSourceResponse,
    responses={200: {"content": {"text/event-stream": {}}, "description": "progress events, then `end`"}},
)
async def audit_events(run_id: str, runs: RunsDep, actor: ViewerDep) -> EventSourceResponse:
    """Progress as server-sent events, ending with the terminal status.

    Polling the run record every 400ms is deliberate over pushing from the
    worker thread: the worker never has to know a client exists, and a client
    that disconnects costs nothing.
    """
    run = _run_or_404(runs, run_id, actor)

    async def stream():
        last: tuple[int, str, str] | None = None
        while True:
            snap = run.snapshot()
            key = (snap["progress"]["done"], snap["progress"]["label"], snap["status"])
            if key != last:
                last = key
                payload = {k: snap[k] for k in ("run_id", "status", "progress", "error")}
                yield {"event": "progress", "data": json.dumps(payload)}
            if snap["status"] in ("done", "error"):
                yield {"event": "end", "data": json.dumps({"status": snap["status"]})}
                return
            await asyncio.sleep(0.4)

    return EventSourceResponse(stream())
