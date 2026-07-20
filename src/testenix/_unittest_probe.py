"""Run unittest discovery and emit a machine-readable validation summary.

This private module is intentionally independent of the Testenix CLI.  The
migration service can launch it as ``python -m testenix._unittest_probe`` in a
disposable shadow project and compare the observed baseline with native
Testenix results.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, cast

EXIT_OK = 0
EXIT_TEST_FAILURE = 1
EXIT_USAGE = 2
EXIT_INTERNAL_ERROR = 3
EXIT_INTERRUPTED = 130

ExceptionInfo = tuple[type[BaseException], BaseException, TracebackType] | tuple[None, None, None]


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    """One terminal outcome observed by the unittest result adapter."""

    test_id: str
    status: str


class JsonTestResult(unittest.TextTestResult):
    """TextTestResult that retains compact, traceback-free outcome records."""

    def __init__(
        self,
        stream: unittest.runner._WritelnDecorator,
        descriptions: bool,
        verbosity: int,
    ) -> None:
        super().__init__(stream, descriptions, verbosity)
        self.outcomes: list[ProbeOutcome] = []

    def addSuccess(self, test: unittest.case.TestCase) -> None:  # noqa: N802
        super().addSuccess(test)
        self.outcomes.append(ProbeOutcome(test.id(), "pass"))

    def addFailure(
        self,
        test: unittest.case.TestCase,
        err: ExceptionInfo,
    ) -> None:  # noqa: N802
        super().addFailure(test, err)
        self.outcomes.append(ProbeOutcome(test.id(), "fail"))

    def addError(
        self,
        test: unittest.case.TestCase,
        err: ExceptionInfo,
    ) -> None:  # noqa: N802
        super().addError(test, err)
        self.outcomes.append(ProbeOutcome(test.id(), "error"))

    def addSkip(self, test: unittest.case.TestCase, reason: str) -> None:  # noqa: N802
        super().addSkip(test, reason)
        self.outcomes.append(ProbeOutcome(test.id(), "skip"))

    def addExpectedFailure(
        self,
        test: unittest.case.TestCase,
        err: ExceptionInfo,
    ) -> None:  # noqa: N802
        super().addExpectedFailure(test, err)
        self.outcomes.append(ProbeOutcome(test.id(), "xfail"))

    def addUnexpectedSuccess(self, test: unittest.case.TestCase) -> None:  # noqa: N802
        super().addUnexpectedSuccess(test)
        self.outcomes.append(ProbeOutcome(test.id(), "xpass"))

    def addSubTest(
        self,
        test: unittest.case.TestCase,
        subtest: unittest.case.TestCase,
        err: ExceptionInfo | None,
    ) -> None:  # noqa: N802
        super().addSubTest(test, subtest, err)
        if err is not None:
            exception_type = err[0]
            status = (
                "fail"
                if exception_type is not None and issubclass(exception_type, test.failureException)
                else "error"
            )
            self.outcomes.append(ProbeOutcome(subtest.id(), status))


def build_parser() -> argparse.ArgumentParser:
    """Build the standalone probe argument parser."""

    parser = argparse.ArgumentParser(
        prog="python -m testenix._unittest_probe",
        description="Discover unittest tests and write a JSON result summary",
    )
    parser.add_argument("paths", nargs="+", help="test file or directory to discover")
    parser.add_argument(
        "--pattern",
        action="append",
        dest="patterns",
        help="discovery filename pattern; repeatable (default: test*.py and *_test.py)",
    )
    parser.add_argument(
        "--output",
        default="-",
        help="summary JSON path, or '-' for stdout (default: -)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Discover requested paths, run the suite, and write one JSON document."""

    arguments = build_parser().parse_args(argv)
    started_at = time.time()
    try:
        paths = _validated_paths(arguments.paths)
        patterns = tuple(arguments.patterns or ("test*.py", "*_test.py"))
        if any(not pattern or Path(pattern).name != pattern for pattern in patterns):
            raise ProbeUsageError("discovery patterns must be non-empty file-name patterns")
        suite = _discover(paths, patterns)
        collected_ids = tuple(test.id() for test in _iter_tests(suite))
    except ProbeUsageError as error:
        summary = _empty_summary(started_at, error=str(error), error_kind="usage")
        try:
            _write_summary(arguments.output, summary)
        except OSError as write_error:
            print(f"unittest probe: cannot write summary: {write_error}", file=sys.stderr)
            return EXIT_INTERNAL_ERROR
        return EXIT_USAGE
    except Exception as error:
        summary = _empty_summary(started_at, error=str(error), error_kind="discovery")
        try:
            _write_summary(arguments.output, summary)
        except OSError as write_error:
            print(f"unittest probe: cannot write summary: {write_error}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR

    captured_stdout = io.StringIO()
    captured_stderr = io.StringIO()
    runner_stream = io.StringIO()
    runner = unittest.TextTestRunner(
        stream=runner_stream,
        verbosity=0,
        resultclass=cast(Any, JsonTestResult),
    )
    try:
        with (
            contextlib.redirect_stdout(captured_stdout),
            contextlib.redirect_stderr(captured_stderr),
        ):
            result = runner.run(suite)
    except KeyboardInterrupt:
        summary = _empty_summary(
            started_at,
            collected_ids=collected_ids,
            error="unittest execution interrupted",
            error_kind="interrupted",
        )
        try:
            _write_summary(arguments.output, summary)
        except OSError as write_error:
            print(f"unittest probe: cannot write summary: {write_error}", file=sys.stderr)
        return EXIT_INTERRUPTED
    except BaseException as error:
        summary = _empty_summary(
            started_at,
            collected_ids=collected_ids,
            error=f"{type(error).__name__}: {error}",
            error_kind="execution",
        )
        try:
            _write_summary(arguments.output, summary)
        except OSError as write_error:
            print(f"unittest probe: cannot write summary: {write_error}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR

    result = cast(JsonTestResult, result)
    summary = {
        "schema_version": 1,
        "framework": "unittest",
        "collected": len(collected_ids),
        "test_ids": list(collected_ids),
        "tests": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "expected_failures": len(result.expectedFailures),
        "unexpected_successes": len(result.unexpectedSuccesses),
        "success": result.wasSuccessful(),
        "outcomes": [
            {"id": outcome.test_id, "status": outcome.status} for outcome in result.outcomes
        ],
        "duration": max(0.0, time.time() - started_at),
    }
    try:
        _write_summary(arguments.output, summary)
    except OSError as error:
        print(f"unittest probe: cannot write summary: {error}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR
    return EXIT_OK if result.wasSuccessful() else EXIT_TEST_FAILURE


class ProbeUsageError(ValueError):
    """Raised for controlled path or discovery-pattern errors."""


def _validated_paths(raw_paths: Sequence[str]) -> tuple[Path, ...]:
    paths: list[Path] = []
    for raw_path in raw_paths:
        path = Path(raw_path).expanduser()
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ProbeUsageError(f"test path does not exist: {path}") from error
        if not resolved.is_file() and not resolved.is_dir():
            raise ProbeUsageError(f"test path is not a file or directory: {resolved}")
        if resolved.is_file() and resolved.suffix != ".py":
            raise ProbeUsageError(f"test file is not a Python module: {resolved}")
        paths.append(resolved)
    return tuple(paths)


def _discover(paths: Sequence[Path], patterns: Sequence[str]) -> unittest.TestSuite:
    unique: dict[str, unittest.case.TestCase] = {}
    for path in paths:
        start_directory = path if path.is_dir() else path.parent
        selected_patterns = patterns if path.is_dir() else (path.name,)
        top_level = _discovery_top_level(start_directory)
        for pattern in selected_patterns:
            loader = unittest.TestLoader()
            try:
                discovered = loader.discover(
                    start_dir=str(start_directory),
                    pattern=pattern,
                    top_level_dir=str(top_level),
                )
            except (ImportError, OSError, TypeError, ValueError) as error:
                raise ProbeUsageError(f"cannot discover {path}: {error}") from error
            for test in _iter_tests(discovered):
                unique.setdefault(test.id(), test)
    return unittest.TestSuite(unique[test_id] for test_id in sorted(unique))


def _discovery_top_level(start_directory: Path) -> Path:
    cursor = start_directory
    while (cursor / "__init__.py").is_file():
        cursor = cursor.parent
    return cursor


def _iter_tests(suite: unittest.TestSuite) -> Iterator[unittest.case.TestCase]:
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_tests(item)
        elif isinstance(item, unittest.case.TestCase):
            yield item


def _empty_summary(
    started_at: float,
    *,
    collected_ids: Sequence[str] = (),
    error: str,
    error_kind: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "framework": "unittest",
        "collected": len(collected_ids),
        "test_ids": list(collected_ids),
        "tests": 0,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
        "expected_failures": 0,
        "unexpected_successes": 0,
        "success": False,
        "outcomes": [],
        "duration": max(0.0, time.time() - started_at),
        "probe_error": {"kind": error_kind, "message": error},
    }


def _write_summary(destination: str, summary: dict[str, object]) -> None:
    payload = json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n"
    if destination == "-":
        output_stream = sys.__stdout__
        if output_stream is None:
            raise OSError("original stdout is unavailable")
        output_stream.write(payload)
        output_stream.flush()
        return
    output = Path(destination).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EXIT_INTERNAL_ERROR",
    "EXIT_INTERRUPTED",
    "EXIT_OK",
    "EXIT_TEST_FAILURE",
    "EXIT_USAGE",
    "JsonTestResult",
    "ProbeOutcome",
    "build_parser",
    "main",
]
