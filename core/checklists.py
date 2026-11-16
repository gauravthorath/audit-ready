"""Checklist pack loader (NFR-4).

Loads a YAML checklist pack (e.g. assistant/data/checklists/iso9001.yaml) into
validated Pydantic models. Everything downstream - retrieval, the check_ticket
tool, the coverage report - consumes these models, never raw YAML dicts.

Why this module exists:
- Packs are config-as-data: a new standard (ISO 27001, CMMI) is a new YAML
  file, not new code (CR-5 depends on this).
- Validation at the boundary: a malformed pack fails loudly HERE with a
  clear message, not deep inside an LLM prompt three tools later.

See LEARNING.md "Step 3" for the full walkthrough of the concepts used here.
"""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

# ---------------------------------------------------------------------------
# Models - mirror the schema documented at the top of iso9001.yaml
#
# A Pydantic BaseModel turns type annotations into a validating constructor:
# you declare WHAT a valid object looks like, and Pydantic generates the
# __init__, the type checks, and precise error messages. Note there is no
# hand-written parsing code anywhere in this file - the annotations ARE the
# schema.
# ---------------------------------------------------------------------------


class Control(BaseModel):
    """One auditable control from a checklist pack."""

    control_id: str
    standard: str
    clause: str
    title: str
    description: str

    # Field(...) attaches constraints beyond the plain type. min_length=1 on
    # a list means "at least one item": a control with no expected evidence
    # types would make the check_ticket tool meaningless, so we reject it at
    # load time instead of discovering it mid-audit.
    expected_evidence: list[str] = Field(min_length=1)

    sufficiency_rubric: str
    citation_required: bool

    # Literal restricts the value to an exact set. A typo like "critical"
    # or "Major" fails validation with a message naming the legal values -
    # instead of silently flowing into the coverage report's severity
    # weighting and skewing the numbers.
    severity: Literal["major", "minor"]


class PackMeta(BaseModel):
    """The `pack:` header block of the YAML file - identity metadata."""

    id: str
    standard: str
    version: str
    description: str


class ChecklistPack(BaseModel):
    """The whole file: pack metadata + the list of controls.

    Nesting models composes validation: validating a ChecklistPack
    recursively validates the PackMeta and every Control inside it, and
    error messages carry the full path (e.g. `controls.3.severity`).
    """

    pack: PackMeta
    controls: list[Control] = Field(min_length=1)

    # Field-level types can't express rules that span MULTIPLE items -
    # "no two controls share an id" needs to see the whole list at once.
    # That's what a model_validator is for. mode="after" runs once the
    # individual fields have already been validated, so here `self` is a
    # fully-typed ChecklistPack and we can safely read self.controls.
    @model_validator(mode="after")
    def _no_duplicate_control_ids(self) -> "ChecklistPack":
        seen: set[str] = set()
        duplicates: set[str] = set()
        for control in self.controls:
            if control.control_id in seen:
                duplicates.add(control.control_id)
            seen.add(control.control_id)
        if duplicates:
            # Raising ValueError inside a validator is the Pydantic
            # convention - it gets wrapped into a ValidationError that
            # points at this model, keeping all failures in one error type.
            raise ValueError(
                f"duplicate control_id(s): {sorted(duplicates)} - "
                "every control needs a unique, stable id"
            )
        return self

    def get_control(self, control_id: str) -> Control:
        """Return the control with this id.

        Raises KeyError listing the valid ids - an unknown id is almost
        always a typo, and showing the legal options turns a dead-end
        error into a self-correcting one.
        """
        for control in self.controls:
            if control.control_id == control_id:
                return control
        valid = ", ".join(c.control_id for c in self.controls)
        raise KeyError(f"unknown control_id {control_id!r}; valid ids: {valid}")


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_pack(path: str | Path) -> ChecklistPack:
    """Load and validate a checklist pack from a YAML file.

    The failure modes are handled in the order they can occur:
    file missing -> not YAML/not a mapping -> schema violation. Each is
    re-raised with the file path in the message, because "which file?" is
    the first question anyone debugging a config error asks.
    """
    path = Path(path)
    if not path.is_file():
        # resolve() shows the absolute path we actually looked at -
        # relative paths in error messages hide cwd mistakes.
        raise FileNotFoundError(f"checklist pack not found: {path.resolve()}")

    # safe_load, never yaml.load: full load() can instantiate arbitrary
    # Python objects from tags in the file, which is code execution.
    # Checklist packs are user-supplied input (users bring their own
    # licensed criteria), so they must be treated as untrusted.
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    # An empty file parses to None; a file containing just a list or a
    # string parses fine too. Catch that before Pydantic so the message
    # says what's wrong with the FILE, not a confusing type error.
    if not isinstance(raw, dict):
        raise ValueError(
            f"{path}: a checklist pack must be a YAML mapping with "
            f"'pack' and 'controls' keys, got {type(raw).__name__}"
        )

    try:
        return ChecklistPack.model_validate(raw)
    except ValidationError as e:
        # Re-raise as plain ValueError so callers don't need to import
        # pydantic to catch it, prefixed with the file path. `from e`
        # chains the tracebacks - the original pydantic report (which
        # pinpoints the exact field) stays visible below ours.
        raise ValueError(f"{path}: invalid checklist pack\n{e}") from e
