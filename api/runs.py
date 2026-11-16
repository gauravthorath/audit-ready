"""Audit runs: the registry, the worker thread, and the files that outlive it (CR-2, CR-8).

*Why finished runs go to disk, and where.* An audit that vanishes when the
process restarts is a demo, not a record. Every finished run is written with
`audit.batch.write_report` into the same directory the CLI uses, and on
startup that directory is read back, so the API lists runs made from the
command line and the command line's reports are visible in the browser. The
files are the source of truth; the registry's dict is a cache of them plus
whatever is in flight.

*Why a class and not two module globals.* The first version kept `_runs` and
`REPORTS_DIR` at module level, and the tests reached in to clear one and
patch the other. A `RunRegistry` owns both, so a test builds one on a temp
directory and hangs it on `app.state.runs` - replaced, not patched. The same
object is where a per-user view of runs will attach when the API knows who is
calling (docs/PRODUCTION_PLAN.md, Step 41).

*Why the runner is a parameter.* `run_batch` already takes a `runner` so tests
can swap the LangGraph agent for a fake. `start()` takes the same callable
from `app.state.runner`, so `tests/test_api.py` drives a full audit through
the real routes, the real pack and the real corpus, without a network or a key.
"""

from __future__ import annotations

import contextvars
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from audit.batch import BatchReport, run_batch, write_report
from audit.reporting import estimate_cost_usd
from core.checklists import ChecklistPack
from core.tracing import bind, elapsed_ms, log_event
from assistant.agent import DEFAULT_MODEL

log = logging.getLogger("auditready.runs")

Rate = tuple[float, float] | None
RunnerFn = Callable[..., dict[str, Any]]
FinishedFn = Callable[["Run"], None]


@dataclass
class Run:
    """One audit in flight or finished. The progress fields are what SSE streams."""

    run_id: str
    pack_id: str
    total: int
    started_at: str
    actor: str = ""  # who started it; "" means loaded from reports/, owned by nobody
    status: str = "running"  # running | done | error
    done: int = 0
    label: str = ""
    report: BatchReport | None = None
    error: str | None = None
    source: str = "live"  # live (this process ran it) | file (loaded from reports/)
    files: dict[str, str] = field(default_factory=dict)  # {"md": ..., "json": ...}
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self, rate: Rate = None, model: str = DEFAULT_MODEL) -> dict[str, Any]:
        """The wire view. `rate` is the model's price, or None for the list-price fallback."""
        with self.lock:
            out: dict[str, Any] = {
                "run_id": self.run_id,
                "pack_id": self.pack_id,
                "status": self.status,
                "started_at": self.started_at,
                "progress": {"done": self.done, "total": self.total, "label": self.label},
                "error": self.error,
                "source": self.source,
                "files": self.files,
            }
            if self.report is not None:
                out["report"] = self.report.to_dict()
                cost, note = estimate_cost_usd(
                    self.report.prompt_tokens, self.report.completion_tokens, rate
                )
                out["cost"] = {"usd": round(cost, 4), "basis": note, "model": model}
            return out


_REPORT_FIELDS = tuple(BatchReport.__dataclass_fields__)


def report_from_file(path: Path) -> BatchReport | None:
    """Rebuild a BatchReport from the JSON `write_report` emitted.

    `to_dict` adds the derived lists (open_gaps, review_queue, errors) on top of
    the dataclass fields; they are recomputed by the properties, so they are
    dropped here rather than passed to the constructor.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "rows" not in data or "pack_id" not in data:
        return None
    fields = {k: data[k] for k in _REPORT_FIELDS if k in data}
    try:
        return BatchReport(**fields)
    except TypeError:
        return None


class RunRegistry:
    """Every run this process knows about, in flight or on disk."""

    def __init__(self, reports_dir: Path) -> None:
        self.reports_dir = Path(reports_dir)
        self._runs: dict[str, Run] = {}
        self._lock = threading.Lock()

    # -- lookup ----------------------------------------------------------------

    def get(self, run_id: str) -> Run | None:
        with self._lock:
            return self._runs.get(run_id)

    def all(self) -> list[Run]:
        with self._lock:
            return list(self._runs.values())

    def clear(self) -> None:
        """Forget everything in memory. The files stay; `load_from_disk` finds them again."""
        with self._lock:
            self._runs.clear()

    # -- disk ------------------------------------------------------------------

    def load_from_disk(self, directory: Path | None = None) -> int:
        """Register every report JSON in `directory` as a finished run. Idempotent.

        The run id is the file stem (`<pack_id>-<timestamp>`), which is what the
        CLI prints, so a run started on the command line can be opened in the
        browser by the name it was given. Returns how many were newly registered.
        """
        directory = self.reports_dir if directory is None else Path(directory)
        if not directory.exists():
            return 0
        added = 0
        for path in sorted(directory.glob("*.json")):
            run_id = path.stem
            if self.get(run_id) is not None:
                continue
            report = report_from_file(path)
            if report is None:
                continue
            md = path.with_suffix(".md")
            run = Run(
                run_id=run_id,
                pack_id=report.pack_id,
                total=report.total_pairs,
                started_at=report.generated_at,
                status="done",
                done=report.total_pairs,
                report=report,
                source="file",
                files={"md": str(md) if md.exists() else "", "json": str(path)},
            )
            with self._lock:
                self._runs[run_id] = run
            added += 1
        return added

    def persist(self, run: Run) -> None:
        """Write a finished run the way the CLI does, and remember where it went."""
        if run.report is None:
            return
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        md, js = write_report(run.report, self.reports_dir)
        with run.lock:
            run.files = {"md": str(md), "json": str(js)}

    # -- start -----------------------------------------------------------------

    def start(
        self,
        pack: ChecklistPack,
        tickets: dict[str, dict],
        ticket_ids: list[str],
        control_ids: list[str],
        runner: RunnerFn,
        actor: str = "",
        finished: FinishedFn | None = None,
    ) -> Run:
        """Register a run and begin it on a daemon thread. Returns at once.

        The caller has already validated the ids (unknown ones must fail with a
        404 before a thread or a token is spent), so this method only owns the
        bookkeeping: the record, the thread, and the terminal status. `finished`
        is called once with the run when it ends, done or error - the router
        uses it to charge the spend ledger.
        """
        run = Run(
            run_id=uuid.uuid4().hex[:12],
            pack_id=pack.pack.id,
            total=len(ticket_ids) * len(control_ids),
            started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            actor=actor,
        )
        with self._lock:
            self._runs[run.run_id] = run
        # The thread runs in a copy of the request's context, so the request id
        # that started the audit is on every line the run logs, next to run_id.
        threading.Thread(
            target=contextvars.copy_context().run,
            args=(self._work, run, pack, tickets, ticket_ids, control_ids, runner, finished),
            name=f"audit-{run.run_id}", daemon=True,
        ).start()
        return run

    def _work(self, run: Run, pack: ChecklistPack, tickets: dict[str, dict],
              ticket_ids: list[str], control_ids: list[str], runner: RunnerFn,
              finished: FinishedFn | None = None) -> None:
        def on_progress(done: int, total: int, label: str) -> None:
            with run.lock:
                run.done, run.total, run.label = done, total, label

        started = time.perf_counter()
        with bind(run_id=run.run_id):
            log_event(log, "run.start", pack_id=run.pack_id, pairs=run.total)
            try:
                report = run_batch(
                    pack, tickets, ticket_ids, control_ids, progress=on_progress, runner=runner,
                )
                with run.lock:
                    run.report, run.status, run.done = report, "done", run.total
                try:
                    self.persist(run)
                except OSError as exc:  # a full disk must not turn a finished audit into an error
                    with run.lock:
                        run.files = {"error": f"report not written: {exc}"}
                log_event(log, "run.end", status="done", elapsed_ms=elapsed_ms(started),
                          readiness=report.readiness_score, verified=report.verified_pairs,
                          pairs=report.total_pairs, prompt_tokens=report.prompt_tokens,
                          completion_tokens=report.completion_tokens)
            except Exception as exc:  # noqa: BLE001 - surfaced to the client as a status, not a trace
                with run.lock:
                    run.status, run.error = "error", f"{type(exc).__name__}: {exc}"
                log_event(log, "run.end", logging.ERROR, status="error",
                          elapsed_ms=elapsed_ms(started), error=run.error)
            if finished is not None:
                try:
                    finished(run)
                except Exception as exc:  # noqa: BLE001 - accounting must never fail the audit
                    log_event(log, "run.finished_hook", logging.ERROR,
                              error=f"{type(exc).__name__}: {exc}")
