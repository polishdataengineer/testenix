"""Adaptive worker selection and reproducible project-local tuning.

The fast path in :func:`resolve_adaptive_workers` is a pure cost model.  It
uses the execution units that Testenix can actually schedule (normal modules
and individually isolated timeout tests), historical test durations, and a
conservative process-startup estimate.  The slower :func:`run_tuning` service
executes an explicit benchmark when a project wants an evidence-based fixed
worker count.
"""

from __future__ import annotations

import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence, Sized
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from testenix.contracts import CollectionIssue, RunResult, Status, TestResult, TestSpec

if TYPE_CHECKING:
    from testenix.config import TestenixConfig

SpawnMethod = Literal["fork", "forkserver", "spawn"]
NativeMeasure = Callable[[Sequence[str], "TestenixConfig"], tuple[float, RunResult]]
PytestMeasure = Callable[[Sequence[str]], tuple[float, int]]

_MARGINAL_SPAWN_FRACTION = 0.25
_NEAR_BEST_FRACTION = 0.02
_NEAR_BEST_SECONDS = 0.005
_COLD_START_WORKERS = 4
_MINIMUM_HISTORY_COVERAGE = 0.5


class TuningError(RuntimeError):
    """Raised when a tuning sample cannot produce a trustworthy recommendation."""


@dataclass(frozen=True, slots=True)
class ExecutionUnitEstimate:
    """One independently schedulable native execution unit."""

    key: str
    duration: float
    tests: int
    isolated: bool = False


@dataclass(frozen=True, slots=True)
class WorkerEstimate:
    """Predicted makespan for one candidate worker count."""

    workers: int
    workload_seconds: float
    startup_seconds: float

    @property
    def total_seconds(self) -> float:
        return self.workload_seconds + self.startup_seconds


@dataclass(frozen=True, slots=True)
class TuningCandidate:
    """Measured timings for one explicit worker count."""

    workers: int
    samples: tuple[float, ...]

    @property
    def median(self) -> float:
        return float(statistics.median(self.samples))

    @property
    def minimum(self) -> float:
        return min(self.samples)

    @property
    def maximum(self) -> float:
        return max(self.samples)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workers": self.workers,
            "samples": list(self.samples),
            "median": self.median,
            "min": self.minimum,
            "max": self.maximum,
        }


@dataclass(frozen=True, slots=True)
class TuningReport:
    """Complete native tuning result with an optional pytest comparison."""

    paths: tuple[str, ...]
    warmups: int
    repeats: int
    discovered_tests: int
    execution_units: int
    model_recommendation: int
    recommended_workers: int
    candidates: tuple[TuningCandidate, ...]
    pytest_paths: tuple[str, ...] = ()
    pytest_samples: tuple[float, ...] = ()
    shard_modules: bool = False
    manifest_used: bool = False

    @property
    def pytest_median(self) -> float | None:
        if not self.pytest_samples:
            return None
        return float(statistics.median(self.pytest_samples))

    @property
    def native_median(self) -> float:
        for candidate in self.candidates:
            if candidate.workers == self.recommended_workers:
                return candidate.median
        raise TuningError("recommended worker count has no measured candidate")

    @property
    def pytest_over_native(self) -> float | None:
        pytest_median = self.pytest_median
        if pytest_median is None or self.native_median <= 0.0:
            return None
        return pytest_median / self.native_median

    def to_dict(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "schema": "testenix.tuning-report",
            "schema_version": 1,
            "paths": list(self.paths),
            "warmups": self.warmups,
            "repeats": self.repeats,
            "discovered_tests": self.discovered_tests,
            "execution_units": self.execution_units,
            "model_recommendation": self.model_recommendation,
            "recommended_workers": self.recommended_workers,
            "execution": {
                "fresh_process_wall_clock": True,
                "history": "disabled",
                "manifest": self.manifest_used,
                "shard_modules": self.shard_modules,
            },
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }
        if self.pytest_paths:
            document["pytest"] = {
                "paths": list(self.pytest_paths),
                "samples": list(self.pytest_samples),
                "median": self.pytest_median,
                "pytest_over_native": self.pytest_over_native,
                "inventory_equivalence_verified": False,
            }
        return document

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"


def execution_units(
    specs: Sequence[TestSpec],
    durations: Mapping[str, float],
    *,
    shardable_paths: AbstractSet[str] = frozenset(),
) -> tuple[ExecutionUnitEstimate, ...]:
    """Build the same module/timeout affinity units as the native scheduler."""

    selected_ids = {spec.id for spec in specs}
    known = tuple(
        float(value)
        for test_id, value in durations.items()
        if test_id in selected_ids
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) >= 0.0
    )
    fallback = float(statistics.median(known)) if known else 1.0
    modules: dict[str, list[TestSpec]] = {}
    isolated: list[TestSpec] = []
    sharded: list[TestSpec] = []
    for spec in specs:
        if spec.timeout is not None:
            isolated.append(spec)
        elif spec.path in shardable_paths:
            sharded.append(spec)
        else:
            modules.setdefault(spec.path, []).append(spec)

    units: list[ExecutionUnitEstimate] = []
    for path, module_specs in sorted(modules.items()):
        duration = sum(_duration_for(spec.id, durations, fallback) for spec in module_specs)
        units.append(
            ExecutionUnitEstimate(
                key=f"module:{path}",
                duration=duration,
                tests=len(module_specs),
            )
        )
    for spec in sorted(isolated, key=lambda item: item.id):
        units.append(
            ExecutionUnitEstimate(
                key=f"isolated:{spec.id}",
                duration=_duration_for(spec.id, durations, fallback),
                tests=1,
                isolated=True,
            )
        )
    for spec in sorted(sharded, key=lambda item: item.id):
        units.append(
            ExecutionUnitEstimate(
                key=f"sharded:{spec.id}",
                duration=_duration_for(spec.id, durations, fallback),
                tests=1,
            )
        )
    return tuple(units)


def estimate_spawn_cost(
    spawn_method: SpawnMethod = "spawn",
    *,
    platform: str | None = None,
) -> float:
    """Return a conservative per-run process startup estimate in seconds."""

    if spawn_method == "fork":
        return 0.010
    if spawn_method == "forkserver":
        return 0.030
    effective_platform = sys.platform if platform is None else platform
    if effective_platform.startswith("win"):
        return 0.120
    if effective_platform == "darwin":
        return 0.080
    return 0.060


def worker_estimates(
    units: Sequence[ExecutionUnitEstimate],
    *,
    maximum_workers: int,
    spawn_cost: float,
) -> tuple[WorkerEstimate, ...]:
    """Predict every feasible worker count using deterministic LPT placement."""

    if maximum_workers < 1:
        raise ValueError("maximum_workers must be at least 1")
    if not math.isfinite(spawn_cost) or spawn_cost < 0.0:
        raise ValueError("spawn_cost must be a finite non-negative number")
    if not units:
        return (WorkerEstimate(1, 0.0, spawn_cost),)

    limit = min(maximum_workers, len(units))
    ordered = sorted(units, key=lambda unit: (-unit.duration, unit.key))
    estimates: list[WorkerEstimate] = []
    for workers in range(1, limit + 1):
        loads = [0.0] * workers
        item_counts = [0] * workers
        for unit in ordered:
            shard = min(
                range(workers),
                key=lambda index: (loads[index], item_counts[index], index),
            )
            loads[shard] += unit.duration
            item_counts[shard] += 1
        startup = spawn_cost * (1.0 + _MARGINAL_SPAWN_FRACTION * (workers - 1))
        estimates.append(WorkerEstimate(workers, max(loads), startup))
    return tuple(estimates)


def resolve_adaptive_workers(
    config: TestenixConfig,
    selected_specs: Sequence[TestSpec],
    durations: Mapping[str, float],
    *,
    spawn_method: SpawnMethod = "spawn",
    spawn_cost: float | None = None,
    cpu_count: int | None = None,
    shardable_paths: AbstractSet[str] = frozenset(),
) -> int:
    """Resolve ``workers = "auto"`` from schedulable work and local costs.

    Explicit integer configuration is returned unchanged.  Auto mode is capped
    by both CPU capacity and the number of independently schedulable units, then
    chooses the smallest count within a narrow tolerance of the predicted best
    makespan.  That bias avoids starting extra processes for negligible gains.
    """

    if config.workers != "auto":
        return config.workers
    units = execution_units(selected_specs, durations, shardable_paths=shardable_paths)
    if not units:
        return 1
    capacity = _schedulable_cpu_capacity(cpu_count)
    maximum = min(capacity, len(units))
    if not _has_reliable_durations(
        selected_specs,
        durations,
        shardable_paths=shardable_paths,
    ):
        # Unknown tests are not one-second measurements.  Modelling them as
        # such makes tiny/no-op suites look massively parallel and recreates
        # the CPU-count oversubscription that adaptive mode is meant to avoid.
        return min(_COLD_START_WORKERS, maximum)
    cost = estimate_spawn_cost(spawn_method) if spawn_cost is None else spawn_cost
    estimates = worker_estimates(
        units,
        maximum_workers=maximum,
        spawn_cost=cost,
    )
    best = min(estimate.total_seconds for estimate in estimates)
    tolerance = max(_NEAR_BEST_SECONDS, best * _NEAR_BEST_FRACTION)
    return min(
        estimate.workers for estimate in estimates if estimate.total_seconds <= best + tolerance
    )


def default_worker_candidates(
    unit_count: int,
    *,
    model_recommendation: int,
    cpu_count: int | None = None,
) -> tuple[int, ...]:
    """Return a conservative automatic sweep within the cold-start ceiling.

    Automatic tuning must not turn a large host CPU count into an experiment
    that launches hundreds of processes.  The adaptive model recommendation is
    retained when it is inside the conservative ceiling.  Users who have
    evidence that a larger process count is useful can still request it
    explicitly with ``--candidates``.
    """

    if unit_count < 1:
        return (1,)
    capacity = _schedulable_cpu_capacity(cpu_count)
    limit = min(unit_count, capacity, _COLD_START_WORKERS)
    recommendation = min(limit, max(1, model_recommendation))
    candidates = {1, recommendation, limit}
    workers = 2
    while workers < limit:
        candidates.add(workers)
        workers *= 2
    return tuple(sorted(candidates))


def _schedulable_cpu_capacity(cpu_count: int | None = None) -> int:
    """Return the process-visible CPU capacity, not only the host total."""

    if cpu_count is not None:
        return max(1, cpu_count)

    detected: list[int] = []
    process_cpu_count = getattr(os, "process_cpu_count", None)
    if callable(process_cpu_count):
        try:
            value = process_cpu_count()
        except OSError:
            value = None
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            detected.append(value)

    sched_getaffinity = getattr(os, "sched_getaffinity", None)
    if callable(sched_getaffinity):
        try:
            affinity = sched_getaffinity(0)
            affinity_count = len(affinity) if isinstance(affinity, Sized) else 0
        except OSError:
            affinity_count = 0
        if affinity_count > 0:
            detected.append(affinity_count)

    host_count = os.cpu_count()
    if host_count is not None and host_count > 0:
        detected.append(host_count)
    return min(detected, default=1)


def run_tuning(
    paths: Sequence[str],
    config: TestenixConfig,
    *,
    candidates: Sequence[int] | None = None,
    warmups: int = 1,
    repeats: int = 5,
    pytest_paths: Sequence[str] = (),
    native_measure: NativeMeasure | None = None,
    pytest_measure: PytestMeasure | None = None,
) -> TuningReport:
    """Measure native worker counts without mutating Testenix history.

    A one-worker probe establishes the inventory and supplies fresh duration
    estimates to the adaptive model.  Candidate order reverses on alternating
    rounds to reduce monotonic thermal/load drift.  Every measured native run
    must preserve the probe inventory and outcomes.
    """

    if warmups < 0:
        raise ValueError("warmups must be at least 0")
    if repeats < 1:
        raise ValueError("repeats must be at least 1")
    effective_paths = tuple(paths) if paths else config.paths
    measure_native = _measure_native if native_measure is None else native_measure
    base = config.with_overrides(history_path=None, json_path=None, junit_path=None)

    _, probe = measure_native(effective_paths, base.with_overrides(workers=1))
    _require_green(probe, "one-worker probe")
    specs = tuple(result.test for result in probe.tests)
    durations = {result.test.id: result.duration for result in probe.tests}
    model_config = base.with_overrides(workers="auto")
    shardable_paths = frozenset(probe.shardable_paths) if config.shard_modules else frozenset()
    model_recommendation = resolve_adaptive_workers(
        model_config,
        specs,
        durations,
        shardable_paths=shardable_paths,
    )
    units = execution_units(specs, durations, shardable_paths=shardable_paths)

    if candidates is None:
        selected_candidates = default_worker_candidates(
            len(units),
            model_recommendation=model_recommendation,
        )
    else:
        requested_candidates = _normalise_candidates(candidates)
        unit_limit = max(1, len(units))
        selected_candidates = tuple(
            sorted({min(workers, unit_limit) for workers in requested_candidates})
        )
    signature = _result_signature(probe)

    effective_pytest_paths = tuple(pytest_paths)
    measure_pytest = _measure_pytest if pytest_measure is None else pytest_measure
    pytest_samples: list[float] = []

    for warmup in range(warmups):
        if effective_pytest_paths and warmup % 2:
            elapsed, return_code = measure_pytest(effective_pytest_paths)
            _validate_pytest_sample(elapsed, return_code, label="warmup")
        for workers in selected_candidates:
            _, result = measure_native(effective_paths, base.with_overrides(workers=workers))
            _require_matching(result, signature, f"warmup with {workers} workers")
        if effective_pytest_paths and warmup % 2 == 0:
            elapsed, return_code = measure_pytest(effective_pytest_paths)
            _validate_pytest_sample(elapsed, return_code, label="warmup")

    samples: dict[int, list[float]] = {workers: [] for workers in selected_candidates}
    for repeat in range(repeats):
        order = selected_candidates if repeat % 2 == 0 else tuple(reversed(selected_candidates))
        if effective_pytest_paths and repeat % 2:
            elapsed, return_code = measure_pytest(effective_pytest_paths)
            _validate_pytest_sample(elapsed, return_code)
            pytest_samples.append(elapsed)
        for workers in order:
            elapsed, result = measure_native(
                effective_paths,
                base.with_overrides(workers=workers),
            )
            _require_matching(result, signature, f"sample with {workers} workers")
            if not math.isfinite(elapsed) or elapsed < 0.0:
                raise TuningError("native timer returned an invalid duration")
            samples[workers].append(elapsed)
        if effective_pytest_paths and repeat % 2 == 0:
            elapsed, return_code = measure_pytest(effective_pytest_paths)
            _validate_pytest_sample(elapsed, return_code)
            pytest_samples.append(elapsed)

    measured = tuple(
        TuningCandidate(workers=workers, samples=tuple(samples[workers]))
        for workers in selected_candidates
    )
    best_median = min(candidate.median for candidate in measured)
    tolerance = max(_NEAR_BEST_SECONDS, best_median * _NEAR_BEST_FRACTION)
    recommended = min(
        candidate.workers for candidate in measured if candidate.median <= best_median + tolerance
    )

    return TuningReport(
        paths=effective_paths,
        warmups=warmups,
        repeats=repeats,
        discovered_tests=len(specs),
        execution_units=len(units),
        model_recommendation=model_recommendation,
        recommended_workers=recommended,
        candidates=measured,
        pytest_paths=effective_pytest_paths,
        pytest_samples=tuple(pytest_samples),
        shard_modules=config.shard_modules,
        manifest_used=config.manifest_path is not None,
    )


def render_tuning_report(report: TuningReport) -> str:
    """Render a compact, deterministic human-readable tuning table."""

    lines = [
        (
            f"Testenix tuning  |  {report.discovered_tests} tests  |  "
            f"{report.execution_units} execution units"
        ),
        "Measurement: fresh-process wall clock | history: disabled (--no-history)",
        (
            "Scheduling: safe intra-module sharding"
            if report.shard_modules
            else "Scheduling: module affinity"
        ),
        "",
        "workers  median     min        max        runs",
    ]
    for candidate in report.candidates:
        marker = " *" if candidate.workers == report.recommended_workers else ""
        lines.append(
            f"{candidate.workers:>7}  {candidate.median:>8.3f}s  "
            f"{candidate.minimum:>8.3f}s  {candidate.maximum:>8.3f}s  "
            f"{len(candidate.samples):>4}{marker}"
        )
    lines.extend(
        (
            "",
            f"Recommended workers: {report.recommended_workers}",
            f"Adaptive model before measurement: {report.model_recommendation}",
        )
    )
    if report.pytest_median is not None:
        lines.append("pytest comparison: orientation only; inventory equivalence is not verified")
        lines.append(f"pytest median: {report.pytest_median:.3f}s")
        ratio = report.pytest_over_native
        if ratio is not None:
            lines.append(f"unverified pytest / recommended native: {ratio:.3f}x")
    return "\n".join(lines) + "\n"


def _duration_for(test_id: str, durations: Mapping[str, float], fallback: float) -> float:
    value = durations.get(test_id, fallback)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0.0:
        return fallback
    return parsed


def _has_reliable_durations(
    specs: Sequence[TestSpec],
    durations: Mapping[str, float],
    *,
    shardable_paths: AbstractSet[str] = frozenset(),
) -> bool:
    if not specs:
        return False
    valid_ids: set[str] = set()
    for spec in specs:
        value = durations.get(spec.id)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        parsed = float(value)
        if math.isfinite(parsed) and parsed >= 0.0:
            valid_ids.add(spec.id)
    if len(valid_ids) / len(specs) < _MINIMUM_HISTORY_COVERAGE:
        return False

    # Global coverage alone can hide a brand-new, potentially dominant module.
    # Require at least one real observation for every schedulable unit before
    # using median fallback values inside the cost model.
    normal_modules: dict[str, set[str]] = {}
    isolated: list[str] = []
    for spec in specs:
        if spec.timeout is None and spec.path not in shardable_paths:
            normal_modules.setdefault(spec.path, set()).add(spec.id)
        else:
            isolated.append(spec.id)
    return all(test_ids & valid_ids for test_ids in normal_modules.values()) and all(
        test_id in valid_ids for test_id in isolated
    )


def _normalise_candidates(candidates: Sequence[int]) -> tuple[int, ...]:
    if not candidates:
        raise ValueError("candidates must contain at least one worker count")
    normalised: set[int] = set()
    for workers in candidates:
        if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
            raise ValueError("candidate worker counts must be positive integers")
        normalised.add(workers)
    return tuple(sorted(normalised))


def _result_signature(result: RunResult) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((item.test.id, item.status.value) for item in result.tests))


def _require_green(result: RunResult, label: str) -> None:
    if result.exit_code != 0:
        raise TuningError(f"{label} failed with exit code {result.exit_code}")


def _require_matching(
    result: RunResult,
    signature: tuple[tuple[str, str], ...],
    label: str,
) -> None:
    _require_green(result, label)
    if _result_signature(result) != signature:
        raise TuningError(f"{label} produced a different test inventory or outcomes")


def _validate_pytest_sample(
    elapsed: float,
    return_code: int,
    *,
    label: str = "sample",
) -> None:
    if return_code != 0:
        raise TuningError(f"pytest {label} failed with exit code {return_code}")
    if not math.isfinite(elapsed) or elapsed < 0.0:
        raise TuningError("pytest timer returned an invalid duration")


def _measure_native(
    paths: Sequence[str],
    config: TestenixConfig,
) -> tuple[float, RunResult]:
    with tempfile.TemporaryDirectory(prefix="testenix-tune-") as directory:
        report_path = Path(directory) / "result.json"
        config_path = Path(directory) / "pyproject.toml"
        config_path.write_text(_tuning_config_toml(config), encoding="utf-8")
        command = [
            sys.executable,
            "-m",
            "testenix",
            "--config",
            str(config_path),
            "run",
            "--no-history",
            "--json",
            str(report_path),
            "--no-color",
            "--quiet",
        ]
        command.extend(paths)

        started = time.perf_counter()
        completed = subprocess.run(
            command,
            env=_tuning_environment(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        elapsed = time.perf_counter() - started
        try:
            document = json.loads(report_path.read_text(encoding="utf-8"))
            result = _run_result_from_dict(document)
        except (OSError, json.JSONDecodeError, TuningError) as error:
            raise TuningError(
                f"native timing subprocess failed with exit code {completed.returncode}"
            ) from error
        if completed.returncode != result.exit_code:
            raise TuningError("native timing subprocess exit code does not match its result report")
        return elapsed, result


def _tuning_config_toml(config: TestenixConfig) -> str:
    workers = json.dumps(config.workers) if config.workers == "auto" else str(config.workers)
    lines = [
        "[tool.testenix]",
        f"workers = {workers}",
        f"retries = {config.retries}",
        "history = false",
        f"shard_modules = {'true' if config.shard_modules else 'false'}",
    ]
    if config.timeout is not None:
        lines.append(f"timeout = {config.timeout!r}")
    if config.tags:
        rendered_tags = ", ".join(json.dumps(tag, ensure_ascii=True) for tag in config.tags)
        lines.append(f"tags = [{rendered_tags}]")
    if config.manifest_path is not None:
        lines.append(f"manifest = {json.dumps(str(config.manifest_path), ensure_ascii=True)}")
    return "\n".join(lines) + "\n"


def _measure_pytest(paths: Sequence[str]) -> tuple[float, int]:
    command = (
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        *paths,
    )
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        env=_tuning_environment(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return time.perf_counter() - started, completed.returncode


def _tuning_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "NO_COLOR": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTEST_ADDOPTS": "",
            "TERM": "dumb",
        }
    )
    package_root = str(Path(__file__).resolve().parents[1])
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        f"{package_root}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else package_root
    )
    return environment


def _run_result_from_dict(value: object) -> RunResult:
    """Read the small stable subset needed by the isolated tuning service."""

    try:
        if not isinstance(value, Mapping):
            raise TypeError("result must be an object")
        tests: list[TestResult] = []
        raw_tests = value["tests"]
        if not isinstance(raw_tests, list):
            raise TypeError("tests must be an array")
        for item in raw_tests:
            if not isinstance(item, Mapping) or not isinstance(item["test"], Mapping):
                raise TypeError("test result must be an object")
            test = item["test"]
            parameters = test.get("parameters", {})
            if not isinstance(parameters, Mapping):
                raise TypeError("test parameters must be an object")
            spec = TestSpec(
                id=str(test["id"]),
                path=str(test["path"]),
                module_name=str(test["module_name"]),
                function_name=str(test["function_name"]),
                display_name=str(test["display_name"]),
                parameters=dict(parameters),
                case_id=None if test.get("case_id") is None else str(test["case_id"]),
                tags=frozenset(str(tag) for tag in test.get("tags", ())),
                skip_reason=(None if test.get("skip_reason") is None else str(test["skip_reason"])),
                xfail_reason=(
                    None if test.get("xfail_reason") is None else str(test["xfail_reason"])
                ),
                timeout=None if test.get("timeout") is None else float(test["timeout"]),
                source_line=(None if test.get("source_line") is None else int(test["source_line"])),
            )
            tests.append(
                TestResult(
                    test=spec,
                    status=Status(str(item["status"])),
                    attempts=(),
                    duration=float(item["duration"]),
                )
            )
        raw_issues = value["collection_issues"]
        if not isinstance(raw_issues, list):
            raise TypeError("collection_issues must be an array")
        issues = tuple(
            CollectionIssue(
                path=str(issue["path"]),
                message=str(issue["message"]),
                traceback=(None if issue.get("traceback") is None else str(issue["traceback"])),
            )
            for issue in raw_issues
            if isinstance(issue, Mapping)
        )
        raw_workers = value.get("workers_used")
        workers_used = None if raw_workers is None else int(raw_workers)
        raw_shardable = value.get("shardable_paths", [])
        if not isinstance(raw_shardable, list):
            raise TypeError("shardable_paths must be an array")
        return RunResult(
            run_id=str(value["run_id"]),
            tests=tuple(tests),
            collection_issues=issues,
            started_at=float(value["started_at"]),
            finished_at=float(value["finished_at"]),
            workers_used=workers_used,
            shardable_paths=tuple(str(path) for path in raw_shardable),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise TuningError("native timing subprocess returned a malformed result") from error


__all__ = [
    "ExecutionUnitEstimate",
    "TuningCandidate",
    "TuningError",
    "TuningReport",
    "WorkerEstimate",
    "default_worker_candidates",
    "estimate_spawn_cost",
    "execution_units",
    "render_tuning_report",
    "resolve_adaptive_workers",
    "run_tuning",
    "worker_estimates",
]
