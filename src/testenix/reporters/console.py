"""Deterministic terminal reporting with compact and diagnostic views."""

from __future__ import annotations

import os
import shutil
import sys
from collections import Counter
from collections.abc import Mapping
from io import StringIO
from typing import Literal, TextIO

from testenix.contracts import PhaseResult, RunResult, Status, TestResult

ColorMode = Literal["auto", "always", "never"]

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

_NON_PROBLEM_STATUSES = {
    Status.PASS,
    Status.CACHED_PASS,
    Status.SKIP,
    Status.XFAIL,
}
_SKIP_STATUSES = {Status.SKIP, Status.XFAIL}
_ANSI_RESET = "\x1b[0m"
_ANSI_BY_STATUS = {
    Status.PASS: "\x1b[32m",
    Status.CACHED_PASS: "\x1b[32m",
    Status.SKIP: "\x1b[33m",
    Status.XFAIL: "\x1b[33m",
    Status.XPASS: "\x1b[33m",
    Status.FLAKY: "\x1b[33m",
    Status.FAIL: "\x1b[31m",
    Status.ERROR_SETUP: "\x1b[35m",
    Status.ERROR_TEARDOWN: "\x1b[35m",
    Status.TIMEOUT: "\x1b[35m",
    Status.CRASH: "\x1b[35m",
    Status.INFRA_ERROR: "\x1b[35m",
    Status.CANCELLED: "\x1b[35m",
    Status.NOT_RUN: "\x1b[35m",
}
_ANSI_COLLECT = "\x1b[31m"
_ANSI_HEADER = "\x1b[1;36m"
_DEFAULT_WIDTH = 100
_MAX_WIDTH = 120


class ConsoleReporter:
    """Render a run in quiet, compact, legacy, or diagnostic form.

    ``verbosity=1`` deliberately retains Testenix's original deterministic,
    colour-free output when all other arguments use their defaults. Compact
    output groups successful results by file, so its size is independent of
    the number of passing tests in a file.
    """

    def __init__(
        self,
        *,
        verbosity: int = 1,
        color: ColorMode = "never",
        show_skips: bool = False,
        durations: int | None = None,
        workers: int | None = None,
        width: int | None = None,
    ) -> None:
        if isinstance(verbosity, bool) or verbosity not in {-1, 0, 1, 2}:
            raise ValueError("verbosity must be one of -1, 0, 1, or 2")
        if color not in {"auto", "always", "never"}:
            raise ValueError("color must be 'auto', 'always', or 'never'")
        if not isinstance(show_skips, bool):
            raise TypeError("show_skips must be a bool")
        if durations is not None and (
            isinstance(durations, bool) or not isinstance(durations, int) or durations < 0
        ):
            raise ValueError("durations must be None or a non-negative integer")
        if workers is not None and (
            isinstance(workers, bool) or not isinstance(workers, int) or workers < 0
        ):
            raise ValueError("workers must be None or a non-negative integer")
        if width is not None and (
            isinstance(width, bool) or not isinstance(width, int) or width < 1
        ):
            raise ValueError("width must be None or a positive integer")

        self.verbosity: int = verbosity
        self.color: ColorMode = color
        self.show_skips: bool = show_skips
        self.durations: int | None = durations
        self.workers: int | None = workers
        self.width: int | None = width

    def render(self, run: RunResult) -> str:
        """Return deterministic text.

        ``auto`` colour is intentionally plain here because no destination is
        known. ``write`` resolves it against the actual stream and environment.
        """

        use_color = self.color == "always"
        width = min(self.width or _DEFAULT_WIDTH, _MAX_WIDTH)
        return self._render(run, use_color=use_color, width=width)

    def write(self, run: RunResult, stream: TextIO | None = None) -> None:
        """Write a run, resolving automatic colour and terminal width."""

        target = stream if stream is not None else sys.stdout
        use_color = _should_use_color(self.color, target, os.environ)
        width = min(self.width or _stream_width(target), _MAX_WIDTH)
        target.write(self._render(run, use_color=use_color, width=width))

    report = write

    def _render(self, run: RunResult, *, use_color: bool, width: int) -> str:
        output = StringIO()

        if self.verbosity == 0:
            file_count = len({result.test.path for result in run.tests})
            test_label = "test" if len(run.tests) == 1 else "tests"
            file_label = "file" if file_count == 1 else "files"
            header = f"Testenix  |  {len(run.tests)} {test_label}  |  {file_count} {file_label}"
            if self.workers is not None:
                worker_label = "worker" if self.workers == 1 else "workers"
                header = f"{header}  |  {self.workers} {worker_label}"
            output.write(f"{_paint_header(header, use_color)}\n\n")
        elif self.verbosity >= 1:
            header = f"Testenix run {run.run_id}"
            if self.verbosity == 2 and self.workers is not None:
                header = f"{header} [workers={self.workers}]"
            output.write(f"{header}\n")

        _write_collection_issues(output, run, use_color=use_color)

        ordered_tests = sorted(run.tests, key=_test_sort_key)
        if self.verbosity == 0:
            if run.collection_issues and ordered_tests:
                _write_section_break(output)
            self._write_compact_rows(output, ordered_tests, use_color=use_color, width=width)
            if _has_problems(ordered_tests):
                _write_section_break(output)
            self._write_problem_section(output, ordered_tests, use_color=use_color, width=width)
        elif self.verbosity == 1:
            self._write_legacy_rows(output, ordered_tests, use_color=use_color)
        elif self.verbosity == 2:
            if run.collection_issues and ordered_tests:
                _write_section_break(output)
            self._write_debug_rows(output, ordered_tests, use_color=use_color)
        else:
            if run.collection_issues and _has_problems(ordered_tests):
                _write_section_break(output)
            self._write_problem_section(output, ordered_tests, use_color=use_color, width=width)

        if self.show_skips and any(result.status in _SKIP_STATUSES for result in ordered_tests):
            if self.verbosity != 1 or ordered_tests:
                _write_section_break(output)
            self._write_skips_section(output, ordered_tests, use_color=use_color, width=width)
        if self.durations is not None and ordered_tests:
            _write_section_break(output)
            self._write_durations_section(output, ordered_tests, width=width)

        # Keep this line plain and stable: benchmark tooling and shell users
        # intentionally parse it from the start of a line.
        if self.verbosity != 1:
            _write_section_break(output)
        output.write(_summary_line(run))
        return output.getvalue()

    def _write_legacy_rows(
        self,
        output: StringIO,
        tests: list[TestResult],
        *,
        use_color: bool,
    ) -> None:
        for result in tests:
            status = f"{result.status.value.upper():<9}"
            output.write(
                f"{_paint_status(status, result.status, use_color)} "
                f"{result.test.id} [{result.duration:.3f}s]\n"
            )
            for line in _failure_details(result, include_not_run=False):
                output.write(f"          {line}\n")

    def _write_compact_rows(
        self,
        output: StringIO,
        tests: list[TestResult],
        *,
        use_color: bool,
        width: int,
    ) -> None:
        grouped: dict[str, list[TestResult]] = {}
        for result in tests:
            grouped.setdefault(result.test.path, []).append(result)

        rows: list[tuple[str, str, Status, str, str]] = []
        for path, path_tests in grouped.items():
            counts = Counter(result.status for result in path_tests)
            label, label_status = _group_label(counts)
            duration = sum(max(0.0, result.duration) for result in path_tests)
            rows.append(
                (
                    path,
                    label,
                    label_status,
                    _counts_text(counts),
                    f"[{_format_duration(duration)}]",
                )
            )

        counts_width = max((len(row[3]) for row in rows), default=0)
        duration_width = max((len(row[4]) for row in rows), default=0)
        for path, label, label_status, counts_text, duration_text in rows:
            prefix = f"{label:<5} "
            suffix_width = counts_width + 1 + duration_width
            path_width = max(0, width - len(prefix) - suffix_width - 2)
            fitted_path = _fit_path(path, path_width).ljust(path_width)
            suffix = f"{counts_text:<{counts_width}} {duration_text:>{duration_width}}"
            output.write(
                f"{_paint_status(prefix, label_status, use_color)}{fitted_path}  {suffix}\n"
            )

    def _write_problem_section(
        self,
        output: StringIO,
        tests: list[TestResult],
        *,
        use_color: bool,
        width: int,
    ) -> None:
        problems = [result for result in tests if result.status not in _NON_PROBLEM_STATUSES]
        if not problems:
            return

        output.write(f"Problems ({len(problems)})\n")
        for result in problems:
            status = f"{result.status.value.upper():<9}"
            identifier = _fit_path(result.test.id, width - len(status) - 2)
            output.write(f"{_paint_status(status, result.status, use_color)} {identifier}\n")
            for line in _failure_details(result):
                output.write(f"          {line}\n")

    def _write_debug_rows(
        self,
        output: StringIO,
        tests: list[TestResult],
        *,
        use_color: bool,
    ) -> None:
        for result in tests:
            status = f"{result.status.value.upper():<9}"
            output.write(
                f"{_paint_status(status, result.status, use_color)} "
                f"{result.test.id} [{_format_duration(result.duration)}]\n"
            )
            for line in _debug_details(result):
                output.write(f"          {line}\n")

    def _write_skips_section(
        self,
        output: StringIO,
        tests: list[TestResult],
        *,
        use_color: bool,
        width: int,
    ) -> None:
        skipped = [result for result in tests if result.status in _SKIP_STATUSES]
        if not skipped:
            return

        output.write(f"Skipped tests ({len(skipped)})\n")
        for result in skipped:
            reason = _skip_reason(result)
            suffix = f" - {reason}" if reason else ""
            status = f"{result.status.value.upper():<6}"
            identifier = _fit_path(result.test.id, width - len(status) - len(suffix) - 1)
            output.write(
                f"{_paint_status(status, result.status, use_color)} {identifier}{suffix}\n"
            )

    def _write_durations_section(
        self,
        output: StringIO,
        tests: list[TestResult],
        *,
        width: int,
    ) -> None:
        ordered = sorted(tests, key=lambda result: (-result.duration, _test_sort_key(result)))
        selected = ordered if self.durations == 0 else ordered[: self.durations]
        if not selected:
            return

        title = "Durations (all)" if self.durations == 0 else f"Slowest durations ({len(selected)})"
        output.write(f"{title}\n")
        for result in selected:
            duration = _format_duration(result.duration)
            identifier = _fit_path(result.test.id, width - len(duration) - 2)
            output.write(f"{duration:>9} {identifier}\n")


def _test_sort_key(result: TestResult) -> tuple[str, int, str]:
    line = result.test.source_line if result.test.source_line is not None else -1
    return (result.test.path, line, result.test.id)


def _write_collection_issues(output: StringIO, run: RunResult, *, use_color: bool) -> None:
    for issue in sorted(run.collection_issues, key=lambda item: (item.path, item.message)):
        label = f"{_ANSI_COLLECT}COLLECT{_ANSI_RESET}" if use_color else "COLLECT"
        output.write(f"{label}  {issue.path}\n")
        for line in issue.message.splitlines() or [""]:
            output.write(f"         {line}\n")
        if issue.traceback:
            for line in issue.traceback.rstrip().splitlines():
                output.write(f"         {line}\n")


def _failure_details(
    result: TestResult,
    *,
    include_not_run: bool = True,
) -> tuple[str, ...]:
    if result.status in _NON_PROBLEM_STATUSES or (
        result.status is Status.NOT_RUN and not include_not_run
    ):
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


def _debug_details(result: TestResult) -> tuple[str, ...]:
    lines: list[str] = []
    for attempt in sorted(result.attempts, key=lambda item: item.attempt):
        lines.append(
            f"attempt {attempt.attempt}: {attempt.status.value}, "
            f"worker={attempt.worker_id}, duration={_format_duration(attempt.duration)}"
        )
        for phase in attempt.phases:
            heading = (
                f"  {phase.phase.value}: {phase.status.value}, "
                f"duration={_format_duration(phase.duration)}"
            )
            if phase.message:
                heading = f"{heading}: {phase.message}"
            lines.append(heading)
            if phase.traceback:
                lines.extend(f"    {line}" for line in phase.traceback.rstrip().splitlines())
            if phase.stdout:
                lines.append(f"  [{phase.phase.value} stdout]")
                lines.extend(f"    {line}" for line in phase.stdout.rstrip().splitlines())
            if phase.stderr:
                lines.append(f"  [{phase.phase.value} stderr]")
                lines.extend(f"    {line}" for line in phase.stderr.rstrip().splitlines())
    return tuple(lines)


def _is_problem_phase(phase: PhaseResult) -> bool:
    return phase.status not in _NON_PROBLEM_STATUSES


def _group_label(counts: Counter[Status]) -> tuple[str, Status]:
    problems = [
        status for status in Status if counts[status] and status not in _NON_PROBLEM_STATUSES
    ]
    if problems:
        return "FAIL", problems[0]
    if counts[Status.PASS] or counts[Status.CACHED_PASS]:
        return "PASS", Status.PASS
    if counts[Status.SKIP] or counts[Status.XFAIL]:
        status = Status.SKIP if counts[Status.SKIP] else Status.XFAIL
        return "SKIP", status
    return "PASS", Status.PASS


def _has_problems(tests: list[TestResult]) -> bool:
    return any(result.status not in _NON_PROBLEM_STATUSES for result in tests)


def _counts_text(counts: Counter[Status]) -> str:
    return ", ".join(
        f"{counts[status]} {_SUMMARY_LABELS[status]}" for status in Status if counts[status]
    )


def _summary_line(run: RunResult) -> str:
    counts = Counter(test.status for test in run.tests)
    parts = [f"{len(run.tests)} tests"]
    parts.extend(
        f"{counts[status]} {_SUMMARY_LABELS[status]}" for status in Status if counts[status]
    )
    if run.collection_issues:
        parts.append(f"{len(run.collection_issues)} collection errors")
    duration = max(0.0, run.finished_at - run.started_at)
    return f"{', '.join(parts)} in {duration:.3f}s\n"


def _skip_reason(result: TestResult) -> str | None:
    if result.status is Status.SKIP and result.test.skip_reason:
        return result.test.skip_reason
    if result.status is Status.XFAIL and result.test.xfail_reason:
        return result.test.xfail_reason
    for attempt in sorted(result.attempts, key=lambda item: item.attempt):
        for phase in attempt.phases:
            if phase.message:
                return phase.message
    return None


def _format_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds >= 1.0:
        return f"{seconds:.3g}s"
    if seconds >= 0.001:
        return f"{seconds * 1_000:.3g}ms"
    return f"{seconds * 1_000_000:.3g}us"


def _fit_path(value: str, available: int) -> str:
    if available <= 0:
        return ""
    if len(value) <= available:
        return value
    if available <= 3:
        return "." * available

    remaining = available - 3
    left = remaining // 3
    right = remaining - left
    return f"{value[:left]}...{value[-right:]}"


def _paint_status(text: str, status: Status, enabled: bool) -> str:
    if not enabled:
        return text
    return f"{_ANSI_BY_STATUS[status]}{text}{_ANSI_RESET}"


def _paint_header(text: str, enabled: bool) -> str:
    if not enabled:
        return text
    return f"{_ANSI_HEADER}{text}{_ANSI_RESET}"


def _write_section_break(output: StringIO) -> None:
    position = output.tell()
    if position == 0:
        return
    output.seek(max(0, position - 2))
    tail = output.read()
    output.seek(position)
    if not tail.endswith("\n\n"):
        output.write("\n")


def _stream_width(stream: TextIO) -> int:
    if not _stream_is_tty(stream):
        return _DEFAULT_WIDTH
    detected = shutil.get_terminal_size(fallback=(_DEFAULT_WIDTH, 24)).columns
    return min(_MAX_WIDTH, max(1, detected))


def _stream_is_tty(stream: TextIO) -> bool:
    isatty = getattr(stream, "isatty", None)
    if not callable(isatty):
        return False
    try:
        return bool(isatty())
    except (OSError, ValueError):
        return False


def _should_use_color(mode: ColorMode, stream: TextIO, environ: Mapping[str, str]) -> bool:
    if mode == "always":
        return True
    if mode == "never":
        return False
    if "NO_COLOR" in environ:
        return False
    if _env_enabled(environ.get("FORCE_COLOR")):
        return True
    if _env_enabled(environ.get("CI")):
        return False
    if environ.get("TERM", "").lower() == "dumb":
        return False
    return _stream_is_tty(stream)


def _env_enabled(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() not in {"", "0", "false", "no", "off"}
