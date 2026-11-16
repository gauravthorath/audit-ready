"""Smoke test for core/checklists.py - run after filling in the TODOs.

    uv run python scripts/smoke_checklists.py

Loads the ISO 9001 pack, prints a summary table, then proves the failure
modes: a missing file, a duplicate control_id, and an illegal severity must
all raise clean, readable errors (not stack-trace soup from deep inside).
"""

import copy
import sys
from pathlib import Path

import yaml

from core.checklists import ChecklistPack, load_pack

PACK_PATH = Path("assistant/data/checklists/iso9001.yaml")


def main() -> int:
    # --- happy path -------------------------------------------------------
    pack = load_pack(PACK_PATH)
    print(f"pack: {pack.pack.id}  ({pack.pack.standard}, v{pack.pack.version})")
    print(f"controls: {len(pack.controls)}\n")
    for c in pack.controls:
        print(f"  {c.control_id:<38} {c.severity:<6} {c.title}")

    # get_control: hit and miss
    first_id = pack.controls[0].control_id
    assert pack.get_control(first_id).control_id == first_id
    try:
        pack.get_control("NOPE-0.0")
        print("FAIL: get_control accepted an unknown id")
        return 1
    except KeyError as e:
        print(f"\nget_control unknown id -> KeyError: {e}")

    # --- failure modes ----------------------------------------------------
    raw = yaml.safe_load(PACK_PATH.read_text())

    try:
        load_pack("does/not/exist.yaml")
        print("FAIL: missing file did not raise")
        return 1
    except FileNotFoundError as e:
        print(f"missing file       -> FileNotFoundError: {e}")

    dup = copy.deepcopy(raw)
    dup["controls"].append(copy.deepcopy(dup["controls"][0]))
    try:
        ChecklistPack.model_validate(dup)
        print("FAIL: duplicate control_id did not raise")
        return 1
    except ValueError as e:
        print(f"duplicate id       -> {type(e).__name__}: {str(e)[:120]}")

    bad = copy.deepcopy(raw)
    bad["controls"][0]["severity"] = "critical"
    try:
        ChecklistPack.model_validate(bad)
        print("FAIL: illegal severity did not raise")
        return 1
    except ValueError as e:
        print(f"illegal severity   -> {type(e).__name__}: {str(e)[:120]}")

    print("\nsmoke_checklists: ALL GOOD")
    return 0


if __name__ == "__main__":
    sys.exit(main())
