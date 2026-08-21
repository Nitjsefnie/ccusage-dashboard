"""Tests for the test-speed comparator.

The comparator is a gate, so its own failure modes matter more than most
code here: a false red teaches people to ignore it, and a false green
means the gate is decorative. The cases below are exactly the ways it
could be wrong — added tests, removed tests, flaky rounds, non-passing
testcases — plus the arithmetic.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load():
    """Import scripts/ci/compare_durations.py by path.

    scripts/ci is not a package and deliberately has no __init__.py — it
    holds standalone CI entry points, not an importable library.
    """
    path = REPO_ROOT / "scripts" / "ci" / "compare_durations.py"
    spec = importlib.util.spec_from_file_location("compare_durations", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["compare_durations"] = module
    spec.loader.exec_module(module)
    return module


cd = _load()


def write_junit(path: Path, cases: dict, not_passed: dict | None = None) -> Path:
    """Minimal but real pytest --junitxml output."""
    not_passed = not_passed or {}
    parts = ['<?xml version="1.0" encoding="utf-8"?>', "<testsuites><testsuite>"]
    for nid, seconds in cases.items():
        classname, _, name = nid.partition("::")
        child = not_passed.get(nid)
        body = f"<{child}/>" if child else ""
        parts.append(
            f'<testcase classname="{classname}" name="{name}" '
            f'time="{seconds}">{body}</testcase>'
        )
    parts.append("</testsuite></testsuites>")
    path.write_text("".join(parts), encoding="utf-8")
    return path


def test_node_id_joins_class_and_name(tmp_path):
    report = write_junit(tmp_path / "r.xml", {"tests.test_a::test_one": 1.0})
    assert set(cd.parse_junit(report)) == {"tests.test_a::test_one"}


def test_non_passing_cases_are_excluded(tmp_path):
    report = write_junit(
        tmp_path / "r.xml",
        {"m::ok": 1.0, "m::bad": 9.0, "m::skip": 9.0},
        not_passed={"m::bad": "failure", "m::skip": "skipped"},
    )
    # A failure can be fast or slow for reasons unrelated to speed; a skip
    # is not a measurement at all.
    assert set(cd.parse_junit(report)) == {"m::ok"}


def test_rounds_fold_to_the_minimum(tmp_path):
    a = write_junit(tmp_path / "a.xml", {"m::t": 1.00})
    b = write_junit(tmp_path / "b.xml", {"m::t": 3.00})
    # The minimum estimates the floor; the mean would track the noise.
    assert cd.fold_rounds([str(a), str(b)]) == {"m::t": 1.00}


def test_a_test_missing_from_one_round_is_dropped(tmp_path):
    a = write_junit(tmp_path / "a.xml", {"m::t": 1.0, "m::flaky": 1.0})
    b = write_junit(tmp_path / "b.xml", {"m::t": 1.0})
    assert set(cd.fold_rounds([str(a), str(b)])) == {"m::t"}


def test_added_tests_cannot_trip_the_gate():
    """The whole reason this compares an intersection."""
    base = {"m::a": 1.0, "m::b": 1.0}
    head = {"m::a": 1.0, "m::b": 1.0, "m::brand_new": 50.0}

    result = cd.compare(base, head)

    assert result["ratio"] == pytest.approx(1.0)
    assert result["head_only"] == ["m::brand_new"]


def test_removed_tests_cannot_hide_a_regression():
    base = {"m::a": 1.0, "m::slow_one_being_deleted": 100.0}
    head = {"m::a": 2.0}

    result = cd.compare(base, head)

    # Total wall time fell from 101s to 2s; the shared test still doubled.
    assert result["ratio"] == pytest.approx(2.0)
    assert result["base_only"] == ["m::slow_one_being_deleted"]


def test_ratio_is_over_the_shared_population_only():
    base = {"m::a": 2.0, "m::b": 8.0}
    head = {"m::a": 3.0, "m::b": 9.0}

    assert cd.compare(base, head)["ratio"] == pytest.approx(12.0 / 10.0)


def test_no_shared_tests_is_an_error_not_a_pass():
    with pytest.raises(cd.ComparisonError, match="share no passing test"):
        cd.compare({"m::a": 1.0}, {"m::z": 1.0})


def test_zero_baseline_is_an_error_not_a_division_by_zero():
    with pytest.raises(cd.ComparisonError, match="baseline total is zero"):
        cd.compare({"m::a": 0.0}, {"m::a": 1.0})


def test_empty_report_is_an_error(tmp_path):
    report = write_junit(tmp_path / "r.xml", {})
    with pytest.raises(cd.ComparisonError, match="no passing testcases"):
        cd.parse_junit(report)


def test_unparseable_report_is_an_error(tmp_path):
    bad = tmp_path / "bad.xml"
    bad.write_text("<testsuites", encoding="utf-8")
    with pytest.raises(cd.ComparisonError, match="not parseable"):
        cd.parse_junit(bad)


def test_sub_50ms_tests_stay_out_of_the_table_but_count_in_the_total():
    base = {"m::tiny": 0.002, "m::real": 1.0}
    head = {"m::tiny": 0.008, "m::real": 1.0}

    result = cd.compare(base, head)

    # 4x on a 2 ms test is noise and would bury genuine entries.
    assert [row[2] for row in result["per_test"]] == ["m::real"]
    # ...but it is still in the totals, where it belongs.
    assert result["head_total"] == pytest.approx(1.008)


def test_render_marks_a_regression_and_names_the_budget():
    result = cd.compare({"m::a": 1.0}, {"m::a": 2.0})

    text = cd.render(result, 0.30, "v1.2.3")

    assert "REGRESSION" in text
    assert "+100.0%" in text
    assert "v1.2.3" in text


def test_render_marks_an_acceptable_change():
    result = cd.compare({"m::a": 1.0}, {"m::a": 1.10})

    text = cd.render(result, 0.30, "v1.2.3")

    assert "within budget" in text
    assert "REGRESSION" not in text


def test_render_disclaims_the_per_test_table():
    """The table misleads without this, and that is measured, not assumed.

    Two runs of identical code on one machine moved individual tests by up
    to +370% while the total moved 1.7%. A reader who takes a row as a
    regression is reading noise.
    """
    result = cd.compare({"m::a": 1.0, "m::b": 1.0}, {"m::a": 1.0, "m::b": 1.4})

    text = cd.render(result, 0.30, "v1.0.0")

    assert "not a regression" in text.lower()
    assert "<details>" in text and "</details>" in text


def test_render_survives_a_result_with_no_movers():
    result = cd.compare({"m::a": 1.0}, {"m::a": 1.0})

    text = cd.render(result, 0.30, "v0.1.0")

    assert "| Test |" not in text
    assert "within budget" in text


def test_main_exits_1_over_budget_and_0_under(tmp_path, capsys):
    base = write_junit(tmp_path / "base.xml", {"m::a": 1.0})
    slow = write_junit(tmp_path / "slow.xml", {"m::a": 2.0})
    fine = write_junit(tmp_path / "fine.xml", {"m::a": 1.05})
    summary = tmp_path / "summary.md"

    argv = sys.argv
    try:
        sys.argv = ["compare_durations.py", "--base", str(base),
                    "--head", str(slow), "--max-regression", "0.30",
                    "--summary-file", str(summary)]
        assert cd.main() == 1

        sys.argv = ["compare_durations.py", "--base", str(base),
                    "--head", str(fine), "--max-regression", "0.30"]
        assert cd.main() == 0
    finally:
        sys.argv = argv

    capsys.readouterr()
    # The report reaches the summary file even on the failing run — a red
    # gate with no detail is one nobody can act on.
    assert "REGRESSION" in summary.read_text(encoding="utf-8")


def test_main_exits_2_when_it_cannot_compare(tmp_path):
    base = write_junit(tmp_path / "base.xml", {"m::a": 1.0})
    other = write_junit(tmp_path / "other.xml", {"m::z": 1.0})

    argv = sys.argv
    try:
        sys.argv = ["compare_durations.py", "--base", str(base),
                    "--head", str(other)]
        # 2, not 1: "could not compare" is a different thing from "slower",
        # and a workflow that conflates them reports a restructure as a
        # performance regression.
        assert cd.main() == 2
    finally:
        sys.argv = argv
