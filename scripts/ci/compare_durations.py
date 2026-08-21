#!/usr/bin/env python3
"""Compare per-test durations between two builds of the suite.

Answers one question: did the tests that exist in BOTH builds get slower?

WHY NOT TOTAL WALL TIME. The obvious metric — how long did the suite take
— punishes the wrong thing. Add twenty tests and the total climbs, the
gate goes red, and nothing regressed. Delete a slow test and the total
drops, hiding a genuine regression somewhere else. So this intersects on
test node id and compares only the tests present, and passing, in both.
Adding or removing tests cannot move the number.

WHY MINIMUM ACROSS ROUNDS. A CI runner's speed varies by a factor of two
between jobs, and a test's duration is a floor plus noise: contention,
page-cache state, a neighbouring container. The minimum over repeated
rounds estimates that floor. The mean does not — it tracks the noise.

WHY THIS IS COMPARABLE AT ALL. Both builds run back to back on the SAME
runner, in the same job. That is what makes a percentage meaningful here;
comparing a duration from one runner against a duration recorded on
another would measure the runners.

Input is pytest's own --junitxml, which carries an exact per-testcase
time and needs no plugin.

    compare_durations.py --base base1.xml base2.xml \\
                         --head head1.xml head2.xml \\
                         --max-regression 0.30
"""
from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


# A testcase carrying any of these children did not pass, and its duration
# is not comparable: a failure can be fast (an early assert) or slow (a
# timeout), and either way it is measuring the wrong thing.
NOT_PASSED = ("failure", "error", "skipped")


class ComparisonError(RuntimeError):
    """The comparison could not be made at all."""


def node_id(case: ET.Element) -> str:
    """`tests.test_api::test_thing`, stable across runs and machines."""
    classname = case.get("classname", "")
    name = case.get("name", "")
    return f"{classname}::{name}" if classname else name


def parse_junit(path: Path) -> dict:
    """{node_id: seconds} for the passing testcases in one report."""
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ComparisonError(f"{path} is not parseable JUnit XML: {exc}") from exc

    times = {}
    for case in root.iter("testcase"):
        if any(case.find(tag) is not None for tag in NOT_PASSED):
            continue
        raw = case.get("time")
        if raw is None:
            continue
        try:
            times[node_id(case)] = float(raw)
        except ValueError:
            continue
    if not times:
        raise ComparisonError(f"{path} contains no passing testcases")
    return times


def fold_rounds(paths: list) -> dict:
    """Per test, the MINIMUM duration across rounds.

    A test must have passed in EVERY round to be included. One that passed
    twice and was skipped once is not the same test in all three, and
    letting it through would compare two different populations.
    """
    if not paths:
        raise ComparisonError("no JUnit reports given")
    rounds = [parse_junit(Path(p)) for p in paths]
    common = set(rounds[0])
    for other in rounds[1:]:
        common &= set(other)
    if not common:
        raise ComparisonError(
            "no test passed in every round — nothing comparable"
        )
    return {nid: min(r[nid] for r in rounds) for nid in common}


def compare(base: dict, head: dict) -> dict:
    shared = sorted(set(base) & set(head))
    if not shared:
        raise ComparisonError(
            "the two builds share no passing test — the suite was renamed "
            "or restructured wholesale, so there is nothing to compare"
        )

    base_total = sum(base[n] for n in shared)
    head_total = sum(head[n] for n in shared)
    if base_total <= 0:
        raise ComparisonError(
            "baseline total is zero — the reports carry no usable timings"
        )

    per_test = []
    for nid in shared:
        was, now = base[nid], head[nid]
        # Tests in the millisecond range are dominated by fixture and
        # collection overhead; a 300% "regression" on a 2 ms test is
        # noise and would bury the real entries in the table. They still
        # count in the totals, which is where they belong.
        if was >= 0.05:
            per_test.append((now - was, (now / was) - 1.0, nid, was, now))
    per_test.sort(reverse=True)

    return {
        "shared": len(shared),
        "base_only": sorted(set(base) - set(head)),
        "head_only": sorted(set(head) - set(base)),
        "base_total": base_total,
        "head_total": head_total,
        "ratio": head_total / base_total,
        "per_test": per_test,
    }


def render(result: dict, threshold: float, base_label: str) -> str:
    delta = result["ratio"] - 1.0
    verdict = "🔴 REGRESSION" if delta > threshold else "🟢 within budget"
    lines = [
        "### Test speed vs "
        f"`{base_label}`",
        "",
        f"**{verdict}** — {delta:+.1%} "
        f"(budget {threshold:+.0%})",
        "",
        f"- Compared on **{result['shared']}** tests passing in both builds",
        f"- Baseline `{base_label}`: **{result['base_total']:.2f}s**",
        f"- This commit: **{result['head_total']:.2f}s**",
    ]
    if result["head_only"]:
        lines.append(
            f"- {len(result['head_only'])} test(s) new since `{base_label}`, "
            "excluded from the comparison"
        )
    if result["base_only"]:
        lines.append(
            f"- {len(result['base_only'])} test(s) removed since "
            f"`{base_label}`, excluded from the comparison"
        )

    movers = [row for row in result["per_test"] if abs(row[1]) >= 0.10][:15]
    if movers:
        lines += [
            "",
            "<details><summary>Largest per-test movements "
            "(individually noisy — read the total, not these)</summary>",
            "",
            "| Test | Was | Now | Change |",
            "| --- | ---: | ---: | ---: |",
        ]
        for _abs_delta, rel, nid, was, now in movers:
            lines.append(
                f"| `{nid}` | {was:.3f}s | {now:.3f}s | {rel:+.0%} |"
            )
        lines += [
            "",
            "_**A row here is not a regression.** Two runs of identical "
            "code on one machine routinely differ by 100% or more on a "
            "single test, while the total moves by under 2% — that is why "
            "the gate is the total across all shared tests and not any "
            "individual row. These are ordered by absolute seconds gained "
            "and are useful only as a starting point once the TOTAL has "
            "already gone red._",
            "",
            "_Tests under 50 ms are omitted — at that scale the number is "
            "fixture overhead, not the test. They still count in the "
            "totals above._",
            "",
            "</details>",
        ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--base", nargs="+", required=True,
                        metavar="XML", help="baseline JUnit report(s)")
    parser.add_argument("--head", nargs="+", required=True,
                        metavar="XML", help="this commit's JUnit report(s)")
    parser.add_argument("--max-regression", type=float, default=0.30,
                        help="fail above this fractional slowdown "
                             "(default: 0.30 = 30%%)")
    parser.add_argument("--base-label", default="baseline",
                        help="name of the baseline, for the report")
    parser.add_argument("--summary-file", default=None,
                        help="append the markdown report here as well as "
                             "printing it (e.g. $GITHUB_STEP_SUMMARY)")
    args = parser.parse_args()

    try:
        result = compare(fold_rounds(args.base), fold_rounds(args.head))
    except ComparisonError as exc:
        print(f"cannot compare: {exc}", file=sys.stderr)
        return 2

    report = render(result, args.max_regression, args.base_label)
    print(report)
    if args.summary_file:
        with open(args.summary_file, "a", encoding="utf-8") as handle:
            handle.write(report)

    delta = result["ratio"] - 1.0
    if delta > args.max_regression:
        print(
            f"FAIL: the shared tests are {delta:+.1%} slower than "
            f"{args.base_label}, over the {args.max_regression:+.0%} budget.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
