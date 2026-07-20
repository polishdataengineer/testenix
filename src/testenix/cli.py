"""Testenix command-line interface.

The application service is imported lazily.  Its integration contract is::

    testenix.runner.run(paths: Sequence[str], config: TestenixConfig) -> RunResult

The CLI calls this synchronous service; async embedders use ``testenix.run_async``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from testenix import __version__
from testenix.config import ConfigError, TestenixConfig, load_config
from testenix.contracts import RunResult
from testenix.reporters import ConsoleReporter, JsonReporter, JUnitReporter

EXIT_OK = 0
EXIT_TEST_FAILURE = 1
EXIT_USAGE = 2
EXIT_INTERNAL_ERROR = 3
EXIT_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="testenix", description="Run Python tests with Testenix")
    parser.add_argument("--version", action="version", version=f"Testenix {__version__}")
    parser.add_argument(
        "--config",
        dest="global_config",
        type=Path,
        help="pyproject.toml containing [tool.testenix]",
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
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
