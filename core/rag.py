"""Advanced RAG: query decomposition + multi-query retrieval (FR-3).

Why naive RAG fails here - the problem this module exists to solve:
a control's sufficiency rubric bundles SEVERAL independent evidence
requirements. The client-comm rubric alone demands (a) a resolution comment,
(b) a logged client communication, (c) an explicit acknowledgement. Embed
that whole rubric as ONE query and you get one blurry vector - an average
of three meanings - which ranks chunks that vaguely resemble *all* of it
above chunks that precisely satisfy *one* requirement. The acknowledgement
comment you actually need can lose to a generically "compliance-flavoured"
paragraph.

The fix (query translation / multi-query retrieval): use the LLM once to
DECOMPOSE the rubric into one focused search query per evidence requirement,
run each query separately, then merge + dedupe the hits. Each sub-query is a
sharp vector, so each evidence type gets its own fair shot at ranking.

Flow (Step 7's check_ticket calls retrieve_evidence directly):

    control ──decompose_control()──> ["resolution comment recorded", ...]
        each query ──search(query, where={"ticket_id": ...})──> hits
            merge by chunk_id (keep best similarity) ──> EvidenceBundle
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass

from pydantic import BaseModel, Field, ValidationError

from core.checklists import Control
from core.llm import chat
from core.prompts import DECOMPOSE_SYSTEM, build_decompose_user_prompt
from core.retrieval import SearchHit, search


class RAGError(Exception):
    """The model failed to produce usable sub-queries (after a retry)."""


# ---------------------------------------------------------------------------
# Decomposition - one LLM call turns a rubric into focused sub-queries
# ---------------------------------------------------------------------------


class _QueryList(BaseModel):
    """Output contract for decomposition - same 'parse, don't validate' move
    as Verdict in core/judge.py: the model's reply is untrusted until it
    passes this schema. Bounds keep a rambling model from turning one
    control into 15 searches (cost) or collapsing it into 1 (defeats the
    whole point)."""

    queries: list[str] = Field(min_length=2, max_length=6)


def _parse_queries(text: str) -> list[str]:
    """First '{' to last '}' slice + JSON + schema validation (the same
    tolerant-extraction pattern as judge._parse_verdict)."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in model reply")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"reply is not valid JSON: {e}") from e
    try:
        return _QueryList.model_validate(data).queries
    except ValidationError as e:
        raise ValueError(f"JSON does not match the query-list schema: {e}") from e


# Decompositions depend only on the CONTROL, not the ticket - so when
# check_ticket sweeps 20 tickets x 9 controls, each control is decomposed
# once, not 20 times. Plain dict, process-lifetime: packs are immutable
# while the app runs, and a fresh process rebuilds it for pennies.
#
# The lock exists for coverage_report's thread pool: its first wave of
# workers all start on the SAME control, and without the lock each would
# see a cache miss and pay a duplicate decomposition LLM call. Fast path
# (cache hit) stays lock-free; only fills serialize.
_decomposition_cache: dict[str, list[str]] = {}
_decomposition_lock = threading.Lock()


def decompose_control(control: Control) -> list[str]:
    """Turn one control's rubric into 2-6 focused evidence sub-queries.

    Prompt wording lives in core/prompts.py - the rubric AND the
    expected_evidence list are deliberately both included (see
    build_decompose_user_prompt's docstring for why).

    One corrective retry on malformed output, then RAGError - same
    rationale as judge_ticket: one retry absorbs formatting slips without
    hiding a systematically broken prompt.
    """
    cached = _decomposition_cache.get(control.control_id)
    if cached is not None:
        return cached

    with _decomposition_lock:
        # Re-check under the lock: another thread may have filled this
        # control's entry while we waited (classic double-checked pattern).
        cached = _decomposition_cache.get(control.control_id)
        if cached is not None:
            return cached
        return _decompose_uncached(control)


def _decompose_uncached(control: Control) -> list[str]:
    """The decomposition LLM call itself - only reached while holding
    _decomposition_lock with a confirmed cache miss."""
    messages = [
        {"role": "system", "content": DECOMPOSE_SYSTEM},
        {"role": "user", "content": build_decompose_user_prompt(control)},
    ]
    result = chat(messages)
    try:
        queries = _parse_queries(result.content)
    except ValueError as first_error:
        messages.append({"role": "assistant", "content": result.content})
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Your reply could not be used: {first_error}. Respond again "
                    'with ONLY the JSON object {"queries": [...]}.'
                ),
            }
        )
        try:
            queries = _parse_queries(chat(messages).content)
        except ValueError as second_error:
            raise RAGError(
                f"could not decompose {control.control_id}: {second_error}"
            ) from second_error

    _decomposition_cache[control.control_id] = queries
    return queries


# ---------------------------------------------------------------------------
# Multi-query retrieval - run every sub-query, merge, dedupe
# ---------------------------------------------------------------------------


@dataclass
class EvidenceBundle:
    """Everything retrieval did for one (control, scope) - queries INCLUDED.

    Carrying the sub-queries alongside the hits is deliberate: the Streamlit
    UI's RAG-visualisation panel (FR bonus) shows the user *how* evidence
    was searched for, and the eval (Step 11) can score retrieval quality
    per sub-query. Returning only hits would throw that provenance away.
    """

    control_id: str
    queries: list[str]
    hits: list[SearchHit]


def multi_query_search(
    queries: list[str],
    k_per_query: int = 3,
    where: dict | None = None,
) -> list[SearchHit]:
    """Run several queries and merge results by chunk_id, keeping each
    chunk's BEST similarity across queries.

    Max-similarity merge is the simplest sane policy: a chunk is as relevant
    as its best-matching sub-query says it is - a chunk that scores 0.6 on
    "client acknowledgement" and 0.1 on "resolution comment" is a strong
    acknowledgement hit, and averaging would wrongly punish it for not being
    everything at once. (Reciprocal Rank Fusion is the fancier alternative;
    it only starts to matter when result lists are long and scores aren't
    comparable - revisit if we ever mix BM25 in.)
    """
    best: dict[str, SearchHit] = {}
    for query in queries:
        for hit in search(query, k=k_per_query, where=where):
            existing = best.get(hit.chunk_id)
            if existing is None or hit.similarity > existing.similarity:
                best[hit.chunk_id] = hit
    return sorted(best.values(), key=lambda h: h.similarity, reverse=True)


def retrieve_evidence(
    control: Control,
    ticket_id: str | None = None,
    k_per_query: int = 3,
) -> EvidenceBundle:
    """The full pipeline for one control: decompose -> scoped multi-search.

    ticket_id scoping applies Step 5's hard-won lesson: evidence sub-queries
    are generic by nature ("client acknowledgement approving closure"), and
    generic queries only rank reliably INSIDE a scope. Corpus-wide they'd
    surface every ticket's approvals. (Safe because every corpus email and
    change record relates to exactly one ticket - its ticket_id metadata is
    a single id, so equality matching works.)
    """
    queries = decompose_control(control)
    where = {"ticket_id": ticket_id} if ticket_id else None
    hits = multi_query_search(queries, k_per_query=k_per_query, where=where)
    return EvidenceBundle(control_id=control.control_id, queries=queries, hits=hits)
