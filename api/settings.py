"""Everything the API reads from the environment, parsed once (CR-8, NFR-4).

Before this module each `AUDITREADY_*` variable was read where it was used —
`REPORTS_DIR` at import time in one place, `WEB_ORIGINS` in another — so a
test that wanted a different reports directory had to patch a module global
and a reader who wanted to know what the API could be told had to grep. Now
there is one dataclass, built by one function, and the app carries it on
`app.state.settings`. A test builds its own `Settings` and passes it to
`create_app`; nothing is patched.

Two rules. Every field has a default that works on a developer laptop with
no configuration, because the demo and `uv run pytest` must never need an
env file. And parsing happens here, not at the use site: a malformed value
fails at startup with the variable named, not on the first request.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Both loopback spellings by default: a browser at 127.0.0.1:3000 is not the
# same origin as localhost:3000, and the page's fetches fail silently on CORS
# with the picker left empty. Anything else (a LAN address, a hostname) is set
# with AUDITREADY_WEB_ORIGIN, comma-separated.
DEFAULT_WEB_ORIGINS = ("http://localhost:3000", "http://127.0.0.1:3000")

# Any origin on a private network is also allowed — the "Network" URL that
# `next dev` prints is a different origin from localhost and its fetches would
# fail silently otherwise. Never "*": this API has no auth yet, and a wildcard
# would let any website a user visits call their local engine.
PRIVATE_ORIGIN_RE = (
    r"^https?://(localhost|127\.0\.0\.1|\[::1\]|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(:\d+)?$"
)


@dataclass(frozen=True)
class Settings:
    """The API's configuration. Frozen: it is read, never mutated, after startup."""

    reports_dir: Path = Path("reports")
    web_origins: list[str] = field(default_factory=lambda: list(DEFAULT_WEB_ORIGINS))
    private_origin_regex: str = PRIVATE_ORIGIN_RE
    # Rate limits (Step 38). Per caller; 0 disables one.
    chat_per_minute: int = 30
    audits_per_hour: int = 6
    max_pairs_per_run: int = 400
    # Daily spend caps in USD (Step 38). None = unlimited, the developer default.
    daily_budget_usd: float | None = None
    actor_daily_budget_usd: float | None = None
    # Authentication (Step 40): none | token | oidc. `none` is the default so a
    # laptop, the demo and the test suite need no credential.
    auth_mode: str = "none"
    api_tokens: str = ""
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_roles_claim: str = "roles"
    oidc_default_roles: str = "auditor,viewer"
    # Where a conversation lives between turns (Step 41): memory | postgres.
    session_store: str = "memory"
    pg_dsn: str = ""

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        """Build from `os.environ` (or the mapping given, for tests)."""
        env = os.environ if env is None else env
        origins = [
            o.strip()
            for o in env.get("AUDITREADY_WEB_ORIGIN", ",".join(DEFAULT_WEB_ORIGINS)).split(",")
            if o.strip()
        ]
        return cls(
            reports_dir=Path(env.get("AUDITREADY_REPORTS_DIR", "reports")),
            web_origins=origins,
            chat_per_minute=_int(env, "AUDITREADY_CHAT_PER_MINUTE", 30),
            audits_per_hour=_int(env, "AUDITREADY_AUDITS_PER_HOUR", 6),
            max_pairs_per_run=_int(env, "AUDITREADY_MAX_PAIRS_PER_RUN", 400),
            daily_budget_usd=_float_or_none(env, "AUDITREADY_DAILY_BUDGET_USD"),
            actor_daily_budget_usd=_float_or_none(env, "AUDITREADY_ACTOR_DAILY_BUDGET_USD"),
            auth_mode=env.get("AUDITREADY_AUTH", "none").strip().lower() or "none",
            api_tokens=env.get("AUDITREADY_API_TOKENS", ""),
            oidc_issuer=env.get("AUDITREADY_OIDC_ISSUER", "").strip(),
            oidc_audience=env.get("AUDITREADY_OIDC_AUDIENCE", "").strip(),
            oidc_roles_claim=env.get("AUDITREADY_OIDC_ROLES_CLAIM", "roles").strip() or "roles",
            oidc_default_roles=env.get("AUDITREADY_OIDC_DEFAULT_ROLES", "auditor,viewer"),
            session_store=env.get("AUDITREADY_SESSION_STORE", "memory").strip().lower() or "memory",
            pg_dsn=env.get("AUDITREADY_PG_DSN", "").strip(),
        )


def _int(env: dict[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a whole number, got {raw!r}") from exc


def _float_or_none(env: dict[str, str], name: str) -> float | None:
    raw = env.get(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number of dollars, got {raw!r}") from exc
    if value < 0:
        raise ValueError(f"{name} must not be negative, got {raw!r}")
    return value
