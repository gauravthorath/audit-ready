"""The spend ledger, the budget arithmetic and the token bucket (Step 38). Offline.

Clocks are injected everywhere, so a day boundary and a refill are a variable
assignment, not a sleep.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from api.ratelimit import RateLimiter, RateLimits
from api.settings import Settings
from core.spend import Budgets, SpendLedger, budget_refusal, remaining_usd


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


@pytest.fixture()
def clock():
    return FakeClock(datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc))


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def test_the_ledger_adds_up_per_actor_and_for_everyone(clock):
    ledger = SpendLedger(None, clock=clock)
    ledger.record("alice", "chat", 3000, 200, 0.001, ref="s1")
    ledger.record("alice", "audit", 30000, 2000, 0.010, ref="r1")
    ledger.record("bob", "chat", 3000, 200, 0.002)
    assert ledger.today("alice") == pytest.approx(0.011)
    assert ledger.today("bob") == pytest.approx(0.002)
    assert ledger.today(None) == pytest.approx(0.013)
    assert ledger.today("carol") == 0.0
    assert [e.kind for e in ledger.entries()] == ["chat", "audit", "chat"]


def test_yesterday_does_not_count_today(clock):
    ledger = SpendLedger(None, clock=clock)
    ledger.record("alice", "chat", 1, 1, 5.0)
    clock.advance(days=1)
    assert ledger.today("alice") == 0.0
    assert ledger.today(None) == 0.0
    assert len(ledger.entries("2026-09-16")) == 1


def test_the_file_survives_a_restart_and_a_torn_line(tmp_path, clock):
    path = tmp_path / "spend.jsonl"
    first = SpendLedger(path, clock=clock)
    first.record("alice", "chat", 10, 5, 0.25)
    first.record("bob", "audit", 10, 5, 0.50, ref="run-1")
    with path.open("a") as fh:
        fh.write('{"ts": "2026-09-16T12:00:00+00:00", "day": "2026-09-16", "act')  # crash mid-write

    second = SpendLedger(path, clock=clock)  # "the process restarted"
    assert second.today("alice") == 0.25 and second.today("bob") == 0.50
    assert second.today(None) == 0.75
    assert len(second.entries()) == 2
    assert path.read_text().count("\n") >= 2


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


def test_no_budget_means_unlimited(clock):
    ledger = SpendLedger(None, clock=clock)
    ledger.record("alice", "audit", 1, 1, 999.0)
    assert remaining_usd(ledger, Budgets(), "alice") is None
    assert budget_refusal(ledger, Budgets(), "alice", expected_usd=1e6) is None


def test_the_tighter_cap_wins(clock):
    ledger = SpendLedger(None, clock=clock)
    ledger.record("alice", "chat", 1, 1, 0.40)
    ledger.record("bob", "chat", 1, 1, 0.50)
    budgets = Budgets(deployment_daily_usd=1.00, actor_daily_usd=0.60)
    assert remaining_usd(ledger, budgets, "alice") == pytest.approx(0.10)  # deployment: 1.00 - 0.90
    assert remaining_usd(ledger, budgets, "carol") == pytest.approx(0.10)  # same, a newcomer
    ledger.record("bob", "chat", 1, 1, 0.20)
    assert remaining_usd(ledger, budgets, "alice") == 0.0  # never negative


def test_refusals_are_sentences_with_the_numbers_in_them(clock):
    ledger = SpendLedger(None, clock=clock)
    budgets = Budgets(actor_daily_usd=0.05)
    assert budget_refusal(ledger, budgets, "alice", expected_usd=0.01) is None
    msg = budget_refusal(ledger, budgets, "alice", expected_usd=0.08)
    assert msg and "$0.0800" in msg and "$0.0500" in msg
    ledger.record("alice", "audit", 1, 1, 0.05)
    msg = budget_refusal(ledger, budgets, "alice")
    assert msg and "used up" in msg


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


def test_a_burst_up_to_the_limit_passes_and_the_next_waits():
    t = {"now": 1000.0}
    limiter = RateLimiter(3, 60.0, clock=lambda: t["now"])
    assert [limiter.allow("k")[0] for _ in range(3)] == [True, True, True]
    ok, wait = limiter.allow("k")
    assert ok is False and 19.9 < wait <= 20.0  # one token every 20s
    assert limiter.allow("other")[0] is True  # keys are independent
    t["now"] += 20.0
    assert limiter.allow("k")[0] is True  # refilled exactly one
    assert limiter.allow("k")[0] is False


def test_check_raises_a_429_with_retry_after_and_a_sentence():
    limiter = RateLimiter(1, 60.0, clock=lambda: 0.0)
    limiter.check("k", "chat turns")
    with pytest.raises(HTTPException) as exc:
        limiter.check("k", "chat turns")
    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"] == "60"
    assert "1 per minute" in exc.value.detail and "chat turns" in exc.value.detail


def test_zero_disables_a_limiter():
    limiter = RateLimiter(0, 60.0)
    assert all(limiter.allow("k")[0] for _ in range(100))
    limits = RateLimits.build(0, 0, 0)
    assert not limits.chat.enabled and not limits.audit.enabled and limits.max_pairs_per_run == 0


# ---------------------------------------------------------------------------
# Settings parse the env once and fail loudly
# ---------------------------------------------------------------------------


def test_settings_read_limits_and_budgets_from_the_env():
    s = Settings.from_env({})
    assert (s.chat_per_minute, s.audits_per_hour, s.max_pairs_per_run) == (30, 6, 400)
    assert s.daily_budget_usd is None and s.actor_daily_budget_usd is None

    s = Settings.from_env({"AUDITREADY_CHAT_PER_MINUTE": "5", "AUDITREADY_DAILY_BUDGET_USD": "2.50",
                           "AUDITREADY_ACTOR_DAILY_BUDGET_USD": ""})
    assert s.chat_per_minute == 5 and s.daily_budget_usd == 2.5 and s.actor_daily_budget_usd is None

    with pytest.raises(ValueError, match="AUDITREADY_DAILY_BUDGET_USD"):
        Settings.from_env({"AUDITREADY_DAILY_BUDGET_USD": "two dollars"})
    with pytest.raises(ValueError, match="negative"):
        Settings.from_env({"AUDITREADY_DAILY_BUDGET_USD": "-1"})
    with pytest.raises(ValueError, match="AUDITREADY_AUDITS_PER_HOUR"):
        Settings.from_env({"AUDITREADY_AUDITS_PER_HOUR": "many"})
