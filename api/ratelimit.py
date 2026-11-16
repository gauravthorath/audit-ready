"""Rate limits for the two routes that spend money (FR-15, FR-17).

A token bucket per caller: `limit` calls per `period_s`, refilled continuously,
so a burst up to the limit is fine and a sustained loop is not. In-process and
lock-guarded, which is exactly right for one API process and exactly wrong for
several - when there is a second worker, the bucket moves to the same store
the sessions do (docs/PRODUCTION_PLAN.md, Step 41). The interface stays.

Why here and not at a gateway. Nginx or a cloud WAF can count requests; they
cannot see tokens or dollars, and dollars are the limit that matters. The
bucket is also what makes the refusal testable offline: `clock` is injected.

Refusals are `429` with `Retry-After` in whole seconds and a sentence a person
can act on. That is the FR-15 rule (fail gracefully, say why) applied to
"no" rather than to "broken".
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import HTTPException


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    """`limit` events per `period_s` per key. `limit <= 0` disables the limiter."""

    def __init__(self, limit: int, period_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = int(limit)
        self.period_s = float(period_s)
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def allow(self, key: str) -> tuple[bool, float]:
        """Take one token for `key`. Returns (allowed, seconds until the next token)."""
        if not self.enabled:
            return True, 0.0
        now = self._clock()
        rate = self.limit / self.period_s  # tokens per second
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=float(self.limit), updated=now)
                self._buckets[key] = bucket
            else:
                bucket.tokens = min(float(self.limit), bucket.tokens + (now - bucket.updated) * rate)
                bucket.updated = now
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True, 0.0
            return False, (1.0 - bucket.tokens) / rate

    def check(self, key: str, what: str) -> None:
        """Raise a 429 with Retry-After if `key` is over its limit for `what`."""
        ok, wait = self.allow(key)
        if ok:
            return
        seconds = max(1, math.ceil(wait))
        raise HTTPException(
            status_code=429,
            detail=f"Too many {what}: the limit is {self.limit} per "
                   f"{_period_words(self.period_s)}. Try again in {seconds}s.",
            headers={"Retry-After": str(seconds)},
        )


def _period_words(seconds: float) -> str:
    if seconds == 60:
        return "minute"
    if seconds == 3600:
        return "hour"
    return f"{seconds:g}s"


@dataclass
class RateLimits:
    """The limiters the routes use, built once from settings and hung on `app.state.limits`."""

    chat: RateLimiter
    audit: RateLimiter
    max_pairs_per_run: int

    @classmethod
    def build(cls, chat_per_minute: int, audits_per_hour: int, max_pairs_per_run: int,
              clock: Callable[[], float] = time.monotonic) -> RateLimits:
        return cls(
            chat=RateLimiter(chat_per_minute, 60.0, clock),
            audit=RateLimiter(audits_per_hour, 3600.0, clock),
            max_pairs_per_run=int(max_pairs_per_run),
        )
