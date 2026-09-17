"""The three domain tools (FR-5, FR-6, FR-7): find_evidence, check_ticket,
coverage_report.

THE architecture-critical rule of this module (FR-7): these are PLAIN
functions with JSON-serializable inputs/outputs and ZERO LangChain imports.
Step 9 wraps them as LangChain tools in assistant/agent.py; v2's
LangGraph agent and batch report engine (CR-1, CR-2) call the very same
functions. The framework is a delivery detail; the capability lives here.

Why dicts (not Pydantic models) as RETURN types: these returns cross the
tool boundary — LangChain serializes tool output into the model's context,
the Streamlit UI renders it, the eval harness records it. A plain dict is
already all three. (Inputs still get validated — by the functions they
delegate to; outputs were validated at birth in judge/rag.)

The audit philosophy encoded in check_ticket:
  the TICKET is the record under audit (given, not searched for) —
  RETRIEVAL finds the *supporting* artifacts (emails, change records) the
  rubric needs but the ticket only references. That mirrors how a human
  auditor works: record open on the desk, chasing its references.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

import json
import os

from core.checklists import ChecklistPack, load_pack
from core.guardrails import check_citations, sanitize_record, scan_injection
from core.judge import judge_ticket
from core.rag import retrieve_evidence
from core.retrieval import search

DEFAULT_PACK_PATH = Path("assistant/data/checklists/iso9001.yaml")
# Overridable so the whole app — chat tools included — can be pointed at a
# connector snapshot (CR-3) instead of the synthetic corpus.
DEFAULT_CORPUS_DIR = Path(os.getenv("AUDITREADY_CORPUS_DIR", "assistant/data/corpus"))

# Score weights for the coverage report: satisfied earns full credit,
# partial half, gap none; not_applicable is EXCLUDED from the denominator
# (an inapplicable control can neither help nor hurt readiness).
# Public so v2 batch runner scores with the SAME policy as
# coverage_report — one scoring rule, stated once (CR-2 reuses, not redefines).
VERDICT_CREDIT = {"satisfied": 1.0, "partial": 0.5, "gap": 0.0}
_VERDICT_CREDIT = VERDICT_CREDIT

# A major control failing should dent the score three times harder than a
# minor one — the 3:1 ratio is a policy choice, stated here so it's visible
# and arguable, not buried in arithmetic.
SEVERITY_WEIGHT = {"major": 3.0, "minor": 1.0}
_SEVERITY_WEIGHT = SEVERITY_WEIGHT


# ---------------------------------------------------------------------------
# Lazy, cached data access — same pattern as the LLM client and the
# embedding model: pay for loading once, on first use, never at import.
# ---------------------------------------------------------------------------

_pack_cache: ChecklistPack | None = None
_tickets_cache: dict[str, dict] | None = None


def _pack() -> ChecklistPack:
    global _pack_cache
    if _pack_cache is None:
        _pack_cache = load_pack(DEFAULT_PACK_PATH)
    return _pack_cache


def load_tickets(corpus_dir: str | Path = DEFAULT_CORPUS_DIR) -> dict[str, dict]:
    """tickets.json → {ticket_id: ticket}. Uncached; callers that want a
    different corpus (v2 batch runs, tests) pass their own dir."""
    raw = json.loads((Path(corpus_dir) / "tickets.json").read_text())
    return {t["ticket_id"]: t for t in raw}


def load_artifacts(corpus_dir: str | Path = DEFAULT_CORPUS_DIR) -> dict[str, dict]:
    """Change records and emails keyed by id (CHG-…, EML-…). Uncached.

    For joining a ticket's `linked_changes` / `linked_emails` BY ID. Linkage is
    structure, not similarity: an approval record must never look "missing"
    just because no retrieval sub-query happened to pull its chunk (Step 20
    eval: three finalized wrong verdicts traced to exactly that).
    """
    corpus_dir = Path(corpus_dir)
    out: dict[str, dict] = {}
    for c in json.loads((corpus_dir / "changes.json").read_text()):
        out[c["change_id"]] = c
    for e in json.loads((corpus_dir / "emails.json").read_text()):
        out[e["email_id"]] = e
    return out


def _tickets() -> dict[str, dict]:
    global _tickets_cache
    if _tickets_cache is None:
        _tickets_cache = load_tickets(DEFAULT_CORPUS_DIR)
    return _tickets_cache


# ---------------------------------------------------------------------------
# Tool 1 — find_evidence (FR-5): semantic search, agent-friendly shape
# ---------------------------------------------------------------------------


def find_evidence(
    query: str,
    ticket_id: str | None = None,
    doc_type: str | None = None,
    k: int = 5,
) -> list[dict[str, Any]]:
    """Search the corpus for evidence matching a natural-language query.

    Optional filters (they AND together): ticket_id scopes to one ticket's
    cluster (the ticket, its comments, its linked emails/changes); doc_type
    narrows to 'ticket' | 'ticket_comment' | 'email' | 'change'.

    Returns are deliberately flat and small: chunk id (= citation handle),
    text, similarity, and the provenance fields the UI shows. The agent
    will paste these straight into its context — everything here is
    something we're happy to spend tokens on.
    """
    filters = []
    if ticket_id:
        filters.append({"ticket_id": ticket_id})
    if doc_type:
        filters.append({"doc_type": doc_type})
    # Chroma wants exactly one operator at the top level: a bare dict for
    # one condition, an explicit $and for two.
    where = filters[0] if len(filters) == 1 else {"$and": filters} if filters else None

    results = []
    for hit in search(query, k=k, where=where):
        # Guardrail (FR-13): search results go straight into the agent's
        # context, so they get the same scan-and-quarantine treatment as
        # judge input. The metadata stays intact — the hit remains findable
        # and citable, only the payload text is withheld.
        clean_text, flags = sanitize_record(hit.text, _path=hit.chunk_id)
        results.append(
            {
                "chunk_id": hit.chunk_id,
                "text": clean_text,
                "similarity": round(hit.similarity, 3),
                "doc_type": hit.metadata.get("doc_type"),
                "doc_id": hit.metadata.get("doc_id"),
                **({"security_flags": flags} if flags else {}),
            }
        )
    return results


# ---------------------------------------------------------------------------
# Tool 2 — check_ticket (FR-6): the heart. Steps 4+5+6 composed.
# ---------------------------------------------------------------------------


def check_ticket(ticket_id: str, control_id: str | None = None) -> dict[str, Any]:
    """Judge one ticket against one control — or against every control in
    the pack when control_id is omitted.

    Per (ticket, control) pair:
      1. retrieve_evidence(): the control's cached sub-queries run scoped
         to this ticket, surfacing SUPPORTING artifacts (linked emails,
         change records) the raw ticket JSON only references by id. This
         is what the walking skeleton was missing: TCK-1007's approval
         email exists, but Step 4 alone could never see it.
      2. judge_ticket() on a composite record: the full ticket (the record
         under audit is a given, not a search result) + the retrieved
         supporting chunks, each keyed by its chunk_id so the model can
         cite retrieval results precisely.

    Returns a dict with the verdict, rationale, citations, the evidence
    trail (sub-queries + chunk ids — the UI's RAG-visualisation feed), and
    token counts (FR-17).
    """
    ticket = _tickets().get(ticket_id)
    if ticket is None:
        return {"error": f"unknown ticket_id {ticket_id!r}"}

    controls = (
        [_pack().get_control(control_id)] if control_id else list(_pack().controls)
    )

    results = []
    for control in controls:
        bundle = retrieve_evidence(control, ticket_id=ticket_id)
        # Supporting evidence only: chunks that ARE the ticket (header,
        # comments) would duplicate the full ticket JSON below and pay
        # tokens twice for the same facts.
        supporting = [
            {"chunk_id": h.chunk_id, "text": h.text}
            for h in bundle.hits
            if h.metadata.get("doc_type") not in ("ticket", "ticket_comment")
        ]
        record = {
            "ticket": ticket,
            "retrieved_supporting_evidence": supporting or "none found",
        }
        # Guardrail layer 2 (FR-13): the ONE choke point every string passes
        # through before reaching the judging prompt — the poisoned comment
        # in the ticket JSON and any payload in retrieved chunks both get
        # quarantined here. The corpus file itself stays untouched; we
        # sanitize the copy that enters the prompt.
        record, security_flags = sanitize_record(record)
        judged = judge_ticket(record, control)
        # Guardrail layer 3 (FR-14): a verdict that cites nothing (or cites
        # phantoms) gets flagged — the backstop that catches a hijacked
        # "everything satisfied, cite nothing" response even if the scan
        # above missed the payload.
        citation_check = check_citations(
            judged.verdict.citations,
            judged.verdict.verdict,
            control.citation_required,
            record,
        )
        results.append(
            {
                "ticket_id": ticket_id,
                "control_id": control.control_id,
                "control_title": control.title,
                "severity": control.severity,
                "verdict": judged.verdict.verdict,
                "rationale": judged.verdict.rationale,
                "citations": judged.verdict.citations,
                "citation_check": citation_check,
                "security_flags": security_flags,
                "evidence_queries": bundle.queries,
                "evidence_chunks": [h.chunk_id for h in bundle.hits],
                "prompt_tokens": judged.prompt_tokens,
                "completion_tokens": judged.completion_tokens,
            }
        )

    return results[0] if control_id else {"ticket_id": ticket_id, "results": results}


# ---------------------------------------------------------------------------
# Tool 3 — coverage_report (FR-6): the aggregate view
# ---------------------------------------------------------------------------


def coverage_report(
    ticket_ids: list[str] | None = None,
    control_ids: list[str] | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    max_workers: int = 4,
) -> dict[str, Any]:
    """Audit-readiness rollup: every requested ticket judged against every
    requested control, aggregated per control and overall.

    Defaults to the WHOLE corpus x pack — that's 20 x 9 = 180 LLM calls, so
    callers (and the agent's tool description in Step 9) should scope it.
    The readiness score is a severity-weighted average of verdict credit
    (satisfied 1.0 / partial 0.5 / gap 0.0); not_applicable pairs drop out
    of the denominator entirely.

    Each (ticket, control) judgment is independent, so they run on a small
    thread pool: the work is ~all network wait on the LLM, which threads
    overlap fine despite the GIL. max_workers=4 is deliberately modest —
    enough to cut an unscoped sweep from ~12 min to ~3 without tripping
    provider rate limits (the batch engine proper is CR-2).

    progress, if given, is called from the CALLER'S thread after each
    completed judgment as progress(done, total, "TCK-1007 × <control>") —
    a plain callable, so core/ stays framework-free (FR-7); the Streamlit
    layer turns it into a progress bar (FR-16).
    """
    ticket_ids = ticket_ids or list(_tickets().keys())
    control_ids = control_ids or [c.control_id for c in _pack().controls]

    # Resolve all controls up front: an unknown id fails HERE with the
    # valid-ids KeyError before any tokens are spent — and touching the
    # lazy pack/ticket caches now means worker threads only ever read them.
    controls = {cid: _pack().get_control(cid) for cid in control_ids}
    _tickets()

    pairs = [(t, c) for c in control_ids for t in ticket_ids]
    results: dict[tuple[str, str], dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(check_ticket, t, c): (t, c) for t, c in pairs}
        for done, future in enumerate(as_completed(futures), start=1):
            pair = futures[future]
            results[pair] = future.result()  # a failed judgment propagates
            if progress is not None:
                progress(done, len(pairs), f"{pair[0]} × {pair[1]}")

    # Aggregate in the deterministic requested order (not completion order),
    # so the report is identical to what the old sequential loop produced.
    per_control: dict[str, dict[str, Any]] = {}
    weighted_credit = weighted_total = 0.0
    total_prompt_tokens = total_completion_tokens = 0

    for control_id in control_ids:
        control = controls[control_id]
        counts = {"satisfied": 0, "partial": 0, "gap": 0, "not_applicable": 0}
        failing: list[dict[str, str]] = []

        for ticket_id in ticket_ids:
            result = results[(ticket_id, control_id)]
            total_prompt_tokens += result["prompt_tokens"]
            total_completion_tokens += result["completion_tokens"]
            verdict = result["verdict"]
            counts[verdict] += 1
            if verdict in ("partial", "gap"):
                failing.append(
                    {
                        "ticket_id": ticket_id,
                        "verdict": verdict,
                        "rationale": result["rationale"],
                    }
                )
            if verdict != "not_applicable":
                weight = _SEVERITY_WEIGHT[control.severity]
                weighted_credit += _VERDICT_CREDIT[verdict] * weight
                weighted_total += weight

        applicable = sum(counts.values()) - counts["not_applicable"]
        per_control[control_id] = {
            "title": control.title,
            "severity": control.severity,
            "counts": counts,
            "satisfied_rate": (
                round(counts["satisfied"] / applicable, 3) if applicable else None
            ),
            "failing_tickets": failing,
        }

    return {
        "tickets_checked": ticket_ids,
        "controls_checked": control_ids,
        "per_control": per_control,
        # One number for the executive summary; the per_control table is
        # where the actual work lives.
        "readiness_score": (
            round(weighted_credit / weighted_total, 3) if weighted_total else None
        ),
        "prompt_tokens": total_prompt_tokens,
        "completion_tokens": total_completion_tokens,
    }
