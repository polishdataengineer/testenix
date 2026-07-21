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
    from testenix.sharding import TrustedCollectionManifest

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
        help="pyproject.toml containing [tool.testenix] for native run/tune commands",
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
    verbosity_group = run_parser.add_mutually_exclusive_group()
    verbosity_group.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="show failures and the final summary only",
    )
    verbosity_group.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="increase detail; repeat for full captured output",
    )
    color_group = run_parser.add_mutually_exclusive_group()
    color_group.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="color output: auto, always, or never (default: auto)",
    )
    color_group.add_argument(
        "--no-color",
        dest="color",
        action="store_const",
        const="never",
        help="alias for --color never",
    )
    run_parser.add_argument(
        "--show-skips",
        action="store_true",
        help="show details for skipped and expected-failure tests",
    )
    run_parser.add_argument(
        "--durations",
        type=_non_negative_int,
        default=None,
        metavar="N",
        help="show the N slowest tests; 0 shows all",
    )
    run_parser.add_argument("--json", dest="json_path", type=Path, default=None)
    run_parser.add_argument("--junit", dest="junit_path", type=Path, default=None)
    history_group = run_parser.add_mutually_exclusive_group()
    history_group.add_argument("--history", dest="history_path", type=Path, default=None)
    history_group.add_argument("--no-history", action="store_true")
    sharding_group = run_parser.add_mutually_exclusive_group()
    sharding_group.add_argument(
        "--shard-modules",
        dest="shard_modules",
        action="store_true",
        default=None,
        help="opt in to splitting statically safe modules across workers",
    )
    sharding_group.add_argument(
        "--no-shard-modules",
        dest="shard_modules",
        action="store_false",
        help="disable intra-module sharding configured in pyproject.toml",
    )
    run_parser.add_argument(
        "--manifest",
        dest="manifest_path",
        type=Path,
        default=None,
        help="trusted collection manifest; stale manifests fall back to isolated collection",
    )
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

    tune_parser = subparsers.add_parser(
        "tune",
        aliases=("benchmark",),
        help="benchmark native worker counts and recommend a project-local setting",
    )
    tune_parser.add_argument(
        "paths",
        nargs="*",
        help="native test files or directories (default: [tool.testenix].paths)",
    )
    tune_parser.add_argument(
        "--config",
        dest="tune_config",
        type=Path,
        help="pyproject.toml containing [tool.testenix]",
    )
    tune_parser.add_argument(
        "--candidates",
        type=_worker_candidates,
        metavar="N[,N...]",
        help="explicit worker counts to measure (default: adaptive powers-of-two sweep)",
    )
    tune_parser.add_argument(
        "--warmups",
        type=_non_negative_int,
        default=1,
        metavar="N",
        help="warmup runs per native candidate and pytest (default: 1)",
    )
    tune_parser.add_argument(
        "--repeats",
        type=_positive_int,
        default=5,
        metavar="N",
        help="measured runs per candidate (default: 5)",
    )
    tune_parser.add_argument(
        "--pytest-source",
        dest="pytest_paths",
        action="append",
        default=[],
        metavar="PATH",
        help="also time pytest on this source path; repeat for multiple paths",
    )
    tune_parser.add_argument(
        "--json",
        dest="tuning_json",
        metavar="FILE|-",
        help="write the tuning report as JSON, or '-' for stdout",
    )
    tune_parser.add_argument(
        "--write",
        action="store_true",
        help="persist the measured recommendation to [tool.testenix].workers",
    )
    tune_sharding_group = tune_parser.add_mutually_exclusive_group()
    tune_sharding_group.add_argument(
        "--shard-modules",
        dest="tune_shard_modules",
        action="store_true",
        default=None,
        help="benchmark opt-in safe intra-module sharding",
    )
    tune_sharding_group.add_argument(
        "--no-shard-modules",
        dest="tune_shard_modules",
        action="store_false",
        help="benchmark with module affinity even if sharding is configured",
    )
    tune_parser.add_argument(
        "--manifest",
        dest="tune_manifest_path",
        type=Path,
        default=None,
        help="trusted collection manifest used by every native sample",
    )
    tune_parser.set_defaults(handler=_tune_command)

    manifest_parser = subparsers.add_parser(
        "manifest",
        help="create an explicit trusted collection manifest in an isolated worker",
    )
    manifest_parser.add_argument("paths", nargs="+", help="native test files or directories")
    manifest_parser.add_argument(
        "--output",
        required=True,
        metavar="FILE|-",
        help="write a new manifest file, or '-' for stdout; never overwrite",
    )
    manifest_parser.set_defaults(handler=_manifest_command)
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
    for name in (
        "workers",
        "retries",
        "timeout",
        "json_path",
        "junit_path",
        "manifest_path",
        "shard_modules",
    ):
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
    trusted_manifest = _load_trusted_manifest(config.manifest_path)
    if trusted_manifest is not None:
        from testenix.sharding import verify_trusted_collection_manifest

        if not verify_trusted_collection_manifest(trusted_manifest, paths):
            print(
                "testenix: collection manifest is stale or does not match these paths; "
                "falling back to isolated collection",
                file=sys.stderr,
            )
    try:
        result = (
            _call_runner(paths, config)
            if trusted_manifest is None
            else _call_runner(paths, config, trusted_manifest=trusted_manifest)
        )
    except KeyboardInterrupt:
        raise
    except Exception as error:  # the CLI is the final application boundary
        print(f"testenix: runner error: {error}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR

    if not isinstance(result, RunResult):
        print("testenix: runner returned an invalid result", file=sys.stderr)
        return EXIT_INTERNAL_ERROR

    verbosity = -1 if arguments.quiet else min(arguments.verbose, 2)
    workers = _reporter_worker_count(result, config)
    ConsoleReporter(
        verbosity=verbosity,
        color=arguments.color,
        show_skips=arguments.show_skips,
        durations=arguments.durations,
        workers=workers,
    ).write(result)
    try:
        if config.json_path is not None:
            JsonReporter(config.json_path).write(result)
        if config.junit_path is not None:
            JUnitReporter(config.junit_path).write(result)
    except (OSError, ValueError) as error:
        print(f"testenix: cannot write report: {error}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR
    return result.exit_code


def _call_runner(
    paths: Sequence[str],
    config: TestenixConfig,
    *,
    trusted_manifest: TrustedCollectionManifest | None = None,
) -> RunResult:
    # Importing here keeps `testenix --help` usable even if an optional execution
    # backend cannot be imported in the current environment.
    from testenix.runner import run
    from testenix.sharding import ShardingPolicy

    return run(
        paths,
        config,
        sharding_policy=ShardingPolicy(intra_module=config.shard_modules),
        trusted_manifest=trusted_manifest,
    )


def _load_trusted_manifest(path: Path | None) -> TrustedCollectionManifest | None:
    if path is None:
        return None
    from testenix.sharding import (
        CollectionManifestError,
        deserialize_trusted_collection_manifest,
    )

    try:
        data = path.read_bytes()
        return deserialize_trusted_collection_manifest(data)
    except (OSError, CollectionManifestError) as error:
        raise ConfigError(f"cannot read collection manifest {path}: {error}") from error


def _reporter_worker_count(result: RunResult, config: TestenixConfig) -> int:
    """Mirror the native runner's initial module/timeout execution units."""

    if result.workers_used is not None:
        return result.workers_used
    shared_modules = {test.test.path for test in result.tests if test.test.timeout is None}
    isolated_tests = sum(test.test.timeout is not None for test in result.tests)
    return min(config.resolved_workers, len(shared_modules) + isolated_tests)


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


def _manifest_command(arguments: argparse.Namespace) -> int:
    from testenix.runner import collect_trusted_manifest
    from testenix.sharding import CollectionManifestError, serialize_trusted_collection_manifest

    output = None if arguments.output == "-" else Path(arguments.output).expanduser()
    if output is not None:
        try:
            _validate_new_output(output, label="collection manifest")
        except ConfigError as error:
            print(f"testenix: cannot write collection manifest: {error}", file=sys.stderr)
            return EXIT_USAGE
    try:
        manifest = collect_trusted_manifest(tuple(arguments.paths))
        encoded = serialize_trusted_collection_manifest(manifest) + "\n"
        if output is None:
            print(encoded, end="")
        else:
            _write_new_text(output, encoded)
    except KeyboardInterrupt:
        raise
    except (CollectionManifestError, OSError, ValueError) as error:
        print(f"testenix: manifest error: {error}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR
    except Exception as error:  # the CLI is the final application boundary
        print(f"testenix: manifest error: {error}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR
    if output is not None:
        print(f"Wrote trusted collection manifest to {output}")
    return EXIT_OK


def _tune_command(arguments: argparse.Namespace) -> int:
    from testenix.config import write_worker_recommendation
    from testenix.tuning import TuningError, render_tuning_report, run_tuning

    config_path = arguments.tune_config or arguments.global_config
    loaded_config = load_config(config_path)
    tune_overrides: dict[str, Any] = {}
    if arguments.tune_shard_modules is not None:
        tune_overrides["shard_modules"] = arguments.tune_shard_modules
    if arguments.tune_manifest_path is not None:
        tune_overrides["manifest_path"] = arguments.tune_manifest_path
    transient_profile = _different_tune_profile_overrides(loaded_config, tune_overrides)
    if arguments.write and transient_profile:
        rendered = ", ".join(transient_profile)
        print(
            "testenix: tuning error: --write refuses a workers-only recommendation "
            f"measured with transient execution-profile override(s): {rendered}; "
            "add the profile to [tool.testenix] or rerun without --write",
            file=sys.stderr,
        )
        return EXIT_USAGE
    config = loaded_config.with_overrides(**tune_overrides)
    paths = tuple(arguments.paths) if arguments.paths else config.paths
    destination = config_path or Path("pyproject.toml")
    if arguments.write and arguments.paths and not _same_paths(paths, config.paths):
        print(
            "testenix: tuning error: --write refuses to persist a recommendation for "
            "paths different from [tool.testenix].paths",
            file=sys.stderr,
        )
        return EXIT_USAGE

    report_output = (
        None
        if not arguments.tuning_json or arguments.tuning_json == "-"
        else Path(arguments.tuning_json).expanduser()
    )
    if report_output is not None:
        try:
            _validate_new_output(report_output, label="tuning report")
            if arguments.write and _same_file_target(report_output, destination):
                raise ConfigError("tuning report must not target the active configuration file")
        except ConfigError as error:
            print(f"testenix: cannot write tuning report: {error}", file=sys.stderr)
            return EXIT_USAGE

    trusted_manifest = _load_trusted_manifest(config.manifest_path)
    if trusted_manifest is not None:
        from testenix.sharding import verify_trusted_collection_manifest

        if not verify_trusted_collection_manifest(trusted_manifest, paths):
            print(
                "testenix: tuning error: manifest is stale or does not match the tuned paths; "
                "regenerate it before measuring",
                file=sys.stderr,
            )
            return EXIT_USAGE

    # The automatic sweep is bounded by the four-worker cold-start ceiling.
    candidate_limit = len(arguments.candidates) if arguments.candidates else 4
    native_runs = 1 + candidate_limit * (arguments.warmups + arguments.repeats)
    pytest_runs = arguments.warmups + arguments.repeats if arguments.pytest_paths else 0
    print(
        f"Testenix tune: up to {native_runs + pytest_runs} complete suite runs "
        f"({candidate_limit} native candidates).",
        file=sys.stderr,
    )
    try:
        report = run_tuning(
            paths,
            config,
            candidates=arguments.candidates,
            warmups=arguments.warmups,
            repeats=arguments.repeats,
            pytest_paths=tuple(arguments.pytest_paths),
        )
    except KeyboardInterrupt:
        raise
    except (TuningError, OSError, ValueError) as error:
        print(f"testenix: tuning error: {error}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR
    except Exception as error:  # the CLI is the final application boundary
        print(f"testenix: tuning error: {error}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR

    summary_stream = sys.stderr if arguments.tuning_json == "-" else sys.stdout
    print(render_tuning_report(report), end="", file=summary_stream)
    if arguments.tuning_json == "-":
        print(report.to_json(), end="")
    elif report_output is not None:
        try:
            _write_new_text(report_output, report.to_json())
        except OSError as error:
            print(f"testenix: cannot write tuning report: {error}", file=sys.stderr)
            return EXIT_INTERNAL_ERROR

    if arguments.write:
        try:
            changed = write_worker_recommendation(destination, report.recommended_workers)
        except ConfigError as error:
            print(f"testenix: cannot write tuning recommendation: {error}", file=sys.stderr)
            return EXIT_INTERNAL_ERROR
        action = "Wrote" if changed else "Kept"
        print(
            f"{action} workers = {report.recommended_workers} in {destination}",
            file=summary_stream,
        )
    return EXIT_OK


def _same_file_target(first: Path, second: Path) -> bool:
    return first.resolve(strict=False) == second.resolve(strict=False)


def _different_tune_profile_overrides(
    config: TestenixConfig,
    overrides: dict[str, Any],
) -> tuple[str, ...]:
    """Name transient measurement settings that differ from project config."""

    labels = {
        "manifest_path": "--manifest",
        "shard_modules": "--shard-modules/--no-shard-modules",
    }
    different: list[str] = []
    for name, requested in overrides.items():
        configured = getattr(config, name)
        if name.endswith("_path") and configured is not None and requested is not None:
            matches = _same_file_target(Path(configured), Path(requested))
        else:
            matches = configured == requested
        if not matches:
            different.append(labels.get(name, f"--{name.replace('_', '-')}"))
    return tuple(different)


def _same_paths(first: Sequence[str], second: Sequence[str]) -> bool:
    return tuple(Path(path).resolve(strict=False) for path in first) == tuple(
        Path(path).resolve(strict=False) for path in second
    )


def _validate_new_output(path: Path, *, label: str) -> None:
    if os.path.lexists(path):
        raise ConfigError(f"{label} path already exists and will not be replaced: {path}")


def _write_new_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="") as output:
        output.write(content)


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


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("value must be an integer") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be at least 0")
    return parsed


def _positive_int(value: str) -> int:
    parsed = _non_negative_int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return parsed


def _worker_candidates(value: str) -> tuple[int, ...]:
    candidates: set[int] = set()
    for item in value.split(","):
        stripped = item.strip()
        if not stripped:
            raise argparse.ArgumentTypeError("candidate worker counts cannot be empty")
        workers = _worker_count(stripped)
        if not isinstance(workers, int):
            raise argparse.ArgumentTypeError("candidate worker counts must be explicit integers")
        candidates.add(workers)
    return tuple(sorted(candidates))


def _migration_worker_count(value: str) -> int | str:
    workers = _worker_count(value)
    if isinstance(workers, int) and workers < 2:
        raise argparse.ArgumentTypeError(
            "migration workers must be at least 2 for the parallel validation gate"
        )
    return workers
