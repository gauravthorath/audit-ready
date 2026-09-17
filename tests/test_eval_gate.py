"""Offline tests for the eval gate (CR-7).

A gate is worth only what its failure modes are worth, so what is pinned here
is mostly the ways it could pass when it shouldn't: a metric that quietly
vanished from the results, an eval that was never committed, an input edited
after the numbers were recorded. The floors themselves live in
`thresholds.yaml` and are checked against the committed results by CI, not
here — a test that re-asserts the numbers in the file it reads proves nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from audit.eval import gate


# ---------------------------------------------------------------------------
# dig: the path walker
# ---------------------------------------------------------------------------


def test_dig_follows_a_dotted_path():
    data = {"packs": {"iso9001": {"metrics": {"gap_recall": 1.0}}}}
    assert gate.dig(data, "packs.iso9001.metrics.gap_recall") == 1.0


@pytest.mark.parametrize(
    "path",
    ["packs.missing.metrics", "packs.iso9001.metrics.nope", "packs.iso9001.metrics.gap_recall.deep"],
)
def test_a_broken_path_is_none_not_an_exception(path):
    data = {"packs": {"iso9001": {"metrics": {"gap_recall": 1.0}}}}
    assert gate.dig(data, path) is None


# ---------------------------------------------------------------------------
# check_floors
# ---------------------------------------------------------------------------


def test_metrics_inside_their_bounds_pass():
    results = {"r.json": {"pooled": {"gap_recall": 1.0, "false_positive_rate": 0.02}}}
    thresholds = {
        "r.json": {"pooled.gap_recall": {"min": 1.0}, "pooled.false_positive_rate": {"max": 0.06}}
    }
    assert gate.check_floors(results, thresholds) == []


def test_a_metric_below_its_floor_fails_with_both_numbers():
    results = {"r.json": {"pooled": {"gap_recall": 0.95}}}
    thresholds = {"r.json": {"pooled.gap_recall": {"min": 1.0}}}

    violations = gate.check_floors(results, thresholds)

    assert len(violations) == 1
    assert "0.95" in violations[0].detail and "1.0" in violations[0].detail


def test_a_metric_above_its_ceiling_fails():
    results = {"r.json": {"pooled": {"false_positive_rate": 0.2}}}
    thresholds = {"r.json": {"pooled.false_positive_rate": {"max": 0.06}}}

    violations = gate.check_floors(results, thresholds)

    assert len(violations) == 1
    assert "above the ceiling" in violations[0].detail


def test_a_renamed_metric_fails_rather_than_silently_passing():
    # The failure this guards: someone renames `gap_recall`, every threshold
    # for it stops matching anything, and the gate goes green having checked
    # nothing at all.
    results = {"r.json": {"pooled": {"gap_recall_v2": 1.0}}}
    thresholds = {"r.json": {"pooled.gap_recall": {"min": 1.0}}}

    violations = gate.check_floors(results, thresholds)

    assert len(violations) == 1
    assert "missing" in violations[0].detail


def test_a_missing_results_file_fails():
    thresholds = {"never-ran.json": {"pooled.gap_recall": {"min": 1.0}}}

    violations = gate.check_floors({}, thresholds)

    assert len(violations) == 1
    assert "no results on record" in violations[0].detail


def test_a_non_numeric_metric_fails():
    results = {"r.json": {"pooled": {"gap_recall": None}}}
    thresholds = {"r.json": {"pooled.gap_recall": {"min": 1.0}}}

    # None is what a dead grader leaves behind (assistant Part B), and it must not
    # sail through a `<` comparison or crash the gate.
    violations = gate.check_floors(results, thresholds)

    assert len(violations) == 1
    assert "missing" in violations[0].detail or "not a number" in violations[0].detail


def test_every_violation_is_reported_not_just_the_first():
    results = {"r.json": {"pooled": {"gap_recall": 0.9, "false_positive_rate": 0.5}}}
    thresholds = {
        "r.json": {"pooled.gap_recall": {"min": 1.0}, "pooled.false_positive_rate": {"max": 0.06}}
    }

    assert len(gate.check_floors(results, thresholds)) == 2


# ---------------------------------------------------------------------------
# check_freshness
# ---------------------------------------------------------------------------


def test_an_eval_newer_than_every_input_is_fresh():
    violations = gate.check_freshness(
        results_time=200,
        input_times={"core/prompts.py": 100, "audit/graph.py": 150},
        dirty=[],
        results_path="r.json",
    )
    assert violations == []


def test_an_input_committed_after_the_eval_makes_it_stale():
    violations = gate.check_freshness(
        results_time=100,
        input_times={"core/prompts.py": 200},
        dirty=[],
        results_path="r.json",
    )
    assert len(violations) == 1
    assert "core/prompts.py" in str(violations[0])


def test_a_dirty_input_makes_the_eval_stale_too():
    # Committed order says fresh; the working tree says the prompt changed
    # since. Uncommitted is the easy way to fool a commit-time check.
    violations = gate.check_freshness(
        results_time=200,
        input_times={"core/prompts.py": 100},
        dirty=["core/prompts.py"],
        results_path="r.json",
    )
    assert len(violations) == 1
    assert "uncommitted" in violations[0].detail


def test_an_uncommitted_eval_is_not_evidence():
    violations = gate.check_freshness(
        results_time=None,
        input_times={"core/prompts.py": 100},
        dirty=[],
        results_path="r.json",
    )
    assert len(violations) == 1
    assert "never committed" in violations[0].detail


def test_an_input_git_has_never_seen_is_not_a_staleness_claim():
    # A brand-new watched file that isn't committed yet has no commit time; it
    # shows up in `dirty` instead, so treating None as "newer" would double-
    # report it.
    violations = gate.check_freshness(
        results_time=100,
        input_times={"audit/prompts.py": None},
        dirty=[],
        results_path="r.json",
    )
    assert violations == []


# ---------------------------------------------------------------------------
# The real thresholds file, and the CLI
# ---------------------------------------------------------------------------


def test_the_committed_thresholds_file_is_loadable_and_well_formed():
    thresholds = gate.load_thresholds()

    assert thresholds, "thresholds.yaml is empty"
    for path, checks in thresholds.items():
        assert Path(path).exists(), f"{path} is gated but does not exist"
        for metric, bound in checks.items():
            assert set(bound) <= {"min", "max"}, f"{metric}: unknown bound {set(bound)}"
            assert bound, f"{metric}: no bound at all"


def test_gap_recall_is_gated_at_one_everywhere_it_is_measured():
    # The product-level promise: a planted deficiency must not survive. If a
    # future edit softens this floor, it should have to argue with a test.
    thresholds = gate.load_thresholds()
    floors = [
        bound.get("min")
        for checks in thresholds.values()
        for metric, bound in checks.items()
        if metric.endswith("gap_recall")
    ]
    assert floors, "no gap_recall floor is set at all"
    assert all(floor == 1.0 for floor in floors), f"gap_recall floors softened: {floors}"


def test_cli_reports_a_failure_as_a_nonzero_exit(tmp_path, monkeypatch, capsys):
    results = tmp_path / "results.json"
    results.write_text('{"pooled": {"gap_recall": 0.5}}')
    thresholds = tmp_path / "thresholds.yaml"
    thresholds.write_text(yaml.safe_dump({str(results): {"pooled.gap_recall": {"min": 1.0}}}))

    code = gate.main(["--floors", "--thresholds", str(thresholds)])

    assert code == 1
    assert "EVAL GATE FAILED" in capsys.readouterr().out


def test_cli_passes_clean_results(tmp_path, capsys):
    results = tmp_path / "results.json"
    results.write_text('{"pooled": {"gap_recall": 1.0}}')
    thresholds = tmp_path / "thresholds.yaml"
    thresholds.write_text(yaml.safe_dump({str(results): {"pooled.gap_recall": {"min": 1.0}}}))

    code = gate.main(["--floors", "--thresholds", str(thresholds)])

    assert code == 0
    assert "passed" in capsys.readouterr().out


def test_an_unrunnable_gate_exits_two_not_one(tmp_path, capsys):
    # Exit 2 means "could not check", which a CI job should treat differently
    # from exit 1, "checked and it failed".
    code = gate.main(["--floors", "--thresholds", str(tmp_path / "nope.yaml")])

    assert code == 2
    assert "could not run" in capsys.readouterr().err
