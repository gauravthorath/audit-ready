"""`GET /packs` and `GET /pricing` - the catalogue and the price list, both read-only."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from api.deps import PricingDep, ViewerDep
from api.schemas import PackOut, PricingOut
from audit.reporting import discover_packs
from assistant.agent import DEFAULT_MODEL

router = APIRouter()


def pack_json(entry: dict[str, Any]) -> dict[str, Any]:
    """The wire shape of one discovered pack, valid or not.

    An invalid pack is listed with its error rather than hidden: a YAML typo
    that silently drops a standard from the picker is a bug nobody reports.
    """
    pack = entry["pack"]
    if pack is None:
        return {
            "id": None, "label": entry["label"], "path": entry["path"].name,
            "error": entry["error"], "controls": [],
        }
    return {
        "id": pack.pack.id,
        "standard": pack.pack.standard,
        "version": pack.pack.version,
        "description": pack.pack.description,
        "label": entry["label"],
        "path": entry["path"].name,
        "error": None,
        "controls": [
            {
                "control_id": c.control_id,
                "clause": c.clause,
                "title": c.title,
                "severity": c.severity,
                "description": c.description,
                "expected_evidence": c.expected_evidence,
                "citation_required": c.citation_required,
            }
            for c in pack.controls
        ],
    }


@router.get("/packs", response_model=list[PackOut])
def packs(actor: ViewerDep) -> list[dict[str, Any]]:
    """Every checklist pack, invalid ones included with their error (not hidden)."""
    return [pack_json(e) for e in discover_packs()]


@router.get("/pricing", response_model=PricingOut)
def pricing(pricing: PricingDep, actor: ViewerDep, model: str | None = None) -> dict[str, Any]:
    model = model or DEFAULT_MODEL
    rate = pricing(model)
    return {
        "model": model,
        "prompt_usd_per_m": rate[0] if rate else None,
        "completion_usd_per_m": rate[1] if rate else None,
        "source": "openrouter" if rate else "fallback list price",
    }
