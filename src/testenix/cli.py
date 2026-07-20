"""Testenix command-line interface.

The application service is imported lazily.  Its integration contract is::

    testenix.runner.run(paths: Sequence[str], config: TestenixConfig) -> RunResult

The CLI calls this synchronous service; async embedders use ``testenix.run_async``.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from testenix import __version__
from testenix.config import ConfigError, TestenixConfig, load_config
from testenix.contracts import RunResult
from testenix.reporters import ConsoleReporter, JsonReporter, JUnitReporter

if TYPE_CHECKING:
    from testenix.migration_service import MigrationOptions, MigrationReport

EXIT_OK = 0
EXIT_TEST_FAILURE = 1
EXIT_USAGE = 2
EXIT_INTERNAL_ERROR = 3
EXIT_UNSUPPORTED = 4
EXIT_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="testenix", description="Run Python tests with Testenix")
    parser.add_argument("--version", action="version", version=f"Testenix {__version__}")
    parser.add_argument(
        "--config",
        dest="global_config",
        type=Path,
        help="pyproject.toml containing [tool.testenix] for the native run command",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="discover and run tests")
    run_parser.add_argument(
        "paths",
        nargs="*",
        help="test files or directories (default: [tool.testenix].paths, otherwise tests)",
    )
    run_parser.add_argument(
        "--config",
        dest="run_config",
        type=Path,
        help="pyproject.toml containing [tool.testenix]",
    )
    run_parser.add_argument("-w", "--workers", type=_worker_count, default=None)
    run_parser.add_argument("--retries", type=int, default=None)
    run_parser.add_argument("--timeout", type=float, default=None, metavar="SECONDS")
    run_parser.add_argument(
        "-t",
        "--tag",
        dest="tags",
        action="append",
        default=None,
        help="run tests with this tag; repeat for multiple tags",
    )
    run_parser.add_argument("--json", dest="json_path", type=Path, default=None)
    run_parser.add_argument("--junit", dest="junit_path", type=Path, default=None)
    history_group = run_parser.add_mutually_exclusive_group()
    history_group.add_argument("--history", dest="history_path", type=Path, default=None)
    history_group.add_argument("--no-history", action="store_true")
    run_parser.set_defaults(handler=_run_command)

    # Pytest owns the complete argument grammar for this compatibility command.
    # ``main`` intercepts it before argparse so flags such as ``-q`` and ``-k``
    # can pass through unchanged; this placeholder keeps top-level help useful.
    pytest_parser = subparsers.add_parser(
        "pytest",
        add_help=False,
        help="run an existing pytest suite through the compatibility adapter",
    )
    pytest_parser.set_defaults(handler=_misplaced_pytest_command)

    migrate_parser = subparsers.add_parser(
        "migrate",
        help="convert a supported pytest or unittest suite to native Testenix safely",
    )
    migrate_parser.add_argument(
        "framework",
        choices=("auto", "pytest", "unittest"),
        help="source framework; auto may combine separate pytest and unittest modules",
    )
    migrate_parser.add_argument(
        "paths",
        nargs="+",
        help="source test files or directories",
    )
    migrate_parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("testenix_migrated"),
        help="new output directory; it must not already exist (default: testenix_migrated)",
    )
    migrate_parser.add_argument(
        "-w",
        "--workers",
        type=_migration_worker_count,
        default="auto",
        help="worker count for parallel candidate validation (default: auto)",
    )
    migrate_parser.add_argument(
        "--validation-timeout",
        type=_positive_float,
        default=300.0,
        metavar="SECONDS",
        help="deadline for each validation run (default: 300)",
    )
    migration_mode = migrate_parser.add_mutually_exclusive_group()
    migration_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="analyze support without running tests or writing the output",
    )
    migration_mode.add_argument(
        "--check",
        action="store_true",
        help="convert and validate in temporary copies without publishing the output",
    )
    migrate_parser.add_argument(
        "--report-json",
        metavar="FILE|-",
        help=(
            "write the audit report to a new in-project file outside source/output suites, "
            "or '-' for stdout; never overwrite"
        ),
    )
    migrate_parser.set_defaults(handler=_migrate_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw_arguments = tuple(sys.argv[1:] if argv is None else argv)
    if raw_arguments[:1] == ("pytest",):
        return _pytest_command(raw_arguments[1:])

    parser = build_parser()
    arguments = parser.parse_args(raw_arguments)
    try:
        return int(arguments.handler(arguments))
    except ConfigError as error:
        print(f"testenix: configuration error: {error}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("testenix: interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED


def _run_command(arguments: argparse.Namespace) -> int:
    config_path = arguments.run_config or arguments.global_config
    config = load_config(config_path)
    overrides: dict[str, Any] = {}
    for name in ("workers", "retries", "timeout", "json_path", "junit_path"):
        value = getattr(arguments, name)
        if value is not None:
            overrides[name] = value
    if arguments.tags is not None:
        overrides["tags"] = tuple(arguments.tags)
    if arguments.no_history:
        overrides["history_path"] = None
    elif arguments.history_path is not None:
        overrides["history_path"] = arguments.history_path
    config = config.with_overrides(**overrides)

    paths = tuple(arguments.paths) if arguments.paths else config.paths
    try:
        result = _call_runner(paths, config)
    except KeyboardInterrupt:
        raise
    except Exception as error:  # the CLI is the final application boundary
        print(f"testenix: runner error: {error}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR

    if not isinstance(result, RunResult):
        print("testenix: runner returned an invalid result", file=sys.stderr)
        return EXIT_INTERNAL_ERROR

    ConsoleReporter().write(result)
    try:
        if config.json_path is not None:
            JsonReporter(config.json_path).write(result)
        if config.junit_path is not None:
            JUnitReporter(config.junit_path).write(result)
    except (OSError, ValueError) as error:
        print(f"testenix: cannot write report: {error}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR
    return result.exit_code


def _call_runner(paths: Sequence[str], config: TestenixConfig) -> RunResult:
    # Importing here keeps `testenix --help` usable even if an optional execution
    # backend cannot be imported in the current environment.
    from testenix.runner import run

    return run(paths, config)


def _pytest_command(arguments: Sequence[str]) -> int:
    from testenix.pytest_adapter import PytestInvocationError, PytestUnavailableError

    try:
        return _call_pytest(tuple(arguments))
    except KeyboardInterrupt:
        print("testenix: interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    except PytestUnavailableError as error:
        print(f"testenix: {error}", file=sys.stderr)
        return EXIT_USAGE
    except PytestInvocationError as error:
        print(f"testenix: {error}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR


def _call_pytest(arguments: Sequence[str]) -> int:
    # Kept behind this seam so the CLI contract can be tested without handing
    # the current test process to pytest.
    from testenix.pytest_adapter import run_pytest

    return run_pytest(arguments)


def _misplaced_pytest_command(arguments: argparse.Namespace) -> int:
    del arguments
    print(
        "testenix: put 'pytest' immediately after 'testenix'; "
        "Testenix --config applies only to the native run command",
        file=sys.stderr,
    )
    return EXIT_USAGE


def _migrate_command(arguments: argparse.Namespace) -> int:
    from testenix.migration_fs import (
        MigrationFilesystemError,
        validate_migration_paths,
        validate_migration_report_path,
    )
    from testenix.migration_service import (
        MigrationOptions,
        render_migration_summary,
        write_migration_report,
    )

    options = MigrationOptions(
        framework=arguments.framework,
        sources=tuple(Path(path) for path in arguments.paths),
        output=arguments.output,
        workers=arguments.workers,
        validation_timeout=arguments.validation_timeout,
        dry_run=arguments.dry_run,
        check_only=arguments.check,
    )
    report_path: Path | None = None
    if arguments.report_json and arguments.report_json != "-":
        requested_report = Path(arguments.report_json).expanduser()
        if os.path.lexists(requested_report):
            print(
                f"testenix: cannot write migration report: path already exists and "
                f"will not be replaced: {requested_report}",
                file=sys.stderr,
            )
            return EXIT_INTERNAL_ERROR
        try:
            migration_paths = validate_migration_paths(
                Path.cwd(),
                options.sources,
                options.output,
            )
            report_path = validate_migration_report_path(
                migration_paths,
                requested_report,
            )
        except MigrationFilesystemError as error:
            print(f"testenix: unsafe migration report path: {error}", file=sys.stderr)
            return EXIT_USAGE

    try:
        report = _call_migrator(options)
    except KeyboardInterrupt:
        raise
    except Exception as error:  # the CLI is the final application boundary
        print(f"testenix: migration error: {error}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR

    summary = render_migration_summary(report)
    stream = sys.stderr if report.exit_code or arguments.report_json == "-" else sys.stdout
    print(summary, file=stream)
    if arguments.report_json == "-":
        print(report.to_json(), end="")
    elif report_path is not None:
        try:
            write_migration_report(report, report_path)
        except OSError as error:
            qualifier = " after successful publication" if report.published else ""
            print(
                f"testenix: cannot write migration report{qualifier}: {error}",
                file=sys.stderr,
            )
            if report.published:
                print(
                    "testenix: the migrated output is complete and remains published; "
                    "only the optional report write failed",
                    file=sys.stderr,
                )
                return report.exit_code
            return EXIT_INTERNAL_ERROR
    return report.exit_code


def _call_migrator(options: MigrationOptions) -> MigrationReport:
    from testenix.migration_service import migrate

    return migrate(options)


def _worker_count(value: str) -> int | str:
    if value == "auto":
        return value
    try:
        workers = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("workers must be 'auto' or an integer") from error
    if workers < 1:
        raise argparse.ArgumentTypeError("workers must be at least 1")
    return workers


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("value must be a number") from error
    if parsed <= 0 or not math.isfinite(parsed):
        raise argparse.ArgumentTypeError("value must be a finite number greater than zero")
    return parsed


def _migration_worker_count(value: str) -> int | str:
    workers = _worker_count(value)
    if isinstance(workers, int) and workers < 2:
        raise argparse.ArgumentTypeError(
            "migration workers must be at least 2 for the parallel validation gate"
        )
    return workers
