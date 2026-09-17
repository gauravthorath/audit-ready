"""FastAPI dependencies: how a route reaches the things the app owns (CR-8).

Routes never import a global. They declare what they need — the run registry,
the session store, the price of the model, a pack by id — and FastAPI hands it
to them from `app.state`. That is the whole reason the test suite can swap the
LangGraph agent, the pricing fetch, the reports directory and the chat agent
without patching a private name: it assigns a different object to `app.state`
and every route sees it on the next request.

Everything here is a one-liner on purpose. A dependency that grows logic is a
dependency that has started to compute a verdict, and `api/` holds none.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer

from api.auth import ROLE_AUDITOR, ROLE_VIEWER, Actor, Authenticator
from api.ratelimit import RateLimits
from api.runs import Rate, RunRegistry
from api.sessions import SessionStore
from api.settings import Settings
from audit.reporting import discover_packs
from core.checklists import ChecklistPack
from core.llm import get_model_pricing
from core.spend import Budgets, Ledger


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_runs(request: Request) -> RunRegistry:
    return request.app.state.runs


def get_sessions(request: Request) -> SessionStore:
    return request.app.state.sessions


def get_runner(request: Request) -> Callable[..., dict[str, Any]]:
    """The per-pair judge. `audit.graph.run_control` in production, a fake in tests."""
    return request.app.state.runner


def get_agent_factory(request: Request) -> Callable[[], Any]:
    """What makes a chat agent. `assistant.agent.AuditAgent` in production, a fake in tests."""
    return request.app.state.agent_factory


def get_pricing(request: Request) -> Callable[[str], Rate]:
    """model id -> (prompt, completion) USD per million, or None for the list-price fallback."""
    return request.app.state.pricing


def get_ledger(request: Request) -> Ledger:
    return request.app.state.ledger


def get_limits(request: Request) -> RateLimits:
    return request.app.state.limits


def get_budgets(request: Request) -> Budgets:
    s: Settings = request.app.state.settings
    return Budgets(deployment_daily_usd=s.daily_budget_usd, actor_daily_usd=s.actor_daily_budget_usd)


def get_auth(request: Request) -> Authenticator:
    return request.app.state.auth


# Declared with `auto_error=False` so the OpenAPI document advertises the
# bearer scheme (a generated client knows to send one) while `AUDITREADY_AUTH=none`
# keeps working without a credential. Whether a token is *required* is a
# property of the deployment, not of the route.
_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="AuditReadyBearer",
    description="Required when AUDITREADY_AUTH is token or oidc; ignored when it is none.",
)


def current_actor(request: Request, _credentials=Depends(_bearer)) -> Actor:
    """Who is calling. The installed authenticator decides; the route never knows which.

    Everything downstream keys on `actor.id`: the rate limiter, the spend
    ledger, and the owner of a run. With `AUDITREADY_AUTH=none` that id is the
    client address, which is exactly as trustworthy as it sounds — behind a
    proxy every caller shares one. That is the reason this seam exists.
    """
    return get_auth(request).authenticate(request)


def require_role(role: str) -> Callable[[Actor], Actor]:
    """A dependency that admits only actors holding `role`, and says so plainly (FR-15)."""
    def dependency(actor: Annotated[Actor, Depends(current_actor)]) -> Actor:
        if not actor.may(role):
            raise HTTPException(
                status_code=403,
                detail=f"This needs the {role} role, and your account does not have it.",
            )
        return actor
    return dependency


@lru_cache(maxsize=8)
def cached_pricing(model: str) -> Rate:
    """Live OpenRouter rate, fetched once per model per process.

    Same policy as the Streamlit pages (one fetch per model per hour there);
    a process lifetime is a coarser cache but the same idea. A failed fetch is
    a None, which `estimate_cost_usd` turns into the list-price fallback.
    """
    try:
        return get_model_pricing(model)
    except Exception:  # noqa: BLE001 — pricing is a courtesy, never a failure
        return None


def pack_by_id(pack_id: str) -> ChecklistPack:
    """Resolve a pack by its declared id, never by a client-supplied path.

    Taking an id and looking it up in the checklist directory is what keeps a
    browser from asking this process to load an arbitrary YAML file.
    """
    for entry in discover_packs():
        pack = entry["pack"]
        if pack is not None and pack.pack.id == pack_id:
            return pack
    raise HTTPException(status_code=404, detail=f"unknown pack {pack_id!r}")


SettingsDep = Annotated[Settings, Depends(get_settings)]
AuthDep = Annotated[Authenticator, Depends(get_auth)]
RunsDep = Annotated[RunRegistry, Depends(get_runs)]
SessionsDep = Annotated[SessionStore, Depends(get_sessions)]
RunnerDep = Annotated[Callable[..., dict[str, Any]], Depends(get_runner)]
AgentFactoryDep = Annotated[Callable[[], Any], Depends(get_agent_factory)]
PricingDep = Annotated[Callable[[str], Rate], Depends(get_pricing)]
LedgerDep = Annotated[Ledger, Depends(get_ledger)]
LimitsDep = Annotated[RateLimits, Depends(get_limits)]
BudgetsDep = Annotated[Budgets, Depends(get_budgets)]
ActorDep = Annotated[Actor, Depends(current_actor)]
AuditorDep = Annotated[Actor, Depends(require_role(ROLE_AUDITOR))]
ViewerDep = Annotated[Actor, Depends(require_role(ROLE_VIEWER))]
