"""Benchmark a local real project from a redaction-safe JSON manifest.

The harness never invokes a shell and never copies project source into its result. Commands are
argument arrays executed from ``--project``. Output records timing, aggregate output sizes,
content fingerprints, and Git provenance without stdout/stderr or environment values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

ROOT = Path(__file__).resolve().parents[1]
RunnerKind = Literal["pytest", "testenix"]


@dataclass(frozen=True, slots=True)
class Runner:
    name: str
    kind: RunnerKind
    command: tuple[str, ...]
    redact_arguments: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Sample:
    elapsed: float
    stdout_bytes: int
    stderr_bytes: int
    observed_workers: int | None


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot read benchmark manifest {path}: {error}") from error
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise RuntimeError("manifest must be a schema_version 1 JSON object")
    return value


def _positive_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RuntimeError(f"manifest {name} must be a positive integer")
    return value


def _non_negative_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"manifest {name} must be a non-negative integer")
    return value


def _runners(manifest: dict[str, Any]) -> tuple[Runner, ...]:
    raw_runners = manifest.get("runners")
    if not isinstance(raw_runners, list) or len(raw_runners) < 2:
        raise RuntimeError("manifest runners must contain at least two runner objects")
    runners: list[Runner] = []
    for raw in raw_runners:
        if not isinstance(raw, dict):
            raise RuntimeError("every manifest runner must be an object")
        name = raw.get("name")
        kind = raw.get("kind")
        command = raw.get("command")
        if not isinstance(name, str) or not name or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
            raise RuntimeError("runner names must use lowercase letters, numbers, '-' or '_'")
        if kind not in {"pytest", "testenix"}:
            raise RuntimeError(f"runner {name}: kind must be pytest or testenix")
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(argument, str) or not argument for argument in command)
        ):
            raise RuntimeError(f"runner {name}: command must be a non-empty string array")
        if command[0] != "{python}":
            raise RuntimeError(
                f"runner {name}: command must start with {{python}} so the recorded "
                "interpreter and package versions match the executed command"
            )
        rendered = tuple(
            sys.executable if argument == "{python}" else argument for argument in command
        )
        if any("{" in argument or "}" in argument for argument in rendered):
            raise RuntimeError(f"runner {name}: only the exact {{python}} placeholder is supported")
        required_prefix = (
            (sys.executable, "-m", "pytest")
            if kind == "pytest"
            else (sys.executable, "-m", "testenix", "run")
        )
        if rendered[: len(required_prefix)] != required_prefix:
            expected = " ".join(("{python}", *required_prefix[1:]))
            raise RuntimeError(
                f"runner {name}: {kind} benchmarks must use the canonical {expected!r} "
                "entrypoint; kind labels cannot describe arbitrary scripts"
            )
        redactions = raw.get("redact_arguments", [])
        if (
            not isinstance(redactions, list)
            or any(isinstance(index, bool) or not isinstance(index, int) for index in redactions)
            or any(index < 0 or index >= len(rendered) for index in redactions)
        ):
            raise RuntimeError(
                f"runner {name}: redact_arguments must contain valid argument indexes"
            )
        runners.append(Runner(name, kind, rendered, tuple(sorted(set(redactions)))))
    if len({runner.name for runner in runners}) != len(runners):
        raise RuntimeError("runner names must be unique")
    kinds = {runner.kind for runner in runners}
    if kinds != {"pytest", "testenix"}:
        raise RuntimeError("manifest runners must include at least one pytest and one testenix run")
    return tuple(runners)


def _environment(
    manifest: dict[str, Any],
) -> tuple[dict[str, str], tuple[str, ...], str]:
    environment = os.environ.copy()
    environment["PYTHONHASHSEED"] = "0"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    raw = manifest.get("environment", {})
    if not isinstance(raw, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in raw.items()
    ):
        raise RuntimeError("manifest environment must map strings to strings")
    environment.update(raw)
    fingerprint = hashlib.sha256(
        json.dumps(sorted(environment.items()), separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return environment, tuple(sorted(raw)), fingerprint


def _package_runtime_identity(
    project: Path,
    environment: dict[str, str],
    *,
    module_name: str,
    distribution_name: str,
) -> dict[str, Any]:
    probe = """
import hashlib
import importlib
import importlib.metadata
import json
import sys
from pathlib import Path

module = importlib.import_module(sys.argv[1])
module_file = Path(module.__file__).resolve()
source = module_file.parent if module_file.name != "__init__.py" else module_file.parent
distribution = importlib.metadata.distribution(sys.argv[2])
distribution_root = Path(distribution.locate_file("")).resolve()
digest = hashlib.sha256()
count = 0
for path in sorted(source.rglob("*"), key=lambda item: item.as_posix()):
    if not path.is_file() or path.suffix not in {".py", ".so", ".pyd", ".dll", ".dylib"}:
        continue
    data = path.read_bytes()
    digest.update(path.relative_to(source).as_posix().encode())
    digest.update(b"\\0")
    digest.update(hashlib.sha256(data).digest())
    count += 1
print(json.dumps({
    "version": distribution.version,
    "package_sha256": digest.hexdigest(),
    "package_files": count,
    "source_matches_distribution": module_file.is_relative_to(distribution_root),
}, sort_keys=True))
"""
    completed = subprocess.run(
        (sys.executable, "-c", probe, module_name, distribution_name),
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"cannot identify the {distribution_name} runtime used by benchmark commands\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    try:
        identity = json.loads(completed.stdout.splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"{distribution_name} runtime identity probe returned invalid output"
        ) from error
    valid = (
        isinstance(identity, dict)
        and isinstance(identity.get("version"), str)
        and isinstance(identity.get("package_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", identity["package_sha256"]) is not None
        and isinstance(identity.get("package_files"), int)
        and identity["package_files"] > 0
        and isinstance(identity.get("source_matches_distribution"), bool)
    )
    if not valid:
        raise RuntimeError(f"{distribution_name} runtime identity probe returned an invalid schema")
    return identity


def _testenix_runtime_identity(project: Path, environment: dict[str, str]) -> dict[str, Any]:
    return _package_runtime_identity(
        project,
        environment,
        module_name="testenix",
        distribution_name="testenix",
    )


def _pytest_runtime_identity(project: Path, environment: dict[str, str]) -> dict[str, Any]:
    return _package_runtime_identity(
        project,
        environment,
        module_name="pytest",
        distribution_name="pytest",
    )


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _observed_testenix_workers(output: str) -> int | None:
    plain = _ANSI_ESCAPE.sub("", output)
    match = re.search(r"(?m)^Testenix\s+\|.*\|\s+(\d+)\s+workers?\s*$", plain)
    return int(match.group(1)) if match is not None else None


def _validate_output(
    runner: Runner,
    completed: subprocess.CompletedProcess[str],
    *,
    expected_tests: int,
    expected_passed: int,
) -> None:
    if completed.returncode != 0:
        raise RuntimeError(
            f"runner {runner.name} failed with exit {completed.returncode}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    combined = _ANSI_ESCAPE.sub("", f"{completed.stdout}\n{completed.stderr}")
    if runner.kind == "pytest":
        summary_lines = [
            line for line in combined.splitlines() if re.search(r"\bin\s+\d+(?:\.\d+)?s\b", line)
        ]
        counts = {
            label: int(count)
            for count, label in re.findall(
                r"(?<!\d)(\d+)\s+(passed|skipped|xfailed|xpassed)\b",
                summary_lines[-1] if summary_lines else "",
            )
        }
        actual_total = sum(counts.values())
        valid = actual_total == expected_tests and counts.get("passed", 0) == expected_passed
    else:
        total = re.search(rf"(?m)^\s*{expected_tests}\s+tests(?:,|\s)", combined) is not None
        passed = (
            re.search(rf"(?:^|,\s){expected_passed}\s+passed(?:,|\s+in\s)", combined) is not None
        )
        valid = total and passed
    if not valid:
        raise RuntimeError(
            f"runner {runner.name} did not report {expected_tests} tests / "
            f"{expected_passed} passed\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )


def _run_once(
    runner: Runner,
    *,
    project: Path,
    environment: dict[str, str],
    expected_tests: int,
    expected_passed: int,
    timeout: float,
) -> Sample:
    started = time.perf_counter()
    completed = subprocess.run(
        runner.command,
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    elapsed = time.perf_counter() - started
    _validate_output(
        runner,
        completed,
        expected_tests=expected_tests,
        expected_passed=expected_passed,
    )
    observed_workers = (
        _observed_testenix_workers(f"{completed.stdout}\n{completed.stderr}")
        if runner.kind == "testenix"
        else None
    )
    if runner.kind == "testenix" and observed_workers is None:
        raise RuntimeError(
            f"runner {runner.name} did not expose the actual Testenix worker count; "
            "refusing to record ambiguous benchmark evidence"
        )
    return Sample(
        elapsed=elapsed,
        stdout_bytes=len(completed.stdout.encode("utf-8")),
        stderr_bytes=len(completed.stderr.encode("utf-8")),
        observed_workers=observed_workers,
    )


def _measure(
    runners: tuple[Runner, ...],
    *,
    project: Path,
    environment: dict[str, str],
    expected_tests: int,
    expected_passed: int,
    warmups: int,
    repeats: int,
    timeout: float,
) -> tuple[dict[str, list[Sample]], list[list[str]]]:
    for round_index in range(warmups):
        shift = round_index % len(runners)
        order = (*runners[shift:], *runners[:shift])
        for runner in order:
            _run_once(
                runner,
                project=project,
                environment=environment,
                expected_tests=expected_tests,
                expected_passed=expected_passed,
                timeout=timeout,
            )

    samples: dict[str, list[Sample]] = {runner.name: [] for runner in runners}
    orders: list[list[str]] = []
    for round_index in range(repeats):
        shift = round_index % len(runners)
        order = (*runners[shift:], *runners[:shift])
        orders.append([runner.name for runner in order])
        for runner in order:
            samples[runner.name].append(
                _run_once(
                    runner,
                    project=project,
                    environment=environment,
                    expected_tests=expected_tests,
                    expected_passed=expected_passed,
                    timeout=timeout,
                )
            )
    return samples, orders


def _display_argv(runner: Runner, project: Path) -> tuple[str, ...]:
    values: list[str] = []
    for index, argument in enumerate(runner.command):
        if index in runner.redact_arguments:
            values.append("<redacted>")
            continue
        if argument == sys.executable:
            values.append("python")
        else:
            values.append(argument.replace(str(project), "<project>"))
    return tuple(values)


def _display_command(runner: Runner, project: Path) -> str:
    return shlex.join(_display_argv(runner, project))


def _option_value(command: tuple[str, ...], *names: str) -> str | None:
    for index, argument in enumerate(command):
        if argument in names:
            return command[index + 1] if index + 1 < len(command) else None
        for name in names:
            prefix = f"{name}="
            if argument.startswith(prefix):
                return argument[len(prefix) :]
    return None


def _runner_contract(runner: Runner) -> dict[str, Any]:
    if runner.kind == "testenix":
        history_path = _option_value(runner.command, "--history")
        return {
            "workers_requested": _option_value(runner.command, "--workers", "-w")
            or "configuration",
            "history_mode": (
                "disabled"
                if "--no-history" in runner.command
                else (f"explicit:{history_path}" if history_path is not None else "configuration")
            ),
            "safe_module_sharding": "--shard-modules" in runner.command,
        }
    return {
        "workers_requested": _option_value(runner.command, "-n", "--numprocesses"),
        "history_mode": None,
        "safe_module_sharding": None,
    }


def _measurement(
    samples: list[Sample], runner: Runner, project: Path, expected_tests: int
) -> dict[str, Any]:
    durations = [sample.elapsed for sample in samples]
    return {
        "command": _display_command(runner, project),
        "argv": _display_argv(runner, project),
        "contract": _runner_contract(runner),
        "samples_seconds": durations,
        "median_seconds": statistics.median(durations),
        "minimum_seconds": min(durations),
        "maximum_seconds": max(durations),
        "mean_seconds": statistics.fmean(durations),
        "stdev_seconds": statistics.stdev(durations) if len(durations) > 1 else 0.0,
        "median_throughput_tests_per_second": expected_tests / statistics.median(durations),
        "stdout_bytes": [sample.stdout_bytes for sample in samples],
        "stderr_bytes": [sample.stderr_bytes for sample in samples],
        "observed_workers": [sample.observed_workers for sample in samples],
    }


def _git_value(project: Path, *arguments: str) -> str | None:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _git_provenance(project: Path, *, allow_dirty: bool, include_commit: bool) -> dict[str, Any]:
    status = _git_value(project, "status", "--porcelain")
    commit = _git_value(project, "rev-parse", "HEAD")
    if status is None or commit is None:
        raise RuntimeError(f"benchmark project is not a readable Git checkout: {project}")
    dirty = bool(status)
    if dirty and not allow_dirty:
        raise RuntimeError(
            f"refusing to benchmark dirty checkout {project}; clean it or use the explicit "
            "--allow-dirty-project smoke-only override"
        )
    commit_value = commit if include_commit else hashlib.sha256(commit.encode()).hexdigest()
    return {
        "dirty": dirty,
        "commit" if include_commit else "commit_sha256": commit_value,
        "state_sha256": hashlib.sha256(f"{commit}\0{status}".encode()).hexdigest(),
    }


def _environment_distribution_metadata(
    project: Path,
    environment: dict[str, str],
) -> dict[str, Any]:
    """Fingerprint packages visible to the exact benchmark interpreter/environment."""

    probe = """
import hashlib
import importlib.metadata
import json

distributions = set()
versions = {}
for distribution in importlib.metadata.distributions():
    name = distribution.metadata.get("Name")
    if not name:
        continue
    canonical = name.casefold()
    distributions.add(f"{canonical}=={distribution.version}")
    if canonical in {"pytest", "pytest-xdist", "testenix"}:
        versions[canonical] = distribution.version
serialized = "\\n".join(sorted(distributions)).encode("utf-8")
print(json.dumps({
    "count": len(distributions),
    "sha256": hashlib.sha256(serialized).hexdigest(),
    "versions": versions,
}, sort_keys=True))
"""
    completed = subprocess.run(
        (sys.executable, "-c", probe),
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "cannot fingerprint distributions in the benchmark environment\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    try:
        value = json.loads(completed.stdout.splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as error:
        raise RuntimeError("distribution fingerprint probe returned invalid output") from error
    valid = (
        isinstance(value, dict)
        and isinstance(value.get("count"), int)
        and value["count"] > 0
        and isinstance(value.get("sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is not None
        and isinstance(value.get("versions"), dict)
        and all(
            isinstance(name, str) and isinstance(version, str)
            for name, version in value["versions"].items()
        )
    )
    if not valid:
        raise RuntimeError("distribution fingerprint probe returned an invalid schema")
    return value


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


def _tree_fingerprint(project: Path, relative: str) -> dict[str, Any]:
    root = (project / relative).resolve()
    try:
        root.relative_to(project.resolve())
    except ValueError as error:
        raise RuntimeError(f"fingerprint path escapes the project: {relative}") from error
    if not root.exists():
        raise RuntimeError(f"fingerprint path does not exist: {relative}")
    files = (
        (root,) if root.is_file() else tuple(path for path in root.rglob("*.py") if path.is_file())
    )
    if not files:
        raise RuntimeError(f"fingerprint path contains no Python files: {relative}")
    digest = hashlib.sha256()
    total_bytes = 0
    for path in sorted(files, key=lambda item: item.as_posix()):
        data = path.read_bytes()
        total_bytes += len(data)
        digest.update(path.relative_to(project).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(data).digest())
    return {"sha256": digest.hexdigest(), "files": len(files), "bytes": total_bytes}


def _fingerprints(manifest: dict[str, Any], project: Path) -> dict[str, dict[str, Any]]:
    raw = manifest.get("fingerprints", {})
    if not isinstance(raw, dict) or any(
        not isinstance(name, str) or not isinstance(path, str) for name, path in raw.items()
    ):
        raise RuntimeError("manifest fingerprints must map public labels to relative paths")
    return {name: _tree_fingerprint(project, path) for name, path in sorted(raw.items())}


def _project_relative_path(project: Path, value: Any, *, name: str) -> Path:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise RuntimeError(f"migration report {name} must be a non-empty relative path")
    path = (project / value).resolve()
    try:
        path.relative_to(project.resolve())
    except ValueError as error:
        raise RuntimeError(f"migration report {name} escapes the project") from error
    return path


def _explicit_suite_targets(runner: Runner, project: Path) -> tuple[Path, ...]:
    """Return unambiguous positional suite roots after the CLI ``--`` delimiter."""

    delimiters = [index for index, argument in enumerate(runner.command) if argument == "--"]
    if len(delimiters) != 1:
        raise RuntimeError(
            f"runner {runner.name}: publication commands must separate suite targets with "
            "exactly one '--' delimiter"
        )
    raw_targets = runner.command[delimiters[0] + 1 :]
    if not raw_targets or any(target.startswith("-") for target in raw_targets):
        raise RuntimeError(
            f"runner {runner.name}: publication command has no unambiguous suite targets after '--'"
        )
    targets = tuple(
        (Path(target) if Path(target).is_absolute() else project / target).resolve()
        for target in raw_targets
    )
    if len(set(targets)) != len(targets):
        raise RuntimeError(f"runner {runner.name}: publication suite targets must be unique")
    return targets


def _summary_outcomes(
    summary: Any,
    *,
    label: str,
    expected_ids: set[str],
    expected_tests: int,
    expected_passed: int,
) -> dict[str, str]:
    if not isinstance(summary, dict):
        raise RuntimeError(f"migration report {label} summary is missing")
    outcomes = summary.get("outcomes")
    if not isinstance(outcomes, dict) or any(
        not isinstance(test_id, str) or not isinstance(status, str)
        for test_id, status in outcomes.items()
    ):
        raise RuntimeError(f"migration report {label} outcomes must map test IDs to statuses")
    if set(outcomes) != expected_ids:
        raise RuntimeError(
            f"migration report {label} per-test inventory does not match converter mappings"
        )
    if summary.get("tests") != expected_tests or summary.get("passed") != expected_passed:
        raise RuntimeError(
            f"migration report {label} summary does not match the benchmark expected counts"
        )
    return outcomes


def _python_inventory(root: Path, *, relative_to: Path) -> set[str]:
    files = (root,) if root.is_file() else tuple(root.rglob("*.py"))
    inventory: set[str] = set()
    for path in files:
        if not path.is_file() or path.suffix != ".py":
            continue
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(relative_to.resolve())
        except ValueError as error:
            raise RuntimeError(
                f"Python inventory path escapes its benchmark root: {path}"
            ) from error
        inventory.add(relative.as_posix())
    return inventory


def _migration_gate(
    manifest: dict[str, Any],
    project: Path,
    expected_tests: int,
    expected_passed: int,
    runners: tuple[Runner, ...],
) -> dict[str, Any] | None:
    relative = manifest.get("migration_report")
    if relative is None:
        return None
    path = _project_relative_path(project, relative, name="migration_report")
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot read migration report: {error}") from error
    if not isinstance(report, dict):
        raise RuntimeError("migration report must be a JSON object")
    valid_header = (
        report.get("format") == "testenix.migration-report"
        and report.get("schema_version") == 1
        and report.get("framework") == "pytest"
        and report.get("status") == "published"
        and report.get("published") is True
        and report.get("converted_tests") == expected_tests
        and report.get("originals_modified") is False
    )
    if not valid_header:
        raise RuntimeError("migration report did not pass publication/integrity gates")

    mappings = report.get("mappings")
    if not isinstance(mappings, list) or len(mappings) != expected_tests:
        raise RuntimeError("migration report mappings do not match expected_tests")
    try:
        source_ids = [mapping["source_id"] for mapping in mappings]
        target_files = [mapping["target_file"] for mapping in mappings]
    except (KeyError, TypeError) as error:
        raise RuntimeError("migration report mappings have an invalid schema") from error
    if (
        any(not isinstance(test_id, str) or not test_id for test_id in source_ids)
        or len(set(source_ids)) != expected_tests
        or any(not isinstance(target, str) or not target for target in target_files)
    ):
        raise RuntimeError("migration report mappings must contain unique source test IDs")
    expected_ids = set(source_ids)
    baseline_outcomes = _summary_outcomes(
        report.get("baseline"),
        label="baseline",
        expected_ids=expected_ids,
        expected_tests=expected_tests,
        expected_passed=expected_passed,
    )
    serial_outcomes = _summary_outcomes(
        report.get("native_serial"),
        label="native_serial",
        expected_ids=expected_ids,
        expected_tests=expected_tests,
        expected_passed=expected_passed,
    )
    parallel_outcomes = _summary_outcomes(
        report.get("native_parallel"),
        label="native_parallel",
        expected_ids=expected_ids,
        expected_tests=expected_tests,
        expected_passed=expected_passed,
    )
    if baseline_outcomes != serial_outcomes or baseline_outcomes != parallel_outcomes:
        raise RuntimeError("migration report per-test outcomes are not equivalent")

    raw_sources = report.get("sources")
    output = report.get("output")
    if (
        not isinstance(raw_sources, list)
        or not raw_sources
        or any(not isinstance(source, str) or not source for source in raw_sources)
        or not isinstance(output, str)
        or not output
    ):
        raise RuntimeError("migration report sources/output have an invalid schema")
    source_roots: list[Path] = []
    for source in raw_sources:
        source_root = _project_relative_path(project, source, name="sources[]")
        if not source_root.is_dir():
            raise RuntimeError(
                "publishable migration benchmarks require directory source roots so pytest "
                "support files such as conftest.py are part of the verified inventory"
            )
        source_roots.append(source_root)
    output_path = _project_relative_path(project, output, name="output")
    source_targets = {(project / source).resolve() for source in raw_sources}
    native_targets = {output_path}
    for runner in runners:
        expected_targets = source_targets if runner.kind == "pytest" else native_targets
        if set(_explicit_suite_targets(runner, project)) != expected_targets:
            raise RuntimeError(
                "benchmark runner paths do not match the migration report source/output roots"
            )

    source_hashes = report.get("source_hashes")
    if not isinstance(source_hashes, dict) or not source_hashes:
        raise RuntimeError("migration report source_hashes are missing")
    declared_source_inventory: set[str] = set()
    for source, expected_sha256 in source_hashes.items():
        source_path = _project_relative_path(project, source, name="source_hashes key")
        declared_source_inventory.add(source_path.relative_to(project.resolve()).as_posix())
        if (
            not isinstance(expected_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
            or not source_path.is_file()
            or hashlib.sha256(source_path.read_bytes()).hexdigest() != expected_sha256
        ):
            raise RuntimeError(f"migration report source hash is stale for {source!r}")
    actual_source_inventory = set().union(
        *(_python_inventory(root, relative_to=project) for root in source_roots)
    )
    if actual_source_inventory != declared_source_inventory:
        raise RuntimeError(
            "migration report source Python inventory is stale or incomplete; regenerate migration"
        )

    generated_files = report.get("generated_files")
    if (
        not isinstance(generated_files, list)
        or not generated_files
        or any(not isinstance(generated, str) or not generated for generated in generated_files)
    ):
        raise RuntimeError("migration report generated_files are missing")
    generated_digest = hashlib.sha256()
    generated_paths: dict[str, Path] = {}
    for generated in sorted(generated_files):
        generated_path = _project_relative_path(
            output_path,
            generated,
            name="generated_files[]",
        )
        if not generated_path.is_file():
            raise RuntimeError(f"migration report generated file is missing: {generated!r}")
        generated_paths[generated] = generated_path
        generated_digest.update(generated.encode("utf-8"))
        generated_digest.update(b"\0")
        generated_digest.update(hashlib.sha256(generated_path.read_bytes()).digest())
    if any(target not in generated_paths for target in target_files):
        raise RuntimeError("migration mappings reference files outside generated_files")
    actual_generated_inventory = _python_inventory(output_path, relative_to=output_path)
    if actual_generated_inventory != set(generated_files):
        raise RuntimeError(
            "migration report generated Python inventory is stale or incomplete; "
            "regenerate migration"
        )

    serialized_outcomes = json.dumps(
        sorted(baseline_outcomes.items()), separators=(",", ":")
    ).encode("utf-8")
    return {
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "status": report["status"],
        "converted_tests": report["converted_tests"],
        "originals_modified": report["originals_modified"],
        "inventory_sha256": hashlib.sha256(
            "\n".join(sorted(source_ids)).encode("utf-8")
        ).hexdigest(),
        "outcomes_sha256": hashlib.sha256(serialized_outcomes).hexdigest(),
        "source_files_verified": len(source_hashes),
        "generated_files_verified": len(generated_files),
        "generated_files_sha256": generated_digest.hexdigest(),
        "runner_paths_verified": True,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-dirty-project", action="store_true")
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    project = arguments.project.resolve()
    if not project.is_dir():
        raise RuntimeError(f"benchmark project is not a directory: {project}")
    manifest_path = arguments.manifest.resolve()
    manifest = _read_manifest(manifest_path)
    expected_tests = _positive_integer(manifest.get("expected_tests"), "expected_tests")
    expected_passed = _positive_integer(
        manifest.get("expected_passed", expected_tests), "expected_passed"
    )
    if expected_passed > expected_tests:
        raise RuntimeError("expected_passed cannot exceed expected_tests")
    warmups = _non_negative_integer(manifest.get("warmups", 1), "warmups")
    repeats = _positive_integer(manifest.get("repeats", 5), "repeats")
    raw_timeout = manifest.get("timeout_seconds", 900.0)
    if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
        raise RuntimeError("timeout_seconds must be a number")
    timeout = float(raw_timeout)
    if not 0 < timeout <= 3600:
        raise RuntimeError("timeout_seconds must be greater than zero and at most 3600")

    expected_version = manifest.get("testenix_version")
    if not isinstance(expected_version, str) or not expected_version:
        raise RuntimeError("manifest testenix_version must be a non-empty string")
    runners = _runners(manifest)
    runner_contracts = {runner.name: _runner_contract(runner) for runner in runners}
    explicit_testenix_contracts = all(
        contract["workers_requested"] != "configuration"
        and contract["history_mode"] != "configuration"
        for runner in runners
        if runner.kind == "testenix"
        for contract in (runner_contracts[runner.name],)
    )
    environment, environment_override_keys, environment_sha256 = _environment(manifest)
    testenix_runtime = _testenix_runtime_identity(project, environment)
    pytest_runtime = _pytest_runtime_identity(project, environment)
    distribution_metadata = _environment_distribution_metadata(project, environment)
    installed_version = testenix_runtime["version"]
    if expected_version != installed_version:
        raise RuntimeError(
            f"manifest requires Testenix {expected_version!r}, executed version is "
            f"{installed_version!r}"
        )
    project_provenance = _git_provenance(
        project,
        allow_dirty=arguments.allow_dirty_project,
        include_commit=manifest.get("include_project_commit") is True,
    )
    fingerprints = _fingerprints(manifest, project)
    migration_gate = _migration_gate(
        manifest,
        project,
        expected_tests,
        expected_passed,
        runners,
    )
    samples, orders = _measure(
        runners,
        project=project,
        environment=environment,
        expected_tests=expected_tests,
        expected_passed=expected_passed,
        warmups=warmups,
        repeats=repeats,
        timeout=timeout,
    )
    final_project_provenance = _git_provenance(
        project,
        allow_dirty=arguments.allow_dirty_project,
        include_commit=manifest.get("include_project_commit") is True,
    )
    final_fingerprints = _fingerprints(manifest, project)
    final_migration_gate = _migration_gate(
        manifest,
        project,
        expected_tests,
        expected_passed,
        runners,
    )
    final_testenix_runtime = _testenix_runtime_identity(project, environment)
    final_pytest_runtime = _pytest_runtime_identity(project, environment)
    final_distribution_metadata = _environment_distribution_metadata(project, environment)
    if (
        final_project_provenance != project_provenance
        or final_fingerprints != fingerprints
        or final_migration_gate != migration_gate
        or final_testenix_runtime != testenix_runtime
        or final_pytest_runtime != pytest_runtime
        or final_distribution_metadata != distribution_metadata
    ):
        raise RuntimeError(
            "benchmark project changed during execution; discard this run and restore a stable "
            "checkout before measuring again"
        )

    project_name = manifest.get("name", "private-project")
    if not isinstance(project_name, str) or not project_name.strip():
        raise RuntimeError("manifest name must be a non-empty string")
    publication_eligible = (
        not project_provenance["dirty"]
        and repeats >= 5
        and warmups >= 1
        and explicit_testenix_contracts
        and testenix_runtime["source_matches_distribution"]
        and pytest_runtime["source_matches_distribution"]
        and migration_gate is not None
    )

    result = {
        "schema_version": 1,
        "kind": "testenix.real-project-benchmark",
        "recorded_at": datetime.now(UTC).isoformat(),
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "project": {
            "name": project_name,
            "provenance": project_provenance,
            "fingerprints": fingerprints,
            "migration_gate": migration_gate,
        },
        "environment": {
            "cpu_count": os.cpu_count(),
            "cpu_model": _cpu_model(),
            "machine": platform.machine(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "testenix": installed_version,
            "testenix_runtime": testenix_runtime,
            "pytest_runtime": pytest_runtime,
            "versions": {
                "pytest": pytest_runtime["version"],
                "pytest_xdist": distribution_metadata["versions"].get(
                    "pytest-xdist", "not-installed"
                ),
                "testenix": installed_version,
            },
            "distributions": {
                "count": distribution_metadata["count"],
                "sha256": distribution_metadata["sha256"],
            },
            "override_keys": environment_override_keys,
            "environment_sha256": environment_sha256,
        },
        "scenario": {
            "expected_tests": expected_tests,
            "expected_passed": expected_passed,
            "warmups": warmups,
            "repeats": repeats,
            "measured_execution_orders": orders,
            "timeout_seconds": timeout,
            "publication_eligible": publication_eligible,
            "publication_contract": (
                "clean project, >=5 measured rounds, >=1 warm-up, explicit Testenix workers "
                "and history mode, canonical module entrypoints, installed-distribution runtimes, "
                "and a current published migration report with exact per-test inventory/outcome "
                "parity and runner-path binding"
            ),
            "runner_contracts": runner_contracts,
        },
        "measurements": {
            runner.name: _measurement(
                samples[runner.name],
                runner,
                project,
                expected_tests,
            )
            for runner in runners
        },
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
