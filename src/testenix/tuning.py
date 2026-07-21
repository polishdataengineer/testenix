"""Adaptive worker selection and reproducible project-local tuning.

The fast path in :func:`resolve_adaptive_workers` is a pure cost model.  It
uses the execution units that Testenix can actually schedule (normal modules
and individually isolated timeout tests), historical test durations, and a
conservative process-startup estimate.  The slower :func:`run_tuning` service
executes an explicit benchmark when a project wants an evidence-based fixed
worker count.
"""

from __future__ import annotations

import ctypes
import functools
import hashlib
import json
import math
import os
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence, Sized
from collections.abc import Set as AbstractSet
from contextlib import suppress
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
DEFAULT_TUNING_RUN_TIMEOUT = 300.0
_PROCESS_TERMINATION_GRACE = 2.0
_PROCESS_TRACKER_INTERVAL = 0.02
_SOURCE_SUFFIXES = frozenset({".py", ".pyi", ".toml"})
_SOURCE_SCAN_EXCLUSIONS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".testenix",
        ".tox",
        ".venv",
        "__pycache__",
        "node_modules",
    }
)

_ProcessIdentity = tuple[int, int] | str


class TuningError(RuntimeError):
    """Raised when a tuning sample cannot produce a trustworthy recommendation."""


@dataclass(slots=True)
class _WindowsKillJob:
    """Small kill-on-close Job Object wrapper used only on Windows."""

    kernel32: Any
    handle: Any
    closed: bool = False

    def terminate(self, exit_code: int = 1) -> bool:
        if self.closed:
            return True
        try:
            return bool(self.kernel32.TerminateJobObject(self.handle, exit_code))
        except Exception:
            return False

    def close(self) -> bool:
        if self.closed:
            return True
        try:
            closed = bool(self.kernel32.CloseHandle(self.handle))
        except Exception:
            return False
        if closed:
            self.closed = True
        return closed


@functools.lru_cache(maxsize=1)
def _darwin_child_lister() -> Any | None:
    """Return libproc's direct-child query without timing ``ps`` on macOS."""

    if sys.platform != "darwin":
        return None
    try:
        library = ctypes.CDLL("/usr/lib/libproc.dylib")
        function = library.proc_listchildpids
        function.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_int)
        function.restype = ctypes.c_int
        return function
    except (AttributeError, OSError):
        return None


@functools.lru_cache(maxsize=1)
def _darwin_identity_probe() -> tuple[Any, type[ctypes.Structure]] | None:
    if sys.platform != "darwin":
        return None
    try:

        class _BsdInfo(ctypes.Structure):
            _fields_ = [
                ("flags", ctypes.c_uint32),
                ("status", ctypes.c_uint32),
                ("xstatus", ctypes.c_uint32),
                ("pid", ctypes.c_uint32),
                ("ppid", ctypes.c_uint32),
                ("uid", ctypes.c_uint32),
                ("gid", ctypes.c_uint32),
                ("ruid", ctypes.c_uint32),
                ("rgid", ctypes.c_uint32),
                ("svuid", ctypes.c_uint32),
                ("svgid", ctypes.c_uint32),
                ("reserved", ctypes.c_uint32),
                ("comm", ctypes.c_char * 16),
                ("name", ctypes.c_char * 32),
                ("nfiles", ctypes.c_uint32),
                ("pgid", ctypes.c_uint32),
                ("pjobc", ctypes.c_uint32),
                ("tty_device", ctypes.c_uint32),
                ("tty_pgid", ctypes.c_uint32),
                ("nice", ctypes.c_int32),
                ("start_seconds", ctypes.c_uint64),
                ("start_microseconds", ctypes.c_uint64),
            ]

        library = ctypes.CDLL("/usr/lib/libproc.dylib")
        function = library.proc_pidinfo
        function.argtypes = (
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        )
        function.restype = ctypes.c_int
        return function, _BsdInfo
    except (AttributeError, OSError):
        return None


def _process_identity(pid: int) -> _ProcessIdentity | None:
    """Return a creation token so cleanup never signals a recycled PID."""

    if sys.platform.startswith("linux"):
        try:
            stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            fields_after_name = stat_line.rsplit(")", 1)[1].split()
            return fields_after_name[19]
        except (IndexError, OSError):
            return None
    if sys.platform == "darwin":
        probe = _darwin_identity_probe()
        if probe is None:
            return None
        function, structure = probe
        information = structure()
        size = ctypes.sizeof(information)
        if function(pid, 3, 0, ctypes.byref(information), size) != size:
            return None
        return int(information.start_seconds), int(information.start_microseconds)
    try:
        completed = subprocess.run(
            ("ps", "-o", "lstart=", "-p", str(pid)),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=_PROCESS_TERMINATION_GRACE,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    token = completed.stdout.strip()
    return token if completed.returncode == 0 and token else None


def _posix_direct_children(pid: int) -> tuple[int, ...]:
    """Read direct children cheaply enough for continuous timing containment."""

    if sys.platform.startswith("linux"):
        children: set[int] = set()
        try:
            for child_file in Path(f"/proc/{pid}/task").glob("*/children"):
                raw = child_file.read_text(encoding="ascii")
                children.update(int(value) for value in raw.split())
        except (OSError, ValueError):
            pass
        return tuple(sorted(children))
    if sys.platform == "darwin":
        function = _darwin_child_lister()
        if function is not None:
            values = (ctypes.c_int * 4096)()
            count = function(pid, values, ctypes.sizeof(values))
            if count > 0:
                return tuple(values[: min(count, len(values))])
            return ()
    return _posix_descendant_pids(pid)


class _PosixTreeTracker:
    """Remember workers before a short-lived coordinator can orphan them."""

    def __init__(self, root_pid: int) -> None:
        self.root_pid = root_pid
        self._root_identity = _process_identity(root_pid)
        self._identities: dict[int, _ProcessIdentity] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._capture()
        self._thread.start()

    def _capture(self) -> None:
        with self._lock:
            known = {
                pid: identity
                for pid, identity in self._identities.items()
                if _process_identity(pid) == identity
            }
        root_is_current = (
            self._root_identity is not None
            and _process_identity(self.root_pid) == self._root_identity
        )
        pending = [*([self.root_pid] if root_is_current else []), *known]
        visited: set[int] = set()
        discovered: set[int] = set()
        while pending:
            parent = pending.pop()
            if parent in visited:
                continue
            visited.add(parent)
            for child in _posix_direct_children(parent):
                if child <= 0 or child == os.getpid():
                    continue
                if child not in discovered:
                    discovered.add(child)
                    pending.append(child)
        live = dict(known)
        for pid in discovered:
            identity = _process_identity(pid)
            if identity is not None:
                live[pid] = identity
        with self._lock:
            self._identities = live

    def _run(self) -> None:
        while not self._stop.wait(_PROCESS_TRACKER_INTERVAL):
            self._capture()

    def stop(self) -> dict[int, _ProcessIdentity]:
        self._stop.set()
        self._thread.join(timeout=_PROCESS_TERMINATION_GRACE)
        self._capture()
        with self._lock:
            return {
                pid: identity
                for pid, identity in self._identities.items()
                if _process_identity(pid) == identity
            }


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
    run_timeout: float = DEFAULT_TUNING_RUN_TIMEOUT

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
                "run_timeout_seconds": self.run_timeout,
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
    run_timeout: float = DEFAULT_TUNING_RUN_TIMEOUT,
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
    if not math.isfinite(run_timeout) or run_timeout <= 0.0:
        raise ValueError("run_timeout must be a finite number greater than zero")
    effective_paths = tuple(paths) if paths else config.paths
    effective_pytest_paths = tuple(pytest_paths)
    snapshot_paths = (*effective_paths, *effective_pytest_paths)
    if config.manifest_path is not None:
        snapshot_paths = (*snapshot_paths, str(config.manifest_path))
    source_snapshot: tuple[tuple[str, str], ...] | None
    require_source_snapshot = native_measure is None or (
        bool(effective_pytest_paths) and pytest_measure is None
    )
    try:
        source_snapshot = _tuning_source_snapshot(snapshot_paths)
    except TuningError:
        if require_source_snapshot:
            raise
        # Injected measurement callbacks are a library/testing seam and may
        # deliberately use virtual paths.  Real subprocess measurements never
        # bypass source immutability checks.
        source_snapshot = None

    def require_unchanged_sources(label: str) -> None:
        if source_snapshot is None:
            return
        if _tuning_source_snapshot(snapshot_paths) != source_snapshot:
            raise TuningError(
                f"project sources changed during {label}; tuning result was discarded"
            )

    measure_native: NativeMeasure
    if native_measure is None:

        def default_native_measure(
            measured_paths: Sequence[str], measured_config: TestenixConfig
        ) -> tuple[float, RunResult]:
            return _measure_native(measured_paths, measured_config, timeout=run_timeout)

        measure_native = default_native_measure
    else:
        measure_native = native_measure
    base = config.with_overrides(history_path=None, json_path=None, junit_path=None)

    _, probe = measure_native(effective_paths, base.with_overrides(workers=1))
    require_unchanged_sources("one-worker probe")
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

    measure_pytest: PytestMeasure
    if pytest_measure is None:

        def default_pytest_measure(measured_paths: Sequence[str]) -> tuple[float, int]:
            return _measure_pytest(measured_paths, timeout=run_timeout)

        measure_pytest = default_pytest_measure
    else:
        measure_pytest = pytest_measure
    pytest_samples: list[float] = []

    for warmup in range(warmups):
        if effective_pytest_paths and warmup % 2:
            elapsed, return_code = measure_pytest(effective_pytest_paths)
            require_unchanged_sources("pytest warmup")
            _validate_pytest_sample(elapsed, return_code, label="warmup")
        for workers in selected_candidates:
            _, result = measure_native(effective_paths, base.with_overrides(workers=workers))
            require_unchanged_sources(f"warmup with {workers} workers")
            _require_matching(result, signature, f"warmup with {workers} workers")
        if effective_pytest_paths and warmup % 2 == 0:
            elapsed, return_code = measure_pytest(effective_pytest_paths)
            require_unchanged_sources("pytest warmup")
            _validate_pytest_sample(elapsed, return_code, label="warmup")

    samples: dict[int, list[float]] = {workers: [] for workers in selected_candidates}
    for repeat in range(repeats):
        order = selected_candidates if repeat % 2 == 0 else tuple(reversed(selected_candidates))
        if effective_pytest_paths and repeat % 2:
            elapsed, return_code = measure_pytest(effective_pytest_paths)
            require_unchanged_sources("pytest sample")
            _validate_pytest_sample(elapsed, return_code)
            pytest_samples.append(elapsed)
        for workers in order:
            elapsed, result = measure_native(
                effective_paths,
                base.with_overrides(workers=workers),
            )
            require_unchanged_sources(f"sample with {workers} workers")
            _require_matching(result, signature, f"sample with {workers} workers")
            if not math.isfinite(elapsed) or elapsed < 0.0:
                raise TuningError("native timer returned an invalid duration")
            samples[workers].append(elapsed)
        if effective_pytest_paths and repeat % 2 == 0:
            elapsed, return_code = measure_pytest(effective_pytest_paths)
            require_unchanged_sources("pytest sample")
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
        run_timeout=run_timeout,
    )


def render_tuning_report(report: TuningReport) -> str:
    """Render a compact, deterministic human-readable tuning table."""

    lines = [
        (
            f"Testenix tuning  |  {report.discovered_tests} tests  |  "
            f"{report.execution_units} execution units"
        ),
        "Measurement: fresh-process wall clock | history: disabled (--no-history)",
        f"Per-run deadline: {report.run_timeout:g}s (platform-aware bounded cleanup)",
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


def _tuning_source_snapshot(paths: Sequence[str]) -> tuple[tuple[str, str], ...]:
    """Fingerprint project Python/TOML inputs and every explicit file path.

    Measurements are invalid if test bodies, imported project helpers, the
    project configuration, or a trusted manifest changes between samples.
    Virtual environments and common cache directories are excluded so
    the snapshot remains project-local and stable.
    """

    selected: set[Path] = set()
    scan_roots: set[Path] = {Path.cwd().resolve()}
    for raw_path in paths:
        candidate = Path(raw_path).expanduser()
        if not candidate.exists() and "::" in raw_path:
            # pytest node IDs are valid comparison inputs, while their source
            # component remains the filesystem object that must be hashed.
            candidate = Path(raw_path.split("::", 1)[0]).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise TuningError(f"cannot fingerprint tuning input {candidate}: {error}") from error
        if resolved.is_file():
            selected.add(resolved)
        elif resolved.is_dir():
            scan_roots.add(resolved)
        else:
            raise TuningError(f"cannot fingerprint non-file tuning input: {candidate}")

    pending_roots = sorted(scan_roots, key=lambda item: str(item), reverse=True)
    visited_roots: set[Path] = set()
    while pending_roots:
        root = pending_roots.pop().resolve()
        if root in visited_roots:
            continue
        visited_roots.add(root)
        try:

            def raise_walk_error(error: OSError) -> None:
                raise error

            walker = os.walk(root, onerror=raise_walk_error, followlinks=False)
            for directory, directory_names, file_names in walker:
                parent = Path(directory)
                retained_directories: list[str] = []
                for name in sorted(directory_names):
                    child = parent / name
                    if name in _SOURCE_SCAN_EXCLUSIONS or (child / "pyvenv.cfg").is_file():
                        continue
                    if child.is_symlink():
                        target = child.resolve(strict=True)
                        if target.is_dir() and target not in visited_roots:
                            pending_roots.append(target)
                        continue
                    retained_directories.append(name)
                directory_names[:] = retained_directories
                for name in sorted(file_names):
                    path = parent / name
                    if path.suffix in _SOURCE_SUFFIXES:
                        selected.add(path.resolve(strict=True))
        except OSError as error:
            raise TuningError(
                f"cannot fingerprint project sources below {root}: {error}"
            ) from error

    fingerprints: list[tuple[str, str]] = []
    for source in sorted(selected, key=lambda item: str(item)):
        fingerprints.append((str(source), _source_identity_fingerprint(source)))
    return tuple(fingerprints)


def _source_identity_fingerprint(source: Path) -> str:
    """Hash bytes plus immutable-enough metadata and reject an unstable read."""

    try:
        before = source.stat()
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        after = source.stat()
    except OSError as error:
        raise TuningError(f"cannot fingerprint project source {source}: {error}") from error
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if before_identity != after_identity:
        raise TuningError(f"project source changed while being fingerprinted: {source}")
    metadata = ":".join(str(value) for value in after_identity)
    return f"{digest}:{metadata}"


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
    *,
    timeout: float = DEFAULT_TUNING_RUN_TIMEOUT,
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
        return_code = _run_bounded_process(
            command,
            env=_tuning_environment(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            label="native Testenix sample",
        )
        elapsed = time.perf_counter() - started
        try:
            document = json.loads(report_path.read_text(encoding="utf-8"))
            result = _run_result_from_dict(document)
        except (OSError, json.JSONDecodeError, TuningError) as error:
            raise TuningError(
                f"native timing subprocess failed with exit code {return_code}"
            ) from error
        if return_code != result.exit_code:
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


def _measure_pytest(
    paths: Sequence[str],
    *,
    timeout: float = DEFAULT_TUNING_RUN_TIMEOUT,
) -> tuple[float, int]:
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
    return_code = _run_bounded_process(
        command,
        env=_tuning_environment(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
        label="pytest sample",
    )
    return time.perf_counter() - started, return_code


def _run_bounded_process(
    command: Sequence[str],
    *,
    env: Mapping[str, str],
    stdout: int,
    stderr: int,
    timeout: float,
    label: str,
) -> int:
    """Run one timing command with a bounded, platform-aware cleanup boundary.

    POSIX detached descendants are captured by an immediate identity-aware
    snapshot and poller. A child which calls ``setsid()`` and loses its leader
    before that first snapshot remains a documented best-effort edge; Windows
    uses kernel Job Object containment before the process is resumed.
    """

    if not math.isfinite(timeout) or timeout <= 0.0:
        raise ValueError("subprocess timeout must be a finite number greater than zero")
    options: dict[str, Any] = {
        "env": dict(env),
        "stdout": stdout,
        "stderr": stderr,
    }
    if os.name == "nt":
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        creation_flags |= getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
        options["creationflags"] = creation_flags
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(tuple(command), **options)
    windows_job: _WindowsKillJob | None = None
    tracker: _PosixTreeTracker | None = None
    cleanup_started = False
    try:
        windows_job = _windows_kill_job(process)
        tracker = _PosixTreeTracker(process.pid) if os.name == "posix" else None
        if os.name == "nt":
            if windows_job is None:
                raise TuningError(
                    "cannot place Windows tuning process in a kill-on-close Job Object"
                )
            try:
                _resume_windows_process(process)
            except OSError as error:
                raise TuningError(
                    f"cannot start contained Windows tuning process: {error}"
                ) from error
        try:
            return_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            cleanup_started = True
            tracked_pids = tracker.stop() if tracker is not None else {}
            _terminate_process_tree(
                process,
                windows_job,
                tracked_pids=tracked_pids,
            )
            raise TuningError(f"{label} exceeded the {timeout:g}s per-run deadline") from error

        if tracker is not None:
            cleanup_started = True
            tracked_pids = tracker.stop()
            # A successful coordinator must not leave background workers. The
            # remembered root PGID also catches an untracked same-session child
            # after an unusually fast leader exit.
            _posix_signal_tree(
                process.pid,
                tracked_pids,
                signal.SIGKILL,
                root_group_owned=False,
            )
        elif windows_job is not None:
            cleanup_started = True
            if not _cleanup_windows_tree(process, windows_job):
                raise TuningError("could not verify cleanup of the Windows tuning process tree")
        return return_code
    except BaseException:
        if not cleanup_started:
            cleanup_started = True
            try:
                tracked_pids = tracker.stop() if tracker is not None else {}
            except Exception:
                tracked_pids = {}
            try:
                _terminate_process_tree(
                    process,
                    windows_job,
                    tracked_pids=tracked_pids,
                )
            except Exception:
                with suppress(OSError, ValueError):
                    process.kill()
                with suppress(OSError, subprocess.TimeoutExpired):
                    process.wait(timeout=_PROCESS_TERMINATION_GRACE)
        raise
    finally:
        if tracker is not None:
            tracker.stop()
        if windows_job is not None:
            windows_job.close()


def _windows_kill_job(process: subprocess.Popen[Any]) -> _WindowsKillJob | None:
    """Attach a kill-on-close Job Object when the Windows host allows it."""

    if os.name != "nt":
        return None
    try:  # pragma: no cover - exercised by the Windows CI matrix.
        from ctypes import wintypes

        class _BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimitInformation),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return None
        job = _WindowsKillJob(kernel32, handle)
        information = _ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        configured = kernel32.SetInformationJobObject(
            handle,
            9,  # JobObjectExtendedLimitInformation
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
        raw_process_handle = vars(process).get("_handle")
        if raw_process_handle is None:
            job.close()
            return None
        process_handle = wintypes.HANDLE(int(raw_process_handle))
        if not configured or not kernel32.AssignProcessToJobObject(handle, process_handle):
            job.close()
            return None
        return job
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _resume_windows_process(process: subprocess.Popen[Any]) -> None:
    """Resume a suspended process only after Job Object containment exists."""

    if os.name != "nt":
        raise OSError("Windows process resume requested on a non-Windows host")
    try:  # pragma: no cover - exercised by the Windows CI matrix.
        from ctypes import wintypes

        raw_process_handle = vars(process).get("_handle")
        if raw_process_handle is None:
            raise OSError("subprocess has no Windows process handle")
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)  # type: ignore[attr-defined]
        ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
        ntdll.NtResumeProcess.restype = ctypes.c_long
        status = ntdll.NtResumeProcess(wintypes.HANDLE(int(raw_process_handle)))
        if status != 0:
            raise OSError(f"NtResumeProcess failed with status 0x{status & 0xFFFFFFFF:08x}")
    except (AttributeError, TypeError, ValueError) as error:
        raise OSError(f"cannot resume contained Windows tuning process: {error}") from error


def _terminate_process_tree(
    process: subprocess.Popen[Any],
    windows_job: _WindowsKillJob | None,
    *,
    tracked_pids: Mapping[int, _ProcessIdentity] | None = None,
) -> None:
    """Best-effort cross-platform cleanup for a spawned timing process tree."""

    cleaned = True
    if os.name == "nt":
        cleaned = _cleanup_windows_tree(process, windows_job)
    else:
        descendants = dict(tracked_pids or {})
        descendants.update(_identity_snapshot(_posix_descendant_pids(process.pid)))
        _posix_signal_tree(
            process.pid,
            descendants,
            signal.SIGTERM,
            root_group_owned=process.poll() is None,
        )
        with suppress(OSError, subprocess.TimeoutExpired):
            process.wait(timeout=_PROCESS_TERMINATION_GRACE)
        # Testenix workers create their own sessions, so they can escape the
        # coordinator's process group. Kill both the snapshotted descendants
        # and the group: the former reaches detached workers, while the latter
        # catches children created between the process-table snapshot and TERM.
        descendants.update(_identity_snapshot(_posix_descendant_pids(process.pid)))
        _posix_signal_tree(
            process.pid,
            descendants,
            signal.SIGKILL,
            root_group_owned=process.poll() is None,
        )
    if process.poll() is None:
        with suppress(OSError):
            process.kill()
    with suppress(OSError, subprocess.TimeoutExpired):
        process.wait(timeout=_PROCESS_TERMINATION_GRACE)
    if os.name == "nt" and not cleaned:
        raise TuningError("could not verify cleanup of the Windows tuning process tree")


def _posix_signal_tree(
    root_pid: int,
    descendants: Mapping[int, _ProcessIdentity],
    signum: int,
    *,
    root_group_owned: bool,
) -> None:
    """Signal only live identities, never a PID recycled during a long run."""

    own_group = os.getpgrp()
    groups = {root_pid} if root_group_owned else set()
    for pid, identity in descendants.items():
        if _process_identity(pid) != identity:
            continue
        with suppress(OSError):
            group = os.getpgid(pid)
            if group != own_group and _process_identity(pid) == identity:
                groups.add(group)
    groups.discard(own_group)
    for group in groups:
        with suppress(OSError):
            os.killpg(group, signum)


def _identity_snapshot(pids: Sequence[int]) -> dict[int, _ProcessIdentity]:
    identities: dict[int, _ProcessIdentity] = {}
    for pid in pids:
        identity = _process_identity(pid)
        if identity is not None:
            identities[pid] = identity
    return identities


def _bounded_taskkill(pid: int) -> bool:
    try:  # pragma: no cover - exercised by the Windows CI matrix.
        completed = subprocess.run(
            ("taskkill", "/PID", str(pid), "/T", "/F"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=_PROCESS_TERMINATION_GRACE,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _cleanup_windows_tree(
    process: subprocess.Popen[Any],
    windows_job: _WindowsKillJob | None,
) -> bool:
    if windows_job is not None:
        terminated = windows_job.terminate()
        closed = windows_job.close()
        fallback = False if terminated or closed else _bounded_taskkill(process.pid)
        cleaned = terminated or closed or fallback
    else:
        cleaned = _bounded_taskkill(process.pid)
    with suppress(OSError, ValueError):
        process.kill()
    return cleaned


def _posix_descendant_pids(root_pid: int) -> tuple[int, ...]:
    """Snapshot descendants deepest-first without a third-party dependency."""

    try:
        completed = subprocess.run(
            ("ps", "-axo", "pid=,ppid="),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=_PROCESS_TERMINATION_GRACE,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    if completed.returncode != 0:
        return ()

    children: dict[int, list[int]] = {}
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            pid, parent = (int(field) for field in fields)
        except ValueError:
            continue
        children.setdefault(parent, []).append(pid)

    depths: dict[int, int] = {}
    pending = [(root_pid, 0)]
    while pending:
        parent, depth = pending.pop()
        for child in children.get(parent, ()):
            if child in depths:
                continue
            depths[child] = depth + 1
            pending.append((child, depth + 1))
    return tuple(sorted(depths, key=lambda pid: (-depths[pid], pid)))


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
