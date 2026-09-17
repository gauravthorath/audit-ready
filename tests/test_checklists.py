"""Unit tests for core/checklists.py — config loading + validation (NFR-4).

The loader's job is "validation at the boundary": a malformed pack must fail
loudly at load time with a message naming the file, never surface later as a
confusing error mid-audit. So most tests here are about REJECTION quality.

Testing concepts used:
- tmp_path: a pytest built-in fixture — a fresh temporary directory per
  test, cleaned up automatically. We write throwaway YAML files there
  instead of polluting the repo with fixture files.
- Testing against the real shipped config (iso9001.yaml) at the end: cheap
  insurance that the schema in code and the pack in data never drift apart.
"""

from pathlib import Path

import pytest
import yaml

from core.checklists import load_pack

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_PACK = REPO_ROOT / "assistant" / "data" / "checklists" / "iso9001.yaml"


def minimal_pack_dict() -> dict:
    """The smallest valid pack — each test mutates a copy to break ONE thing,
    so a failure points at exactly the rule under test."""
    return {
        "pack": {
            "id": "test-pack",
            "standard": "TEST 0001",
            "version": "1.0",
            "description": "fixture pack for unit tests",
        },
        "controls": [
            {
                "control_id": "T-1",
                "standard": "TEST 0001",
                "clause": "1.1",
                "title": "Test control",
                "description": "A control used only in tests.",
                "expected_evidence": ["signoff"],
                "sufficiency_rubric": "satisfied if signoff present",
                "citation_required": True,
                "severity": "major",
            }
        ],
    }


def write_pack(tmp_path: Path, data) -> Path:
    path = tmp_path / "pack.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_valid_pack_loads(tmp_path):
    pack = load_pack(write_pack(tmp_path, minimal_pack_dict()))
    assert pack.pack.id == "test-pack"
    assert len(pack.controls) == 1
    assert pack.controls[0].control_id == "T-1"


def test_get_control_returns_the_control(tmp_path):
    pack = load_pack(write_pack(tmp_path, minimal_pack_dict()))
    assert pack.get_control("T-1").title == "Test control"


def test_get_control_unknown_id_raises_with_valid_ids_listed(tmp_path):
    pack = load_pack(write_pack(tmp_path, minimal_pack_dict()))
    with pytest.raises(KeyError) as exc_info:
        pack.get_control("T-999")
    # The error must be self-correcting: it names the legal ids.
    assert "T-1" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Rejection paths — each breaks exactly one rule
# ---------------------------------------------------------------------------


def test_missing_file_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_pack(tmp_path / "does-not-exist.yaml")


def test_non_mapping_yaml_rejected_with_file_in_message(tmp_path):
    # A YAML file containing a bare list parses fine — but it isn't a pack.
    path = write_pack(tmp_path, ["just", "a", "list"])
    with pytest.raises(ValueError) as exc_info:
        load_pack(path)
    assert "pack.yaml" in str(exc_info.value)


def test_duplicate_control_ids_rejected(tmp_path):
    data = minimal_pack_dict()
    data["controls"].append(dict(data["controls"][0]))  # same control twice
    with pytest.raises(ValueError, match="duplicate"):
        load_pack(write_pack(tmp_path, data))


def test_invalid_severity_rejected(tmp_path):
    # Literal["major","minor"] — a typo must fail at load, not skew the
    # coverage report's severity weighting silently.
    data = minimal_pack_dict()
    data["controls"][0]["severity"] = "critical"
    with pytest.raises(ValueError):
        load_pack(write_pack(tmp_path, data))


def test_empty_expected_evidence_rejected(tmp_path):
    # min_length=1: a control with no expected evidence types would make
    # check_ticket meaningless.
    data = minimal_pack_dict()
    data["controls"][0]["expected_evidence"] = []
    with pytest.raises(ValueError):
        load_pack(write_pack(tmp_path, data))


def test_pack_with_no_controls_rejected(tmp_path):
    data = minimal_pack_dict()
    data["controls"] = []
    with pytest.raises(ValueError):
        load_pack(write_pack(tmp_path, data))


# ---------------------------------------------------------------------------
# Schema ↔ shipped-data drift guard
# ---------------------------------------------------------------------------


def test_shipped_iso9001_pack_is_valid():
    """If someone edits iso9001.yaml by hand and breaks the schema, this is
    the test that catches it before the app does."""
    pack = load_pack(REAL_PACK)
    assert pack.pack.standard.startswith("ISO 9001")
    assert len(pack.controls) == 9
