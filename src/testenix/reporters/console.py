"""Deterministic, colour-free terminal reporting."""

from __future__ import annotations

import sys
from collections import Counter
from io import StringIO
from typing import TextIO

from testenix.contracts import PhaseResult, RunResult, Status, TestResult

_SUMMARY_LABELS = {
    Status.PASS: "passed",
    Status.FAIL: "failed",
    Status.ERROR_SETUP: "setup errors",
    Status.ERROR_TEARDOWN: "teardown errors",
    Status.SKIP: "skipped",
    Status.XFAIL: "xfailed",
    Status.XPASS: "xpassed",
    Status.TIMEOUT: "timed out",
    Status.CRASH: "crashed",
    Status.INFRA_ERROR: "infra errors",
    Status.CANCELLED: "cancelled",
    Status.NOT_RUN: "not run",
    Status.FLAKY: "flaky",
    Status.CACHED_PASS: "cached",
}


class ConsoleReporter:
    """Render results in manifest order reconstructed from source locations."""

    def render(self, run: RunResult) -> str:
        output = StringIO()
        output.write(f"Testenix run {run.run_id}\n")

        for issue in sorted(run.collection_issues, key=lambda item: (item.path, item.message)):
            output.write(f"COLLECT  {issue.path}\n")
            for line in issue.message.splitlines() or [""]:
                output.write(f"         {line}\n")
            if issue.traceback:
                for line in issue.traceback.rstrip().splitlines():
                    output.write(f"         {line}\n")

        for result in sorted(run.tests, key=_test_sort_key):
            output.write(
                f"{result.status.value.upper():<9} {result.test.id} [{result.duration:.3f}s]\n"
            )
            for line in _failure_details(result):
                output.write(f"          {line}\n")

        counts = Counter(test.status for test in run.tests)
        parts = [f"{len(run.tests)} tests"]
        parts.extend(
            f"{counts[status]} {_SUMMARY_LABELS[status]}" for status in Status if counts[status]
        )
        if run.collection_issues:
            parts.append(f"{len(run.collection_issues)} collection errors")
        duration = max(0.0, run.finished_at - run.started_at)
        output.write(f"{', '.join(parts)} in {duration:.3f}s\n")
        return output.getvalue()

    def write(self, run: RunResult, stream: TextIO | None = None) -> None:
        target = stream if stream is not None else sys.stdout
        target.write(self.render(run))

    report = write


def _test_sort_key(result: TestResult) -> tuple[str, int, str]:
    line = result.test.source_line if result.test.source_line is not None else -1
    return (result.test.path, line, result.test.id)


def _failure_details(result: TestResult) -> tuple[str, ...]:
    if result.status in {
        Status.PASS,
        Status.CACHED_PASS,
        Status.SKIP,
        Status.XFAIL,
        Status.NOT_RUN,
    }:
        return ()

    lines: list[str] = []
    for attempt in sorted(result.attempts, key=lambda item: item.attempt):
        for phase in attempt.phases:
            if _is_problem_phase(phase):
                heading = f"attempt {attempt.attempt}, {phase.phase.value}"
                if phase.message:
                    heading = f"{heading}: {phase.message}"
                lines.append(heading)
                if phase.traceback:
                    lines.extend(phase.traceback.rstrip().splitlines())

        for phase in attempt.phases:
            if phase.stdout:
                lines.append(f"[attempt {attempt.attempt} {phase.phase.value} stdout]")
                lines.extend(phase.stdout.rstrip().splitlines())
            if phase.stderr:
                lines.append(f"[attempt {attempt.attempt} {phase.phase.value} stderr]")
                lines.extend(phase.stderr.rstrip().splitlines())
    return tuple(lines)


def _is_problem_phase(phase: PhaseResult) -> bool:
    return phase.status not in {
        Status.PASS,
        Status.CACHED_PASS,
        Status.SKIP,
        Status.XFAIL,
    }
