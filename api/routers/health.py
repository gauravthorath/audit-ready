"""`GET /health` — is the API up, and what is it wired to."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter

from api.deps import AuthDep, SessionsDep, SettingsDep
from api.schemas import HealthOut
from core.retrieval import store_label
from core.tools import DEFAULT_CORPUS_DIR
from assistant.agent import DEFAULT_MODEL

router = APIRouter()


@router.get("/health", response_model=HealthOut)
def health(settings: SettingsDep, auth: AuthDep, sessions: SessionsDep) -> dict[str, Any]:
    """Unauthenticated on purpose: a health probe that needs a secret is not a health probe.

    The container's HEALTHCHECK and any orchestrator call this, so it must
    answer before a credential exists. It reports `auth` so "is this deployment
    open to anyone?" is answerable without reading someone's environment.
    """
    return {
        "auth": auth.mode,
        "sessions": sessions.kind,
        "ok": True,
        "model": DEFAULT_MODEL,
        "corpus_dir": str(DEFAULT_CORPUS_DIR),
        "vector_store": store_label(),
        "web_origins": settings.web_origins,
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
