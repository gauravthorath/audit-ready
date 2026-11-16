"""The eval gate (CR-7): what CI refuses to let through.

The harnesses in `assistant/eval/` and `audit/eval/` measure. This module
*decides* - and the two jobs are worth keeping apart, because a number is only
a gate once something acts on it.

It asks two questions, and the second one is the one that actually caught me
out during the build:

  1. **Are the numbers good enough?** Every metric named in
     `thresholds.yaml` is checked against its floor or ceiling. Straight
     comparison; the interesting decisions are in that file's bands.

  2. **Are the numbers still about this code?** A committed `results.json` is
     evidence about the prompts, rubrics and retrieval code *as they stood when
     it ran*. Change a judge prompt and don't re-run, and the committed metrics
     become a claim about software that no longer exists - while still looking
     like a passing gate. So the gate compares commit times: if any
     judgment-affecting input was committed after the results file, or is
     dirty in the working tree, the eval on record is stale and the gate fails.

Check 2 is free and offline, which is why CI runs it on every push while the
full eval (real tokens, needs a key) runs only on the paths that can move a
number. `uv run python audit/eval/gate.py --help`.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

THRESHOLDS_FILE = Path("audit/eval/thresholds.yaml")

# Inputs that can move a metric. Anything here, changed after a results file was
# committed, makes that results file stale. Ground truth is deliberately absent:
# it is frozen, and a change to it is a change to the question, not the answer.
WATCHED_INPUTS = (
    "core/prompts.py",
    "core/judge.py",
    "core/rag.py",
    "core/retrieval.py",
    "core/guardrails.py",
    "core/tools.py",
    "assistant/prompts.py",
    "assistant/agent.py",
    "assistant/data/checklists",
    "audit/prompts.py",
    "audit/graph.py",
)


class GateError(RuntimeError):
    """The gate could not run at all - a missing file, unreadable JSON."""


@dataclass(frozen=True)
class Violation:
    """One failed check, in the words the reader needs to act on it."""

    where: str  # e.g. "audit/eval/results.json: pooled.gap_recall"
    detail: str  # e.g. "0.955 is below the floor 1.0"

    def __str__(self) -> str:
        return f"{self.where}: {self.detail}"


# ---------------------------------------------------------------------------
# Check 1: the numbers
# ---------------------------------------------------------------------------


def dig(data: Any, dotted: str) -> Any:
    """Follow a dotted path into nested dicts. Returns None if the path breaks.

    A broken path is not silently fine - `check_floors` reports it - because a
    renamed metric key is exactly how a gate quietly stops gating anything.
    """
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def check_floors(
    results: dict[str, dict[str, Any]],
    thresholds: dict[str, dict[str, dict[str, float]]],
) -> list[Violation]:
    """Compare every threshold against the loaded results.

    `results` maps a results-file path to its parsed contents; `thresholds` maps
    the same paths to `{metric: {min|max: value}}`. Pure - the caller does the
    file reading, so tests can hand it dictionaries.
    """
    violations: list[Violation] = []
    for path, checks in thresholds.items():
        data = results.get(path)
        if data is None:
            violations.append(Violation(path, "no results on record for this file"))
            continue
        for metric, bound in checks.items():
            where = f"{path}: {metric}"
            value = dig(data, metric)
            if value is None:
                violations.append(
                    Violation(where, "metric missing from results - renamed, or the run skipped it")
                )
                continue
            if not isinstance(value, (int, float)):
                violations.append(Violation(where, f"not a number: {value!r}"))
                continue
            if "min" in bound and value < bound["min"]:
                violations.append(Violation(where, f"{value} is below the floor {bound['min']}"))
            if "max" in bound and value > bound["max"]:
                violations.append(Violation(where, f"{value} is above the ceiling {bound['max']}"))
    return violations


# ---------------------------------------------------------------------------
# Check 2: whether the numbers are still about this code
# ---------------------------------------------------------------------------


def git_commit_time(path: str) -> int | None:
    """Unix time of the last commit touching `path`, or None if git has none."""
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%ct", "--", path],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise GateError(f"git is not usable here: {exc}") from exc
    return int(out) if out else None


def git_dirty(paths: tuple[str, ...]) -> list[str]:
    """Which of `paths` have uncommitted changes (staged or not)."""
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain", "--", *paths],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise GateError(f"git is not usable here: {exc}") from exc
    # Porcelain lines are "XY path"; a rename is "XY old -> new".
    return sorted({line[3:].split(" -> ")[-1].strip() for line in out.splitlines() if line.strip()})


def check_freshness(
    results_time: int | None,
    input_times: dict[str, int | None],
    dirty: list[str],
    results_path: str,
) -> list[Violation]:
    """Is the eval on record newer than every input that could have moved it?

    Pure: the caller supplies the timestamps, so a test can describe a stale
    repo without making one.
    """
    violations: list[Violation] = []
    if results_time is None:
        violations.append(
            Violation(results_path, "never committed - an uncommitted eval is not evidence")
        )
        return violations
    for path, when in sorted(input_times.items()):
        if when is not None and when > results_time:
            violations.append(
                Violation(
                    f"{results_path} vs {path}",
                    f"{path} was committed after the eval ran - re-run the eval",
                )
            )
    for path in dirty:
        violations.append(
            Violation(
                f"{results_path} vs {path}",
                f"{path} has uncommitted changes - the eval on record predates them",
            )
        )
    return violations


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def load_thresholds(path: Path = THRESHOLDS_FILE) -> dict[str, Any]:
    if not path.exists():
        raise GateError(f"no thresholds file at {path}")
    loaded = yaml.safe_load(path.read_text())
    if not isinstance(loaded, dict) or not loaded:
        raise GateError(f"{path} is empty or not a mapping")
    return loaded


def load_results(paths: list[str]) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for path in paths:
        file = Path(path)
        if not file.exists():
            continue  # check_floors reports it, with the metric names in hand
        try:
            results[path] = json.loads(file.read_text())
        except json.JSONDecodeError as exc:
            raise GateError(f"{path} is not valid JSON: {exc}") from exc
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--floors", action="store_true", help="check metrics against thresholds.yaml"
    )
    parser.add_argument(
        "--freshness",
        action="store_true",
        help="check the committed eval is newer than every judgment-affecting input",
    )
    parser.add_argument(
        "--thresholds", default=str(THRESHOLDS_FILE), help=f"default {THRESHOLDS_FILE}"
    )
    args = parser.parse_args(argv)

    # No flag means both: the useful default is the whole gate.
    run_floors = args.floors or not (args.floors or args.freshness)
    run_freshness = args.freshness or not (args.floors or args.freshness)

    try:
        thresholds = load_thresholds(Path(args.thresholds))
        violations: list[Violation] = []

        if run_floors:
            results = load_results(list(thresholds))
            violations += check_floors(results, thresholds)

        if run_freshness:
            dirty_inputs = git_dirty(WATCHED_INPUTS)
            input_times = {p: git_commit_time(p) for p in WATCHED_INPUTS}
            for results_path in thresholds:
                violations += check_freshness(
                    git_commit_time(results_path), input_times, dirty_inputs, results_path
                )
    except GateError as exc:
        print(f"gate could not run: {exc}", file=sys.stderr)
        return 2

    checked = ", ".join(
        part for part, on in (("floors", run_floors), ("freshness", run_freshness)) if on
    )
    if violations:
        print(f"EVAL GATE FAILED ({checked}) - {len(violations)} problem(s):\n")
        for violation in violations:
            print(f"  - {violation}")
        print("\nRe-run the eval (see .claude/skills/eval-run) or fix the regression.")
        return 1

    print(f"eval gate passed ({checked})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
