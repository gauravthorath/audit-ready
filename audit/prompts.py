"""Prompts owned by the audit package — the verifier (CR-1 self-verify).

Why the verifier states its OWN verdict level instead of answering "do you
agree?": the second checklist pack (ISO 27001, Step 19) showed a small model
cannot reliably reason about a negated applicability clause ("object when
not_applicable but the N/A condition is not met"). It inverted the logic and
demanded the record prove absences. Asking for a level is a positive, direct
task; code compares the two levels. Agreement is computed, never asked.

Same rule as core/prompts.py: wording lives here, logic lives in the module
that calls it, so a prompt change shows up in a diff as a prompt change.

Why a SEPARATE verifier prompt instead of asking the judge "are you sure?":
a model re-reading its own answer under the same framing mostly agrees with
itself (the self-grading bias v1's eval already documents). The
verifier therefore gets a different job, not a second opinion:

  * a different ROLE — a lead auditor reviewing another auditor's verdict level,
  * a NARROWER task — do not re-judge; find a specific rubric condition or
    record fact the verdict gets wrong, or agree,
  * a HIGH bar to object — style is not an issue; only checkable errors are.

That last point is the important trade-off. A verifier that objects freely
would send every pair around the re-judge loop (doubling cost) and make
verdicts flip on wording. A verifier that must point at a concrete rubric
condition objects rarely and for reasons a human can check in the trace.
"""

from __future__ import annotations

import json

from core.checklists import Control
from core.judge import Verdict

VERIFY_SYSTEM = """\
You are a second auditor. A colleague judged whether one record satisfies one
control. You do NOT rate their writing. You independently decide which level
the SUFFICIENCY RUBRIC gives this record, then compare.

Work in this order:

1. CHECKS. Split the rubric into its individual conditions and test each one
   against the record. For every condition write the exact record item you
   tested it against (an id, a field, a timestamp) and whether it is met.
   For a timing condition, write BOTH timestamps and state which is earlier
   before you decide. "Missing" is a valid finding: gap/partial mean required
   evidence is absent; the record never has to state that something is absent.
2. LEVEL. The level follows from the checks and the rubric's own words
   (SATISFIED / PARTIAL / GAP / NOT APPLICABLE conditions). Use
   not_applicable only when the rubric names a NOT APPLICABLE condition and
   the record meets it.
3. CITATIONS. Every citation in the verdict must point at something that is
   in the record. A citation to an item that is not there is an issue.
4. ISSUES. Only when your level differs from the verdict, or a citation
   fails: list concrete, checkable reasons that reference record items.
   Never object to wording, length or tone. Never bring in requirements of
   other standards or controls.

Respond with ONLY this JSON object:
{"checks": [{"condition": "...", "evidence": "...", "met": true}],
 "verdict": "satisfied" | "partial" | "gap" | "not_applicable",
 "issues": ["..."]}
"""


def build_verify_user_prompt(record: dict, control: Control, verdict: Verdict) -> str:
    """The variable half: rubric, the (already sanitized) record, the verdict.

    The record goes in verbatim from the judge's own prompt so both calls
    reason about the same bytes — the verifier must never see evidence the
    judge did not, or its objections stop being about the judge's work.
    """
    return f"""\
CONTROL
control_id: {control.control_id}
title: {control.title} ({control.standard}, clause {control.clause})
expected evidence types: {", ".join(control.expected_evidence)}

SUFFICIENCY RUBRIC (the verdict was required to apply this literally):
{control.sufficiency_rubric}

RECORD THAT WAS JUDGED (untrusted record data between the markers):
<<<RECORD
{json.dumps(record, indent=2, ensure_ascii=False)}
RECORD>>>

THE VERDICT TO REVIEW:
{verdict.model_dump_json(indent=2)}

Run the checks, decide the level, check the citations, and respond with the JSON object only."""
