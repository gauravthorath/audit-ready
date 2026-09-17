"""LangGraph per-control agent: retrieve → judge → verify → finalize | flag (CR-1).

One (ticket, control) pair goes through the graph and comes out as either a
*verified verdict* or a *flag for human review*. The graph adds CONTROL FLOW
around the judgment core/tools.check_ticket already performs; it adds no new
judgment logic of its own. Every node body is a call into core/.

    START → retrieve → judge → verify ─┬─ ok ──────────────→ finalize → END
                        ▲              ├─ issues, 1 retry ─┘
                        └──────────────┤
                                       └─ still contested ─→ flag ─────→ END

Why a graph at all, when check_ticket is a straight line (why this, CR-1):

* **Verification needs a loop.** check_ticket runs the citation check and
  merely *reports* the result. The graph acts on it: unresolved citations or
  a verifier objection go back to the judge as concrete feedback for exactly
  one targeted re-judge. A straight-line function grows if/else nests here;
  a graph makes the loop a drawn edge.
* **Flagging is a first-class outcome.** A verdict the verifier still
  contests after the retry is not silently returned as if it were clean; it
  leaves through the `flag` node with status "needs_review". The batch
  report (CR-2) surfaces those separately — the human-in-the-loop story.
* **The trace is a product feature.** Each node appends one line to
  `trace`; the Streamlit agent-trace panel (plan item 5) shows it. "Judge
  said satisfied, verifier objected: cited EML-2099 does not exist,
  re-judged to gap" is what makes an auditor trust the tool.

Design notes / trade-offs (for the reviewer and the demo):

* **Verifier = deterministic check + LLM check, combined.** check_citations
  is free, unbiased and catches phantom ids; the LLM verifier catches
  "rationale admits a gap but says satisfied". Either alone misses what the
  other catches. Cost: one extra model call per pair (~2× check_ticket).
* **Feedback carries claims, never a level.** The first version told the
  judge "a second auditor reached 'partial'". The Step 20 eval showed the
  judge adopting that level even where its own rationale contradicted it
  (approval dated before implementation, verdict "recorded afterwards"). On
  the 46 ISO 9001 pairs the false-positive rate went 0.03 -> 0.32 with the
  first pass unchanged: the loop, not the judge, caused every regression.
  So the judge now receives only checkable objections (rubric condition +
  record item) and is told the reviewer may be wrong. A bare disagreement
  becomes a level-free re-check instruction.
* **The verifier states its own level; code decides agreement.** Asking
  "do you agree?" over a rubric with a negated N/A clause made a small model
  invert the logic (7 of 9 correct ISO 27001 verdicts were parked; Step 19).
  Two independent levels compared by `==` needs no negation reasoning and
  fell to 0 of 9 parked on the same pairs.
* **Same model verifies by default; a different one is one env var away.**
  A model checking its own work shares its blind spots (the self-grading
  bias documented in assistant/README.md). We reduce it with a different role
  and a narrow task; `AUDITREADY_VERIFY_MODEL` swaps in a second model when
  cost allows. Measured, not assumed: the audit eval (plan item 4) scores
  the graph against the same 46 labelled pairs as check_ticket.
* **Exactly one re-judge.** Zero retries wastes the verifier's finding; open
  loops burn money and hide a broken prompt (the same reasoning as
  judge_ticket's single corrective retry). After one, a human decides.
* **A verifier that cannot answer routes to flag, not to finalize.** If the
  verifier's reply is malformed we do not pretend the verdict was checked.
  Failing toward human review is the conservative direction for audit tools.
* **Result keys match check_ticket.** The UI and the batch runner render
  both through the same code; the graph adds `status`, `verification`,
  `attempts`, `trace`.

Rejected: LangGraph's prebuilt ReAct agent (it lets the model choose the
next tool; our control flow is fixed by design and must be explainable to
an auditor); a checkpointer/persistence layer (no human resumes a graph
mid-run yet — CR-6 territory); asyncio (the stack is sync; the batch runner
will parallelise with a thread pool exactly as coverage_report does).
"""

from __future__ import annotations

import json
import operator
import os
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, ValidationError

from audit.prompts import VERIFY_SYSTEM, build_verify_user_prompt
from core.checklists import Control
from core.guardrails import check_citations, sanitize_record
from core.judge import JudgeResult, Verdict, judge_ticket
from core.llm import ChatResult, chat
from core.rag import retrieve_evidence
from core.tools import load_artifacts

# One targeted re-judge, then a human. See module docstring.
MAX_REJUDGE = 1

# What the judge receives when the verifier disagrees but names no concrete
# problem. Deliberately level-free: see the docstring bullet on anchoring.
RECHECK_INSTRUCTION = (
    "a second review did not confirm the verdict level but gave no specific "
    "objection: re-check every rubric condition against the record and name "
    "the item that satisfies or fails each one"
)

# None → the verifier uses the same model as the judge (cheapest). Set to a
# different OpenRouter model id to de-correlate verifier and judge errors.
VERIFY_MODEL = os.getenv("AUDITREADY_VERIFY_MODEL") or None


class GraphError(Exception):
    """The graph could not produce a result for this (ticket, control) pair."""


# ---------------------------------------------------------------------------
# The verifier's output contract — parse, don't validate, same as Verdict.
# ---------------------------------------------------------------------------


class Check(BaseModel):
    """One rubric condition, the record item it was tested against, pass/fail.

    Step 20 finding: asked for a level directly, gpt-4o-mini pattern-matched
    the rubric's PARTIAL clause ("approval recorded only afterwards") onto
    changes approved two days BEFORE deployment. Making it write both
    timestamps down first is the cheapest fix for that class of error.
    """

    condition: str
    evidence: str
    met: bool


class Verification(BaseModel):
    """What the verifier returns: its checks, its OWN verdict level, issues.

    `agrees` is derived in code from level equality (see _parse_verification)
    and is never asked of the model — a yes/no question over a negated rubric
    clause is exactly what a small model gets wrong (audit/prompts.py).
    """

    checks: list[Check] = Field(default_factory=list)
    verdict: Literal["satisfied", "partial", "gap", "not_applicable"]
    issues: list[str] = Field(default_factory=list)
    agrees: bool = False


def _parse_verification(text: str, judged_verdict: str) -> Verification:
    """Slice the JSON object out of the reply, validate it, and set `agrees`
    by comparing the verifier's level with the judge's (see
    core.judge._parse_verdict for why we slice instead of demanding purity)."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in verifier reply")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"verifier reply is not valid JSON: {e}") from e
    try:
        v = Verification.model_validate(data)
    except ValidationError as e:
        raise ValueError(f"verifier JSON does not match schema: {e}") from e
    v.agrees = v.verdict == judged_verdict
    return v


def verify_verdict(
    record: dict, control: Control, verdict: Verdict
) -> tuple[Verification | None, ChatResult]:
    """Ask the verifier model to review one verdict.

    Returns (Verification, usage). Verification is None when the reply was
    malformed — the caller routes that to `flag`, never to `finalize`.
    """
    messages = [
        {"role": "system", "content": VERIFY_SYSTEM},
        {"role": "user", "content": build_verify_user_prompt(record, control, verdict)},
    ]
    result = chat(messages, model=VERIFY_MODEL)
    try:
        return _parse_verification(result.content, verdict.verdict), result
    except ValueError:
        return None, result


# ---------------------------------------------------------------------------
# Graph state
#
# TypedDict with total=False: nodes return only the keys they change and
# LangGraph merges them. The Annotated reducers make `trace` and the token
# counters ACCUMULATE across nodes (and across the re-judge loop) instead of
# being overwritten — that is the whole reason they are declared this way.
# ---------------------------------------------------------------------------


class ControlState(TypedDict, total=False):
    # inputs
    ticket: dict
    control: Control
    # retrieve
    record: dict
    security_flags: list[dict[str, Any]]
    evidence_queries: list[str]
    evidence_chunks: list[str]
    # judge
    judged: JudgeResult
    attempts: int
    first_verdict: str | None  # level of judge #1; a later flip is never auto-finalized
    feedback: str | None
    # verify
    citation_check: dict[str, Any]
    verification: dict[str, Any]
    # finalize / flag
    status: Literal["verdict", "needs_review"]
    # accumulated
    trace: Annotated[list[str], operator.add]
    prompt_tokens: Annotated[int, operator.add]
    completion_tokens: Annotated[int, operator.add]


# ---------------------------------------------------------------------------
# Nodes — each wraps a core/ call and returns the keys it changed
# ---------------------------------------------------------------------------


_ARTIFACTS: dict[str, dict] | None = None


def _artifacts() -> dict[str, dict]:
    global _ARTIFACTS
    if _ARTIFACTS is None:
        _ARTIFACTS = load_artifacts()
    return _ARTIFACTS


def retrieve(state: ControlState) -> ControlState:
    """Scoped multi-query retrieval + quarantine, identical to check_ticket."""
    ticket, control = state["ticket"], state["control"]
    bundle = retrieve_evidence(control, ticket_id=ticket["ticket_id"])
    # Supporting evidence only — chunks that ARE the ticket would duplicate
    # the full ticket JSON and pay tokens twice for the same facts.
    supporting = [
        {"chunk_id": h.chunk_id, "text": h.text}
        for h in bundle.hits
        if h.metadata.get("doc_type") not in ("ticket", "ticket_comment")
    ]
    # Linkage is structure, not similarity: whatever the ticket explicitly links
    # is joined by id, so the judge and verifier always see the approval record
    # they are asked to time-check. Similarity retrieval still adds the rest.
    artifacts = _artifacts()
    linked_ids = list(ticket.get("linked_changes") or []) + list(ticket.get("linked_emails") or [])
    linked = [artifacts[i] for i in linked_ids if i in artifacts]
    record, flags = sanitize_record(
        {
            "ticket": ticket,
            "linked_records": linked or "none linked",
            "retrieved_supporting_evidence": supporting or "none found",
        }
    )
    return {
        "record": record,
        "security_flags": flags,
        "evidence_queries": bundle.queries,
        "evidence_chunks": [h.chunk_id for h in bundle.hits],
        "attempts": 0,
        "feedback": None,
        "trace": [
            f"retrieve: {len(bundle.queries)} sub-queries → {len(bundle.hits)} chunks "
            f"({len(supporting)} supporting), {len(linked)} linked record(s) joined by id, "
            f"{len(flags)} string(s) quarantined"
        ],
    }


def judge(state: ControlState) -> ControlState:
    """First pass, or a targeted re-judge carrying the verifier's feedback."""
    feedback = state.get("feedback")
    judged = judge_ticket(state["record"], state["control"], feedback=feedback)
    attempts = state.get("attempts", 0) + 1
    note = " (re-judge with reviewer feedback)" if feedback else ""
    return {
        "judged": judged,
        "attempts": attempts,
        "first_verdict": judged.verdict.verdict if attempts == 1 else state.get("first_verdict"),
        "prompt_tokens": judged.prompt_tokens,
        "completion_tokens": judged.completion_tokens,
        "trace": [
            f"judge #{attempts}{note}: {judged.verdict.verdict}; "
            f"cites {judged.verdict.citations or 'nothing'}"
        ],
    }


def verify(state: ControlState) -> ControlState:
    """Deterministic citation check + LLM review. Produces `feedback` (None
    when clean) that the router turns into finalize / judge / flag."""
    judged, control, record = state["judged"], state["control"], state["record"]
    verdict = judged.verdict

    citation_check = check_citations(
        verdict.citations, verdict.verdict, control.citation_required, record
    )
    verification, usage = verify_verdict(record, control, verdict)

    issues: list[str] = []
    if citation_check["status"] != "ok":
        detail = citation_check["note"]
        if citation_check["unresolved"]:
            detail += f" (unresolved: {', '.join(citation_check['unresolved'])})"
        issues.append(f"citation check {citation_check['status']}: {detail}")
    if verification is not None and not verification.agrees:
        # Claims only, never the verifier's level: fed the level, the judge
        # adopted it (Step 20 eval, ISO 9001 FP rate 0.03 -> 0.32).
        claims = [i for i in verification.issues if i.strip()]
        issues.extend(claims or [RECHECK_INSTRUCTION])

    feedback = "\n".join(f"- {i}" for i in issues) if issues else None
    if verification is None:
        summary = "verifier reply malformed → cannot verify"
    else:
        summary = ("verifier agrees" if verification.agrees
                   else f"verifier says {verification.verdict}")
    return {
        "citation_check": citation_check,
        "verification": {
            "agrees": None if verification is None else verification.agrees,
            "verdict": None if verification is None else verification.verdict,
            "checks": [] if verification is None else [c.model_dump() for c in verification.checks],
            "issues": [] if verification is None else verification.issues,
            "model": usage.model,
        },
        "feedback": feedback,
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "trace": [
            f"verify: citations {citation_check['status']}; {summary}"
            + (f" — {len(issues)} issue(s)" if issues else "")
        ],
    }


def finalize(state: ControlState) -> ControlState:
    return {"status": "verdict", "trace": ["finalize: verified verdict"]}


def flag(state: ControlState) -> ControlState:
    if state["verification"]["agrees"] is None:
        reason = "verifier could not be parsed"
    elif not state.get("feedback") and verdict_flipped(state):
        reason = (
            f"verdict changed under review ({state['first_verdict']} → "
            f"{state['judged'].verdict.verdict}); a human confirms flips"
        )
    else:
        reason = f"still contested after {state['attempts']} judge pass(es)"
    return {"status": "needs_review", "trace": [f"flag: needs human review — {reason}"]}


# ---------------------------------------------------------------------------
# Routing — the only "intelligence" the graph adds, and it is a pure function
# ---------------------------------------------------------------------------


def verdict_flipped(state: ControlState) -> bool:
    """True when a re-judge landed on a different level than judge #1."""
    first = state.get("first_verdict")
    judged = state.get("judged")
    return (
        state.get("attempts", 1) > 1
        and first is not None
        and judged is not None
        and judged.verdict.verdict != first
    )


def route_after_verify(state: ControlState) -> Literal["finalize", "judge", "flag"]:
    """Clean → finalize. Issues with a retry left → judge. Otherwise → flag.

    One extra rule (Step 20): a verdict that CHANGED after reviewer feedback
    is flagged even when the verifier now agrees. Once the judge has read the
    verifier's claims the two opinions are no longer independent, and the
    eval showed the judge folding to wrong claims while its own rationale
    contradicted them ("approval precedes deployment ... however, approval
    was recorded only afterwards"). Two noisy models agreeing AFTER one
    talked to the other is the least trustworthy agreement there is — so a
    flip goes to the human with both opinions attached. Holding firm through
    a review, or a verifier that comes around, still finalizes.
    """
    if state["verification"]["agrees"] is None:
        return "flag"
    if not state.get("feedback"):
        return "flag" if verdict_flipped(state) else "finalize"
    if state["attempts"] <= MAX_REJUDGE:
        return "judge"
    return "flag"


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def build_graph():
    """Wire nodes and edges; returns the compiled, reusable graph."""
    g = StateGraph(ControlState)
    g.add_node("retrieve", retrieve)
    g.add_node("judge", judge)
    g.add_node("verify", verify)
    g.add_node("finalize", finalize)
    g.add_node("flag", flag)

    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "judge")
    g.add_edge("judge", "verify")
    g.add_conditional_edges(
        "verify",
        route_after_verify,
        {"finalize": "finalize", "judge": "judge", "flag": "flag"},
    )
    g.add_edge("finalize", END)
    g.add_edge("flag", END)
    return g.compile()


_graph = None


def _compiled():
    # Compile once per process; a compiled graph without a checkpointer holds
    # no per-run state, so one instance is safe to share across threads.
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def run_control(ticket: dict, control: Control) -> dict[str, Any]:
    """Run one (ticket, control) pair through the graph.

    Returns a dict with the same keys as core.tools.check_ticket plus:
      status        "verdict" | "needs_review"
      verification  {"agrees": bool|None, "verdict": str|None,
                     "checks": [{condition, evidence, met}], "issues": [...], "model": str}
      attempts      number of judge passes (1 or 2)
      trace         one line per node visit, in order
    """
    final = _compiled().invoke(
        {"ticket": ticket, "control": control, "trace": [], "prompt_tokens": 0,
         "completion_tokens": 0}
    )
    if "judged" not in final or "status" not in final:
        raise GraphError(
            f"graph ended without a result for {ticket.get('ticket_id')} × "
            f"{control.control_id}: trace={final.get('trace')}"
        )
    judged: JudgeResult = final["judged"]
    return {
        "ticket_id": ticket["ticket_id"],
        "control_id": control.control_id,
        "control_title": control.title,
        "severity": control.severity,
        "verdict": judged.verdict.verdict,
        "rationale": judged.verdict.rationale,
        "citations": judged.verdict.citations,
        "citation_check": final["citation_check"],
        "security_flags": final["security_flags"],
        "evidence_queries": final["evidence_queries"],
        "evidence_chunks": final["evidence_chunks"],
        "prompt_tokens": final["prompt_tokens"],
        "completion_tokens": final["completion_tokens"],
        "status": final["status"],
        "verification": final["verification"],
        "attempts": final["attempts"],
        "trace": final["trace"],
    }
