"""The spend ledger - every dollar the model cost, added up and capped (FR-17).

The UI has shown cost per turn and per run since v1 (FR-17). What nothing
did was *add it up* across runs, or *refuse* anything. A 20 × 9 audit is about
400 model calls; a browser tab in a loop could start one a second, and the
first anyone would know is the invoice. This module is the accounting: an
append-only ledger of what was spent, by whom, on what, and two budgets the
API checks before it spends.

Why a JSONL file. One line per charge, appended under a lock, so it survives a
restart and a person can read it with `tail`. Totals are kept in memory and
rebuilt from the file at start, so `today()` is a dict lookup, not a scan. The
Postgres-backed ledger (docs/PRODUCTION_PLAN.md, Step 41) implements the same
three methods; the API talks to the protocol, not the file.

Why two budgets. `AUDITREADY_DAILY_BUDGET_USD` caps the deployment - the number
the person who pays for the key cares about. `AUDITREADY_ACTOR_DAILY_BUDGET_USD`
caps each caller - the number that stops one user from spending everyone
else's day. Either may be unset, which means unlimited; both default to unset
so a developer laptop and the test suite are never refused.

Why days are UTC. A budget that resets at local midnight resets at a different
moment on every machine that runs the API. UTC is one moment everywhere.

Framework-free: stdlib only, so `core/` keeps its rule.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SpendEntry:
    """One charge. `kind` is what was bought (chat turn, audit run); `ref` is its id."""

    ts: str
    day: str
    actor: str
    kind: str
    prompt_tokens: int
    completion_tokens: int
    usd: float
    ref: str | None = None


@dataclass(frozen=True)
class Budgets:
    """Daily caps in USD. None means unlimited."""

    deployment_daily_usd: float | None = None
    actor_daily_usd: float | None = None


class Ledger(Protocol):
    def record(self, actor: str, kind: str, prompt_tokens: int, completion_tokens: int,
               usd: float, ref: str | None = None) -> SpendEntry: ...
    def today(self, actor: str | None = None) -> float: ...
    def entries(self, day: str | None = None) -> list[SpendEntry]: ...


class SpendLedger:
    """Append-only JSONL ledger with in-memory daily totals. Thread-safe."""

    DEPLOYMENT = "*"  # the key for "everyone" in the totals

    def __init__(self, path: Path | str | None = None, clock: Clock = utc_now) -> None:
        self.path = Path(path) if path is not None else None
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: list[SpendEntry] = []
        self._totals: dict[tuple[str, str], float] = {}  # (day, actor|*) -> usd
        if self.path is not None and self.path.exists():
            self._load()

    # -- read ------------------------------------------------------------------

    def today(self, actor: str | None = None) -> float:
        """USD spent today (UTC) by `actor`, or by everyone when `actor` is None."""
        key = (self._day(), self.DEPLOYMENT if actor is None else actor)
        with self._lock:
            return round(self._totals.get(key, 0.0), 6)

    def entries(self, day: str | None = None) -> list[SpendEntry]:
        with self._lock:
            if day is None:
                return list(self._entries)
            return [e for e in self._entries if e.day == day]

    # -- write -----------------------------------------------------------------

    def record(self, actor: str, kind: str, prompt_tokens: int, completion_tokens: int,
               usd: float, ref: str | None = None) -> SpendEntry:
        now = self._clock()
        entry = SpendEntry(
            ts=now.isoformat(timespec="seconds"), day=now.date().isoformat(), actor=actor,
            kind=kind, prompt_tokens=int(prompt_tokens), completion_tokens=int(completion_tokens),
            usd=round(float(usd), 6), ref=ref,
        )
        with self._lock:
            self._add(entry)
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(asdict(entry)) + "\n")
        return entry

    # -- internals ---------------------------------------------------------------

    def _day(self) -> str:
        return self._clock().date().isoformat()

    def _add(self, entry: SpendEntry) -> None:
        self._entries.append(entry)
        for actor in (entry.actor, self.DEPLOYMENT):
            key = (entry.day, actor)
            self._totals[key] = self._totals.get(key, 0.0) + entry.usd

    def _load(self) -> None:
        assert self.path is not None
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                entry = SpendEntry(**{k: data[k] for k in SpendEntry.__dataclass_fields__ if k in data})
            except (ValueError, TypeError, KeyError):
                continue  # a torn line at the end of a crashed write must not sink the ledger
            self._add(entry)


# ---------------------------------------------------------------------------
# Budget arithmetic - pure, so the API's refusals are unit-testable
# ---------------------------------------------------------------------------


def remaining_usd(ledger: Ledger, budgets: Budgets, actor: str) -> float | None:
    """What `actor` may still spend today: the tighter of the two caps, or None if unlimited."""
    room: list[float] = []
    if budgets.deployment_daily_usd is not None:
        room.append(budgets.deployment_daily_usd - ledger.today(None))
    if budgets.actor_daily_usd is not None:
        room.append(budgets.actor_daily_usd - ledger.today(actor))
    if not room:
        return None
    return round(max(min(room), 0.0), 6)


def budget_refusal(ledger: Ledger, budgets: Budgets, actor: str, expected_usd: float = 0.0) -> str | None:
    """A plain sentence if `actor` may not spend `expected_usd` more today, else None.

    The sentence is meant for a person (FR-15): it says what would be spent and
    what is left, never which variable to change.
    """
    left = remaining_usd(ledger, budgets, actor)
    if left is None:
        return None
    if left <= 0:
        return "Today's spending budget is used up. Try again tomorrow or ask an administrator to raise it."
    if expected_usd > left:
        return (f"This would cost about ${expected_usd:.4f} and ${left:.4f} of today's budget "
                "remains. Narrow the selection or wait until tomorrow.")
    return None
