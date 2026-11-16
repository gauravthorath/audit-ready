"""`GET /records` and `GET /records/{ticket_id}` - the evidence, as the judge reads it."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from api.deps import ViewerDep
from api.schemas import RecordSummaryOut, TicketOut
from core.tools import DEFAULT_CORPUS_DIR, load_tickets

router = APIRouter()


def ticket_summary(t: dict[str, Any]) -> dict[str, Any]:
    """The list view: header fields plus counts, never the comment bodies."""
    return {
        k: t.get(k)
        for k in ("ticket_id", "title", "type", "priority", "status", "created",
                  "closed", "reporter", "assignee")
    } | {"comments": len(t.get("comments") or []),
         "linked_changes": t.get("linked_changes") or [],
         "linked_emails": t.get("linked_emails") or []}


@router.get("/records", response_model=list[RecordSummaryOut])
def records(actor: ViewerDep) -> list[dict[str, Any]]:
    tickets = load_tickets(DEFAULT_CORPUS_DIR)
    return [ticket_summary(t) for _, t in sorted(tickets.items())]


@router.get("/records/{ticket_id}", response_model=TicketOut)
def record(ticket_id: str, actor: ViewerDep) -> dict[str, Any]:
    tickets = load_tickets(DEFAULT_CORPUS_DIR)
    if ticket_id not in tickets:
        raise HTTPException(status_code=404, detail=f"unknown ticket {ticket_id!r}")
    return tickets[ticket_id]
