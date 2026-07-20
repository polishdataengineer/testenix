"""Benchmark an existing pytest or unittest suite after safe Testenix migration.

The default scenario creates 3,000 deterministic tests, migrates them through
the public CLI, validates the audit report and source fingerprints, then
measures the source runner and native Testenix in alternating AB/BA rounds.
Only the Python standard library is required by this harness.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

ROOT = Path(__file__).resolve().parents[1]
COMMAND_TIMEOUT_SECONDS = 900.0
Framework = Literal["pytest", "unittest"]


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    returncode: int
    stdout: str
    stderr: str
    elapsed: float


@dataclass(frozen=True, slots=True)
class RunnerSpec:
    name: str
    command: tuple[str, ...]
    validation: Literal["pytest", "unittest", "native"]
    report_path: Path | None = None


@dataclass(frozen=True, slots=True)
class Measurement:
    command: str
    samples: tuple[float, ...]

    @property
    def median(self) -> float:
        return statistics.median(self.samples)

    @property
    def minimum(self) -> float:
        return min(self.samples)

    @property
    def maximum(self) -> float:
        return max(self.samples)

    @property
    def mean(self) -> float:
        return statistics.fmean(self.samples)

    @property
    def stdev(self) -> float:
        return statistics.stdev(self.samples) if len(self.samples) > 1 else 0.0


def _benchmark_environment() -> dict[str, str]:
    environment = os.environ.copy()
    source_root = str(ROOT / "src")
    inherited_path = environment.get("PYTHONPATH")
    environment.update(
        {
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONPATH": (
                f"{source_root}{os.pathsep}{inherited_path}" if inherited_path else source_root
            ),
        }
    )
    return environment


def _module_sizes(test_count: int, module_count: int) -> tuple[int, ...]:
    base, remainder = divmod(test_count, module_count)
    return tuple(base + int(index < remainder) for index in range(module_count))


def _generate_pytest_suite(
    tests_directory: Path,
    sizes: tuple[int, ...],
    *,
    delay_seconds: float,
) -> None:
    ordinal = 0
    for module_index, size in enumerate(sizes):
        lines = ["# Deterministic generated pytest benchmark module.", ""]
        if delay_seconds > 0:
            lines[1:1] = ["import time"]
        for _ in range(size):
            lines.extend(
                [
                    f"def test_generated_{ordinal:06d}() -> None:",
                    f"    value = {ordinal}",
                    *([f"    time.sleep({delay_seconds!r})"] if delay_seconds > 0 else []),
                    "    assert (value * 3) // 3 == value",
                    "",
                ]
            )
            ordinal += 1
        (tests_directory / f"test_generated_{module_index:04d}.py").write_text(
            "\n".join(lines),
            encoding="utf-8",
        )


def _generate_unittest_suite(
    tests_directory: Path,
    sizes: tuple[int, ...],
    *,
    delay_seconds: float,
) -> None:
    ordinal = 0
    for module_index, size in enumerate(sizes):
        lines = [
            "# Deterministic generated unittest benchmark module.",
            "import unittest",
            "",
            f"class TestGenerated{module_index:04d}(unittest.TestCase):",
        ]
        if delay_seconds > 0:
            lines[2:2] = ["import time"]
        for _ in range(size):
            lines.extend(
                [
                    f"    def test_generated_{ordinal:06d}(self) -> None:",
                    f"        value = {ordinal}",
                    *([f"        time.sleep({delay_seconds!r})"] if delay_seconds > 0 else []),
                    "        self.assertEqual((value * 3) // 3, value)",
                    "",
                ]
            )
            ordinal += 1
        (tests_directory / f"test_generated_{module_index:04d}.py").write_text(
            "\n".join(lines),
            encoding="utf-8",
        )


def _generate_project(
    project: Path,
    *,
    framework: Framework,
    test_count: int,
    module_count: int,
    delay_ms: float,
) -> Path:
    tests_directory = project / "tests"
    tests_directory.mkdir(parents=True)
    sizes = _module_sizes(test_count, module_count)
    delay_seconds = delay_ms / 1_000.0
    if framework == "pytest":
        _generate_pytest_suite(tests_directory, sizes, delay_seconds=delay_seconds)
    else:
        _generate_unittest_suite(tests_directory, sizes, delay_seconds=delay_seconds)
    return tests_directory


def _hash_files(project: Path, directory: Path) -> dict[str, str]:
    return {
        path.relative_to(project).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*.py"), key=lambda item: item.as_posix())
    }


def _snapshot_digest(snapshot: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for path, value in sorted(snapshot.items()):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(value.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ("taskkill", "/PID", str(process.pid), "/T", "/F"),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2.0)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()


def _run_process(
    command: tuple[str, ...],
    *,
    project: Path,
    environment: dict[str, str],
) -> ProcessOutcome:
    options: dict[str, Any] = {}
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        options["start_new_session"] = True
    started = time.perf_counter()
    process = subprocess.Popen(
        command,
        cwd=project,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **options,
    )
    try:
        stdout, stderr = process.communicate(timeout=COMMAND_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        _terminate_process_tree(process)
        stdout, stderr = process.communicate()
        raise RuntimeError(
            f"benchmark command timed out after {COMMAND_TIMEOUT_SECONDS:g}s: "
            f"{_display_command(command, project)}\nstdout:\n{stdout}\nstderr:\n{stderr}"
        ) from error
    except BaseException:
        _terminate_process_tree(process)
        process.communicate()
        raise
    return ProcessOutcome(
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
        elapsed=time.perf_counter() - started,
    )


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot read {label} JSON report at {path}: {error}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} JSON report is not an object: {path}")
    return value


def _command_failure(spec: RunnerSpec, outcome: ProcessOutcome, project: Path) -> RuntimeError:
    return RuntimeError(
        f"{spec.name} failed with exit {outcome.returncode}: "
        f"{_display_command(spec.command, project)}\n"
        f"stdout:\n{outcome.stdout}\nstderr:\n{outcome.stderr}"
    )


def _validate_sample(
    spec: RunnerSpec,
    outcome: ProcessOutcome,
    *,
    project: Path,
    expected_tests: int,
) -> None:
    if outcome.returncode != 0:
        raise _command_failure(spec, outcome, project)
    if spec.validation == "pytest":
        combined = f"{outcome.stdout}\n{outcome.stderr}"
        if re.search(rf"(?<!\d){expected_tests}\s+passed\b", combined) is None:
            raise RuntimeError(
                f"pytest sample did not report exactly {expected_tests} passed tests:\n{combined}"
            )
        return

    if spec.validation == "native" and spec.report_path is None:
        combined = f"{outcome.stdout}\n{outcome.stderr}"
        summary = rf"(?m)^{expected_tests} tests, {expected_tests} passed in "
        if re.search(summary, combined) is None:
            raise RuntimeError(
                f"native Testenix sample did not report exactly {expected_tests} "
                f"passing tests:\n{combined}"
            )
        return

    if spec.report_path is None or not spec.report_path.is_file():
        raise RuntimeError(f"{spec.name} did not create its validation report")
    report = _read_json(spec.report_path, label=spec.name)
    if spec.validation == "unittest":
        valid = (
            report.get("tests") == expected_tests
            and report.get("collected") == expected_tests
            and report.get("success") is True
            and report.get("failures") == 0
            and report.get("errors") == 0
            and report.get("unexpected_successes") == 0
        )
    else:
        tests = report.get("tests")
        valid = (
            report.get("exit_code") == 0
            and report.get("collection_issues") == []
            and isinstance(tests, list)
            and len(tests) == expected_tests
            and all(isinstance(test, dict) and test.get("status") == "pass" for test in tests)
        )
    if not valid:
        raise RuntimeError(
            f"{spec.name} report did not contain exactly {expected_tests} passing tests: "
            f"{spec.report_path}"
        )


def _run_sample(
    spec: RunnerSpec,
    *,
    project: Path,
    environment: dict[str, str],
    expected_tests: int,
) -> float:
    if spec.report_path is not None:
        spec.report_path.unlink(missing_ok=True)
    outcome = _run_process(spec.command, project=project, environment=environment)
    _validate_sample(spec, outcome, project=project, expected_tests=expected_tests)
    return outcome.elapsed


def _display_command(command: tuple[str, ...], project: Path) -> str:
    rendered: list[str] = []
    for value in command:
        if value == sys.executable:
            rendered.append("python")
            continue
        try:
            relative = Path(value).resolve().relative_to(project.resolve())
        except (OSError, ValueError):
            rendered.append(value)
        else:
            rendered.append(f"<project>/{relative.as_posix()}")
    return " ".join(rendered)


def _migration_command(framework: Framework, workers: int, report_path: Path) -> tuple[str, ...]:
    return (
        sys.executable,
        "-m",
        "testenix",
        "migrate",
        framework,
        "tests",
        "--output",
        "testenix_migrated",
        "--workers",
        str(workers),
        "--validation-timeout",
        str(int(COMMAND_TIMEOUT_SECONDS)),
        "--report-json",
        str(report_path),
    )


def _validate_migration(
    report: dict[str, Any],
    *,
    framework: Framework,
    test_count: int,
    before_hashes: dict[str, str],
    after_hashes: dict[str, str],
    output: Path,
) -> None:
    expected_counts = ("baseline", "native_serial", "native_parallel")
    summaries_are_valid = all(
        isinstance(report.get(name), dict)
        and report[name].get("tests") == test_count
        and report[name].get("exit_code") == 0
        and report[name].get("failed") == 0
        and report[name].get("errors") == 0
        and report[name].get("xpassed") == 0
        for name in expected_counts
    )
    if not (
        report.get("format") == "testenix.migration-report"
        and report.get("framework") == framework
        and report.get("status") == "published"
        and report.get("published") is True
        and report.get("originals_modified") is False
        and report.get("converted_tests") == test_count
        and isinstance(report.get("generated_files"), list)
        and report.get("source_hashes") == before_hashes
        and before_hashes == after_hashes
        and output.is_dir()
        and summaries_are_valid
    ):
        raise RuntimeError(
            "migration report or source-integrity verification failed:\n"
            + json.dumps(report, indent=2, sort_keys=True)
        )


def _migrate_project(
    project: Path,
    *,
    framework: Framework,
    workers: int,
    test_count: int,
    before_hashes: dict[str, str],
    environment: dict[str, str],
) -> tuple[dict[str, Any], ProcessOutcome, dict[str, str]]:
    report_path = project / "migration-report.json"
    command = _migration_command(framework, workers, Path("migration-report.json"))
    outcome = _run_process(command, project=project, environment=environment)
    spec = RunnerSpec("testenix migrate", command, "native", report_path)
    if outcome.returncode != 0:
        raise _command_failure(spec, outcome, project)
    report = _read_json(report_path, label="migration")
    after_hashes = _hash_files(project, project / "tests")
    _validate_migration(
        report,
        framework=framework,
        test_count=test_count,
        before_hashes=before_hashes,
        after_hashes=after_hashes,
        output=project / "testenix_migrated",
    )
    return report, outcome, after_hashes


def _runner_specs(
    project: Path,
    *,
    framework: Framework,
    workers: int,
) -> tuple[RunnerSpec, RunnerSpec]:
    if framework == "pytest":
        source = RunnerSpec(
            name="source_pytest",
            command=(
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "tests",
            ),
            validation="pytest",
        )
    else:
        unittest_report = project / ".benchmark-unittest.json"
        source = RunnerSpec(
            name="source_unittest",
            command=(
                sys.executable,
                "-m",
                "testenix._unittest_probe",
                "--output",
                str(unittest_report),
                "tests",
            ),
            validation="unittest",
            report_path=unittest_report,
        )
    native = RunnerSpec(
        name="testenix_native",
        command=(
            sys.executable,
            "-m",
            "testenix",
            "run",
            "testenix_migrated",
            "--workers",
            str(workers),
            "--no-history",
        ),
        validation="native",
    )
    return source, native


def _measure(
    specs: tuple[RunnerSpec, RunnerSpec],
    *,
    repeats: int,
    warmups: int,
    project: Path,
    environment: dict[str, str],
    test_count: int,
) -> tuple[dict[str, Measurement], list[list[str]], list[list[str]]]:
    warmup_orders: list[list[str]] = []
    for round_index in range(warmups):
        order = specs if round_index % 2 == 0 else tuple(reversed(specs))
        warmup_orders.append([spec.name for spec in order])
        for spec in order:
            _run_sample(
                spec,
                project=project,
                environment=environment,
                expected_tests=test_count,
            )

    samples: dict[str, list[float]] = {spec.name: [] for spec in specs}
    measured_orders: list[list[str]] = []
    for round_index in range(repeats):
        order = specs if round_index % 2 == 0 else tuple(reversed(specs))
        measured_orders.append([spec.name for spec in order])
        for spec in order:
            samples[spec.name].append(
                _run_sample(
                    spec,
                    project=project,
                    environment=environment,
                    expected_tests=test_count,
                )
            )
    return (
        {
            spec.name: Measurement(
                _display_command(spec.command, project),
                tuple(samples[spec.name]),
            )
            for spec in specs
        },
        warmup_orders,
        measured_orders,
    )


def _measurement_dict(measurement: Measurement, *, test_count: int) -> dict[str, Any]:
    return {
        "command": measurement.command,
        "samples_seconds": list(measurement.samples),
        "median_seconds": measurement.median,
        "minimum_seconds": measurement.minimum,
        "maximum_seconds": measurement.maximum,
        "range_seconds": measurement.maximum - measurement.minimum,
        "mean_seconds": measurement.mean,
        "stdev_seconds": measurement.stdev,
        "median_throughput_tests_per_second": test_count / measurement.median,
    }


def _git_value(*arguments: str) -> str | None:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _distribution_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "not-installed"


def _cpu_model() -> str:
    if sys.platform == "darwin":
        completed = subprocess.run(
            ("sysctl", "-n", "machdep.cpu.brand_string"),
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            return completed.stdout.strip()
    if sys.platform.startswith("linux"):
        try:
            cpuinfo = Path("/proc/cpuinfo").read_text(encoding="utf-8")
        except OSError:
            cpuinfo = ""
        for line in cpuinfo.splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip().lower() in {"hardware", "model name"}:
                return value.strip()
    return (
        platform.processor().strip()
        or os.environ.get("PROCESSOR_IDENTIFIER", "").strip()
        or "unknown"
    )


def _provenance() -> dict[str, Any]:
    status = _git_value("status", "--porcelain")
    lock = ROOT / "uv.lock"
    return {
        "commit": _git_value("rev-parse", "HEAD"),
        "dirty": bool(status) if status is not None else None,
        "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest() if lock.exists() else None,
        "versions": {
            "pytest": _distribution_version("pytest"),
            "python": platform.python_version(),
            "testenix": _distribution_version("testenix"),
            "unittest": f"stdlib-{platform.python_version()}",
        },
    }


def _portable_report(report: dict[str, Any], project: Path) -> dict[str, Any]:
    prefixes = tuple(
        sorted(
            {str(project), str(project.resolve())},
            key=len,
            reverse=True,
        )
    )

    def portable(value: Any) -> Any:
        if isinstance(value, str):
            for prefix in prefixes:
                value = value.replace(prefix, "<project>")
            return value
        if isinstance(value, list):
            return [portable(item) for item in value]
        if isinstance(value, dict):
            return {str(key): portable(item) for key, item in value.items()}
        return value

    return cast(dict[str, Any], portable(report))


def run_benchmark(
    *,
    framework: Framework,
    test_count: int,
    module_count: int,
    workers: int,
    repeats: int,
    warmups: int,
    delay_ms: float = 0.0,
) -> dict[str, Any]:
    if not math.isfinite(delay_ms) or delay_ms < 0:
        raise ValueError("delay_ms must be finite and non-negative")
    delay_ms = 0.0 if delay_ms == 0 else delay_ms
    environment = _benchmark_environment()
    with tempfile.TemporaryDirectory(prefix="testenix-migration-benchmark-") as temporary:
        project = Path(temporary)
        tests_directory = _generate_project(
            project,
            framework=framework,
            test_count=test_count,
            module_count=module_count,
            delay_ms=delay_ms,
        )
        before_hashes = _hash_files(project, tests_directory)
        migration_report, migration_outcome, after_hashes = _migrate_project(
            project,
            framework=framework,
            workers=workers,
            test_count=test_count,
            before_hashes=before_hashes,
            environment=environment,
        )
        specs = _runner_specs(project, framework=framework, workers=workers)
        measurements, warmup_orders, measured_orders = _measure(
            specs,
            repeats=repeats,
            warmups=warmups,
            project=project,
            environment=environment,
            test_count=test_count,
        )
        final_hashes = _hash_files(project, tests_directory)
        if final_hashes != before_hashes:
            raise RuntimeError("a benchmark sample modified the original source suite")
        suite_digest = _snapshot_digest(before_hashes)
        portable_migration_report = _portable_report(migration_report, project)

    return {
        "schema_version": 1,
        "recorded_at": datetime.now(UTC).isoformat(),
        "provenance": _provenance(),
        "environment": {
            "cpu_count": os.cpu_count(),
            "cpu_model": _cpu_model(),
            "machine": platform.machine(),
            "platform": platform.platform(),
            "python_implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
        },
        "scenario": {
            "framework": framework,
            "delay_ms": delay_ms,
            "generated_module_count": module_count,
            "generated_test_count": test_count,
            "measured_execution_orders": measured_orders,
            "repeats": repeats,
            "suite_sha256": suite_digest,
            "warmup_execution_orders": warmup_orders,
            "warmups": warmups,
            "workers": workers,
            "workload_kind": "noop" if delay_ms == 0 else "fixed_sleep",
        },
        "migration": {
            "command": _display_command(
                _migration_command(
                    framework,
                    workers,
                    Path("<project>/migration-report.json"),
                ),
                Path("<project>"),
            ),
            "wall_seconds": migration_outcome.elapsed,
            "reported_seconds": portable_migration_report.get("duration"),
            "report": portable_migration_report,
        },
        "originals": {
            "after_measurements_sha256": _snapshot_digest(final_hashes),
            "after_migration_sha256": _snapshot_digest(after_hashes),
            "before_sha256": suite_digest,
            "file_count": len(before_hashes),
            "modified": False,
        },
        "originals_modified": False,
        "measurements": {
            name: _measurement_dict(measurement, test_count=test_count)
            for name, measurement in measurements.items()
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--framework", choices=("pytest", "unittest"), required=True)
    parser.add_argument("--tests", type=int, default=3_000)
    parser.add_argument("--modules", type=int, default=16)
    parser.add_argument("--workers", type=int, default=max(2, min(4, os.cpu_count() or 1)))
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument(
        "--delay-ms",
        type=float,
        default=0.0,
        help="fixed sleep added to every generated test in milliseconds (default: 0)",
    )
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    if arguments.tests < 1:
        raise SystemExit("--tests must be positive")
    if arguments.modules < 1 or arguments.modules > arguments.tests:
        raise SystemExit("--modules must be between 1 and --tests")
    if arguments.workers < 2:
        raise SystemExit("--workers must be at least 2 for the migration parallel gate")
    if arguments.repeats < 1:
        raise SystemExit("--repeats must be positive")
    if arguments.warmups < 0:
        raise SystemExit("--warmups cannot be negative")
    if not math.isfinite(arguments.delay_ms) or arguments.delay_ms < 0:
        raise SystemExit("--delay-ms must be finite and non-negative")
    delay_ms = 0.0 if arguments.delay_ms == 0 else arguments.delay_ms

    result = run_benchmark(
        framework=arguments.framework,
        test_count=arguments.tests,
        module_count=arguments.modules,
        workers=arguments.workers,
        repeats=arguments.repeats,
        warmups=arguments.warmups,
        delay_ms=delay_ms,
    )
    rendered = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
