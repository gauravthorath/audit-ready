"""Walking skeleton: judge ONE ticket against ONE control (no RAG yet).

This is the conceptual heart of AuditReady: given a record and a control's
sufficiency rubric, get the LLM to return a *structured, defensible* verdict -
satisfied / partial / gap - with a rationale and citations back to the record.

Why build this before retrieval, tools, or UI (the "walking skeleton" idea):
the riskiest question in the whole project is "can an LLM actually apply a
sufficiency rubric to a messy record and produce a reliable verdict?" If the
answer is no, no amount of RAG plumbing saves us. So we close the end-to-end
loop first with everything else stubbed out: the "corpus" is just one ticket
passed in directly. Step 7's check_ticket tool will reuse this module and
swap the direct ticket for retrieved evidence.

Key concepts in this file (see LEARNING.md Step 4 for the full walkthrough):
1. Judging prompt design - role, rubric, evidence, output contract.
2. Structured output: forcing JSON and validating it with Pydantic.
3. The corrective-retry pattern for malformed LLM output.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from core.checklists import Control
from core.llm import ChatResult, chat

# Prompt wording lives in core/prompts.py (with the other prompts) so a
# prompt change shows up in a diff as a prompt change - see that module's
# docstring for the system/user split rationale.
from core.prompts import JUDGE_SYSTEM, build_judge_user_prompt


class JudgeError(Exception):
    """The model failed to produce a valid verdict (after a retry)."""


# ---------------------------------------------------------------------------
# The output contract
#
# Same "parse, don't validate" move as the checklist loader, but pointed at
# the LLM instead of a YAML file: the model's reply is untrusted text until
# it has passed through this schema. If the model invents a fourth verdict
# ("mostly-satisfied") or omits the rationale, validation fails loudly here -
# nothing downstream ever sees a malformed verdict.
# ---------------------------------------------------------------------------


class Verdict(BaseModel):
    """The structured judgment the model must return.

    not_applicable exists because forcing every (record, control) pair into
    satisfied/partial/gap poisons the metrics: an internal ticket with no
    customer can't "fail" the client-communication control - calling it a
    gap would be a false positive, and false positives are what kill
    auditor trust (they stop reading the report).
    """

    verdict: Literal["satisfied", "partial", "gap", "not_applicable"]

    # Why a rationale? Auditors don't accept bare verdicts, and neither
    # should we: forcing the model to argue from the rubric's specific
    # conditions is also what keeps the verdict itself honest.
    rationale: str = Field(min_length=1)

    # What the verdict is grounded ON: ticket ids, comment timestamps,
    # field names. FR-16 (citations) starts here; Step 8's guardrails will
    # verify these citations actually exist in the source.
    citations: list[str] = Field(default_factory=list)


@dataclass
class JudgeResult:
    """A validated verdict plus the token accounting for cost display."""

    verdict: Verdict
    model: str
    prompt_tokens: int
    completion_tokens: int


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _parse_verdict(text: str) -> Verdict:
    """Extract and validate the JSON object from a model reply.

    Even instruction-following models sometimes wrap JSON in ```json fences
    or add a polite sentence before it. Rather than demanding perfection,
    we slice from the first '{' to the last '}' - cheap, and robust to the
    common failure shapes. Anything that still fails json.loads or schema
    validation raises ValueError for the caller's retry logic.
    """
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in model reply")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"reply is not valid JSON: {e}") from e
    try:
        return Verdict.model_validate(data)
    except ValidationError as e:
        raise ValueError(f"JSON does not match the verdict schema: {e}") from e


# ---------------------------------------------------------------------------
# The judge
# ---------------------------------------------------------------------------


def judge_ticket(
    ticket: dict, control: Control, feedback: str | None = None
) -> JudgeResult:
    """Judge one ticket against one control. Returns a validated JudgeResult.

    Flow: build messages → call the LLM (temperature 0, inherited from
    core.llm.chat) → parse + validate. If the reply is malformed, we make
    ONE corrective retry: send the conversation back with the bad reply and
    a pointed correction. One retry fixes the occasional formatting slip;
    unlimited retries would hide a systematically broken prompt and burn
    money. Still malformed after that → JudgeError.

    `feedback` is optional and only the audit graph (CR-1) sets it: the
    verifier's concrete objections to a previous verdict, so the re-judge
    knows what to fix. Every v1 caller leaves it None, which keeps the
    prompt byte-identical to the one the eval numbers were measured on.
    """
    messages = [
        {"role": "system", "content": JUDGE_SYSTEM},
        {
            "role": "user",
            "content": build_judge_user_prompt(ticket, control, feedback=feedback),
        },
    ]

    result: ChatResult = chat(messages)
    prompt_tokens = result.prompt_tokens
    completion_tokens = result.completion_tokens

    try:
        verdict = _parse_verdict(result.content)
    except ValueError as first_error:
        # Corrective retry: include the model's own bad reply in the history
        # so it can see exactly what it did wrong, then restate the contract.
        messages.append({"role": "assistant", "content": result.content})
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Your reply could not be used: {first_error}. "
                    "Respond again with ONLY the JSON object in the required "
                    "schema - no fences, no commentary."
                ),
            }
        )
        retry = chat(messages)
        prompt_tokens += retry.prompt_tokens
        completion_tokens += retry.completion_tokens
        try:
            verdict = _parse_verdict(retry.content)
        except ValueError as second_error:
            raise JudgeError(
                f"model returned malformed verdict twice for "
                f"{control.control_id}: {second_error}"
            ) from second_error

    return JudgeResult(
        verdict=verdict,
        model=result.model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
