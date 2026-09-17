"""Response models — the wire contract, written down once (CR-8).

Until this file existed every route returned `dict[str, Any]`, so the OpenAPI
document FastAPI generates said "object" about everything and the TypeScript
types in `web/lib/api.ts` were mirrored by hand. Two copies of a contract with
nothing enforcing agreement is a drift waiting to be found in a browser.

These models fix that from the Python side: FastAPI validates every response
against them and emits a typed OpenAPI document; `scripts/export_openapi.py`
writes it to `api/openapi.json`; `tests/test_openapi_contract.py` fails if the
committed document is stale; and `web/` generates its TypeScript types from
that file. One source of truth, three consumers, a test on the seam.

Two deliberate softnesses. Rows carry `extra="allow"` and mostly-optional
fields, because a row is whatever `audit/graph.run_control` (or the error
path in `audit/batch`) produced, and a response model that rejects a real
row would turn a finished audit into a 500 — the exact failure this layer must
never introduce. And a ticket keeps unknown keys, because the synthetic corpus
itself carries one (`satisfaction_survey`) and the retrieval layer indexes
whatever it is given.

Req: CR-8.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Packs and records
# ---------------------------------------------------------------------------


class ControlOut(BaseModel):
    control_id: str
    clause: str
    title: str
    severity: Literal["major", "minor"]
    description: str
    expected_evidence: list[str]
    citation_required: bool


class PackOut(BaseModel):
    """A checklist pack. Invalid packs are listed too — with `error` set and no id."""

    id: str | None
    standard: str | None = None
    version: str | None = None
    description: str | None = None
    label: str
    path: str
    error: str | None
    controls: list[ControlOut]


class RecordSummaryOut(BaseModel):
    ticket_id: str
    title: str | None = None
    type: str | None = None
    priority: str | None = None
    status: str | None = None
    created: str | None = None
    closed: str | None = None
    reporter: str | None = None
    assignee: str | None = None
    comments: int = 0  # a count here; the full ticket carries the list
    linked_changes: list[str] = Field(default_factory=list)
    linked_emails: list[str] = Field(default_factory=list)


class CommentOut(BaseModel):
    author: str
    timestamp: str
    body: str


class TicketOut(BaseModel):
    """The full record, as the judge reads it. Unknown keys survive (see module doc)."""

    model_config = ConfigDict(extra="allow")

    ticket_id: str
    title: str
    type: str
    priority: str
    status: str
    created: str
    closed: str | None = None
    reporter: str
    assignee: str | None = None
    description: str
    comments: list[CommentOut]
    linked_changes: list[str]
    linked_emails: list[str]
    resolution: str
    signoff: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Small things
# ---------------------------------------------------------------------------


class HealthOut(BaseModel):
    ok: bool
    auth: str      # none | token | oidc — which credential this deployment asks for
    sessions: str  # memory | postgres — whether a conversation survives a restart
    model: str
    corpus_dir: str
    vector_store: str  # "chroma:<dir>" or "pgvector:<host>:<port>/<db>/<namespace>"
    web_origins: list[str]
    time: str


class PricingOut(BaseModel):
    model: str
    prompt_usd_per_m: float | None
    completion_usd_per_m: float | None
    source: str


class LimitsOut(BaseModel):
    chat_per_minute: int
    audits_per_hour: int
    max_pairs_per_run: int


class SpendOut(BaseModel):
    """Today's spend for the caller and the deployment, the caps, and what remains (null = unlimited)."""

    actor: str
    today_usd: float
    actor_budget_usd: float | None
    deployment_today_usd: float
    deployment_budget_usd: float | None
    remaining_usd: float | None
    model: str
    basis: str
    limits: LimitsOut


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------


class SessionOut(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    cost_basis: str
    model: str


class ToolCallOut(BaseModel):
    """One tool invocation. `result` is whatever the tool returned — JSON if it was JSON."""

    name: str
    args: dict[str, Any]
    result: Any = None


class ChatOut(BaseModel):
    session_id: str
    answer: str
    tool_calls: list[ToolCallOut]
    prompt_tokens: int
    completion_tokens: int
    session: SessionOut


class EndChatOut(BaseModel):
    session_id: str
    ended: bool


class SessionSummaryOut(BaseModel):
    """One of your conversations, as the picker shows it. Never carries the messages."""

    session_id: str
    title: str
    turns: int
    created_at: str
    updated_at: str


# ---------------------------------------------------------------------------
# Audit runs
# ---------------------------------------------------------------------------


class StartAuditOut(BaseModel):
    run_id: str
    total_pairs: int
    estimated_calls: list[int]  # [low, high] — judge + verify, maybe one re-judge


class ProgressOut(BaseModel):
    done: int
    total: int
    label: str


class VerificationOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    agrees: bool | None = None
    verdict: str | None = None
    checks: list[dict[str, Any]] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    model: str | None = None


class PairRowOut(BaseModel):
    """One (ticket, control) result. Shape from run_control, or batch's error row."""

    model_config = ConfigDict(extra="allow")

    ticket_id: str
    control_id: str
    control_title: str | None = None
    severity: str | None = None
    verdict: str | None = None
    rationale: str | None = None
    citations: list[str] = Field(default_factory=list)
    citation_check: dict[str, Any] = Field(default_factory=dict)
    security_flags: list[dict[str, Any]] = Field(default_factory=list)
    evidence_queries: list[str] = Field(default_factory=list)
    evidence_chunks: list[str] = Field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    status: Literal["verdict", "needs_review", "error"]
    verification: VerificationOut | None = None
    attempts: int | None = None
    trace: list[str] = Field(default_factory=list)
    error: str | None = None
    elapsed_ms: int | None = None  # wall time of this pair's judge, verify, re-judge


class PerControlOut(BaseModel):
    title: str
    clause: str
    severity: str
    counts: dict[str, int]
    satisfied_rate: float | None


class ReportOut(BaseModel):
    """`audit.batch.BatchReport.to_dict()` — dataclass fields plus the derived lists."""

    pack_id: str
    standard: str
    generated_at: str
    ticket_ids: list[str]
    control_ids: list[str]
    rows: list[PairRowOut]
    per_control: dict[str, PerControlOut]
    readiness_score: float | None
    verified_pairs: int
    total_pairs: int
    prompt_tokens: int
    completion_tokens: int
    elapsed_ms: int = 0  # wall time of the run; 0 on reports written before it was recorded
    open_gaps: list[PairRowOut]
    review_queue: list[PairRowOut]
    errors: list[PairRowOut]


class CostOut(BaseModel):
    usd: float
    basis: str
    model: str


class RunOut(BaseModel):
    """A run in flight or finished. `report` and `cost` appear once status is `done`."""

    run_id: str
    pack_id: str
    status: Literal["running", "done", "error"]
    started_at: str
    progress: ProgressOut
    error: str | None
    source: Literal["live", "file"]
    files: dict[str, str]
    report: ReportOut | None = None
    cost: CostOut | None = None
