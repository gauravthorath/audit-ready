"""`POST /chat`, `DELETE /chat/{session_id}` - one conversational turn at a time (FR-8, FR-15).

*Why chat is synchronous and audit is not.* A chat turn is one to three model
calls and returns in seconds; a browser can wait on it. A batch audit is
hundreds of calls over minutes - see `routers/audit.py` for the other half of
that decision.

*The error mapping is the contract.* It follows the Streamlit page exactly
(FR-15): a guardrail violation is safe to show verbatim (400); the model being
down is a 503 with a plain sentence; anything else is a 500 with no detail,
because a stack trace in a browser is a leak, not a diagnostic.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api.deps import (
    AgentFactoryDep, AuditorDep, BudgetsDep, LedgerDep, LimitsDep, PricingDep, SessionsDep,
    ViewerDep,
)
from api.schemas import ChatOut, EndChatOut, SessionSummaryOut
from api.sessions import SessionRecord
from audit.reporting import estimate_cost_usd
from core.guardrails import GuardrailViolation
from core.llm import LLMError
from core.spend import budget_refusal
from core.tracing import bind
from assistant.agent import DEFAULT_MODEL

router = APIRouter()


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None


@router.post("/chat", response_model=ChatOut)
def chat(
    req: ChatRequest,
    sessions: SessionsDep,
    new_agent: AgentFactoryDep,
    pricing: PricingDep,
    actor: AuditorDep,
    limits: LimitsDep,
    ledger: LedgerDep,
    budgets: BudgetsDep,
) -> dict[str, Any]:
    """One conversational turn. The agent keeps memory across turns of a session.

    Errors follow the Streamlit page exactly (FR-15): a guardrail violation is
    safe to show verbatim (400); the model being down is a 503 with a plain
    sentence; anything else is a 500 with no detail, because a stack trace in a
    browser is a leak, not a diagnostic. Over the rate limit or the daily
    budget is a 429 that says so - refused before a token is spent.
    """
    limits.chat.check(actor.id, "chat turns")  # the caller, not the session: a new
    # session id is free to mint, so a per-session limit would limit nothing.
    refusal = budget_refusal(ledger, budgets, actor.id)
    if refusal:
        raise HTTPException(status_code=429, detail=refusal)

    # A session id the caller does not own simply is not found, and this turn
    # starts a conversation of their own under it. Nobody can continue someone
    # else's by guessing an id (Step 41).
    session_id = req.session_id or uuid.uuid4().hex
    record = sessions.get(actor.id, session_id)
    try:
        agent = new_agent()
    except Exception as exc:  # missing key etc. - say so, don't crash
        raise HTTPException(status_code=503, detail=f"agent unavailable: {exc}") from exc
    if record is not None:
        agent.restore(record.state)  # the memory, rebuilt; the API holds none between turns

    try:
        with bind(session_id=session_id):
            reply = agent.ask(req.question)
    except GuardrailViolation as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(
            status_code=503, detail=f"The language model is unavailable: {exc}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail="Something went wrong answering that. Please try again or rephrase.",
        ) from exc

    # Stored only after the model answered: a turn that was refused or that
    # died mid-flight must not leave a dangling question in the history.
    sessions.put(SessionRecord(session_id=session_id, actor=actor.id, state=agent.snapshot()))

    rate = pricing(DEFAULT_MODEL)
    turn_usd, _ = estimate_cost_usd(reply.prompt_tokens, reply.completion_tokens, rate)
    ledger.record(actor.id, "chat", reply.prompt_tokens, reply.completion_tokens, turn_usd,
                  ref=session_id)
    cost, note = estimate_cost_usd(agent.total_prompt_tokens, agent.total_completion_tokens, rate)
    return {
        "session_id": session_id,
        "answer": reply.content,
        "tool_calls": reply.tool_calls,
        "prompt_tokens": reply.prompt_tokens,
        "completion_tokens": reply.completion_tokens,
        "session": {
            "prompt_tokens": agent.total_prompt_tokens,
            "completion_tokens": agent.total_completion_tokens,
            "cost_usd": round(cost, 4),
            "cost_basis": note,
            "model": DEFAULT_MODEL,
        },
    }


@router.get("/chat", response_model=list[SessionSummaryOut])
def my_chats(sessions: SessionsDep, actor: ViewerDep) -> list[dict[str, Any]]:
    """Your conversations, newest first. Never anyone else's."""
    return [
        {"session_id": r.session_id, "title": r.title, "turns": r.turns,
         "created_at": r.created_at, "updated_at": r.updated_at}
        for r in sessions.list(actor.id)
    ]


@router.delete("/chat/{session_id}", response_model=EndChatOut)
def end_chat(session_id: str, sessions: SessionsDep, actor: ViewerDep) -> dict[str, Any]:
    return {"session_id": session_id, "ended": sessions.delete(actor.id, session_id)}
