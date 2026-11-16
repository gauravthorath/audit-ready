"""`GET /spend` - what today has cost, what it may cost, and the limits in force (FR-17).

The number a person paying for the key wants on the screen, next to the model
name: spent today, the budget, what remains. `remaining_usd` is the tighter of
the per-caller and the deployment cap, or null when neither is set.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from api.deps import BudgetsDep, LedgerDep, LimitsDep, PricingDep, ViewerDep
from api.schemas import SpendOut
from core.spend import remaining_usd
from assistant.agent import DEFAULT_MODEL

router = APIRouter()


@router.get("/spend", response_model=SpendOut)
def spend(actor: ViewerDep, ledger: LedgerDep, budgets: BudgetsDep, limits: LimitsDep,
          pricing: PricingDep) -> dict[str, Any]:
    rate = pricing(DEFAULT_MODEL)
    return {
        "actor": actor.name,
        "today_usd": ledger.today(actor.id),
        "actor_budget_usd": budgets.actor_daily_usd,
        "deployment_today_usd": ledger.today(None),
        "deployment_budget_usd": budgets.deployment_daily_usd,
        "remaining_usd": remaining_usd(ledger, budgets, actor.id),
        "model": DEFAULT_MODEL,
        "basis": "live OpenRouter rate" if rate else "fallback list price",
        "limits": {
            "chat_per_minute": limits.chat.limit,
            "audits_per_hour": limits.audit.limit,
            "max_pairs_per_run": limits.max_pairs_per_run,
        },
    }
