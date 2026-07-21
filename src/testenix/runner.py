"""Application service connecting discovery, scheduling, workers, events, and history."""

from __future__ import annotations

import asyncio
import os
import statistics
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from testenix.aggregate import finalize_status, reduce_events
from testenix.config import TestenixConfig
from testenix.contracts import (
    AttemptResult,
    CollectionIssue,
    Event,
    EventType,
    Phase,
    PhaseResult,
    RunResult,
    Status,
    TestResult,
    TestSpec,
)
from testenix.discovery import CollectedTest, CollectionResult, discover, discover_selected
from testenix.events import (
    EventFactory,
    EventSink,
    InMemoryEventSink,
    JsonlEventSink,
    json_safe,
    safe_event_copy,
)
from testenix.executor import NativeExecutionError, execute_tests
from testenix.history import HistoryStore
from testenix.scheduler import schedule_lpt
from testenix.sharding import (
    CollectionManifestError,
    ModuleShardingDecision,
    ShardingPolicy,
    TrustedCollectionManifest,
    assess_collection_sharding,
    build_trusted_collection_manifest,
    deserialize_trusted_collection_manifest,
    validate_trusted_collection_manifest,
    verify_trusted_collection_manifest,
)
from testenix.worker import ProcessSupervisor, WorkerExecution, WorkItem

if TYPE_CHECKING:
    from testenix.tuning import SpawnMethod

_RETRYABLE_TEST_STATUSES = frozenset(
    {
        Status.FAIL,
        Status.ERROR_SETUP,
        Status.ERROR_TEARDOWN,
        Status.TIMEOUT,
    }
)
_AUTOMATIC_RECOVERY_STATUSES = frozenset({Status.INFRA_ERROR, Status.CRASH})
_COLLECTION_TIMEOUT = 30.0
_WORKER_STARTUP_TIMEOUT = 30.0
_NO_SHARDABLE_PATHS: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class _PendingAttempt:
    spec: TestSpec
    attempt: int

    @property
    def id(self) -> str:
        return self.spec.id


@dataclass(frozen=True, slots=True)
class _NativeTestLocator:
    """Primitive rediscovery key safe across the spawn boundary."""

    id: str
    path: str
    function_name: str
    case_id: str | None
    timeout: float | None


@dataclass(frozen=True, slots=True)
class _ExecutionUnit:
    """Tests that must share one process, or one hard-isolated timeout test."""

    id: str
    specs: tuple[TestSpec, ...]
    estimated_duration: float
    isolated: bool = False


@dataclass(frozen=True, slots=True)
class _PlannedWork:
    specs: tuple[TestSpec, ...]
    item: WorkItem


@dataclass(frozen=True, slots=True)
class _CollectionManifest:
    tests: tuple[TestSpec, ...]
    issues: tuple[CollectionIssue, ...]
    sharding: tuple[ModuleShardingDecision, ...] = ()


class _RunEventSink:
    """Fast coordinator fanout with isolation only at the user boundary."""

    def __init__(
        self,
        memory: InMemoryEventSink,
        log: JsonlEventSink | None,
        external: EventSink | None,
    ) -> None:
        self._memory = memory
        self._log = log
        self._external = external

    def emit(self, event: Event) -> Event:
        # Internal sinks are trusted and never mutate events. The external sink
        # receives a canonical deep copy so it cannot corrupt the reducer's
        # in-memory facts.
        self._memory.emit(event)
        if self._log is not None:
            self._log.emit(event)
        if self._external is not None:
            self._external.emit(safe_event_copy(event))
        return event

    def close(self) -> None:
        if self._log is not None:
            self._log.close()


def _effective_specs(specs: Sequence[TestSpec], config: TestenixConfig) -> tuple[TestSpec, ...]:
    required_tags = set(config.tags)
    selected: list[TestSpec] = []
    for spec in specs:
        if required_tags and not required_tags.issubset(spec.tags):
            continue
        timeout = spec.timeout if spec.timeout is not None else config.timeout
        selected.append(spec if timeout == spec.timeout else replace(spec, timeout=timeout))
    return tuple(selected)


def _collection_issue_for_empty(paths: Sequence[str]) -> CollectionIssue:
    joined = ", ".join(paths)
    return CollectionIssue(path=joined, message="no tests collected")


def _collection_issue_for_tags(paths: Sequence[str], tags: Sequence[str]) -> CollectionIssue:
    joined = ", ".join(paths)
    rendered_tags = ", ".join(tags)
    return CollectionIssue(
        path=joined,
        message=f"no tests selected by required tags: {rendered_tags}",
    )


def _locator(spec: TestSpec) -> _NativeTestLocator:
    return _NativeTestLocator(
        id=spec.id,
        path=spec.path,
        function_name=spec.function_name,
        case_id=spec.case_id,
        timeout=spec.timeout,
    )


def _portable_spec(spec: TestSpec) -> TestSpec:
    parameters = json_safe(spec.parameters)
    return replace(
        spec,
        parameters=dict(parameters) if isinstance(parameters, Mapping) else {},
    )


def _discover_manifest(
    paths: Sequence[str],
    analyse_sharding: bool = False,
) -> _CollectionManifest:
    collection = discover(paths)
    return _CollectionManifest(
        tests=tuple(_portable_spec(item.spec) for item in collection.items),
        issues=collection.issues,
        sharding=assess_collection_sharding(collection) if analyse_sharding else (),
    )


def _discover_trusted_manifest(
    paths: Sequence[str],
    project_root: str,
) -> TrustedCollectionManifest:
    """Collect and fingerprint inside the same isolated boundary as a run."""

    # ``paths`` are project-relative by contract.  The manifest fingerprinting
    # code already resolves them against ``project_root``; collection must use
    # that same base when the caller's current working directory is elsewhere.
    # This target always runs in a short-lived supervised child process, so the
    # directory change cannot leak into the coordinator.
    os.chdir(project_root)
    collection = discover(paths)
    return build_trusted_collection_manifest(paths, collection, project_root=project_root)


def collect_trusted_manifest(
    paths: Sequence[str] | str,
    *,
    project_root: str | Path | None = None,
) -> TrustedCollectionManifest:
    """Create an explicit collection manifest in a supervised worker process."""

    effective_paths = (paths,) if isinstance(paths, str) else tuple(paths)
    if not effective_paths:
        raise CollectionManifestError("at least one collection path is required")
    root = Path.cwd().resolve() if project_root is None else Path(project_root).resolve()
    supervisor = ProcessSupervisor(start_method="spawn")
    execution = supervisor.execute(
        WorkItem(
            test_id="collection-manifest",
            target=_discover_trusted_manifest,
            args=(effective_paths, str(root)),
            timeout=_COLLECTION_TIMEOUT,
        )
    )
    if execution.status is Status.CANCELLED:
        raise asyncio.CancelledError
    if isinstance(execution.value, TrustedCollectionManifest):
        manifest = validate_trusted_collection_manifest(execution.value)
        if manifest.issues:
            details = "; ".join(issue.message for issue in manifest.issues)
            raise CollectionManifestError(f"collection reported errors: {details}")
        return manifest
    error = execution.error
    message = error.message if error is not None else "collection worker returned no manifest"
    diagnostics = (
        (error.traceback if error is not None else None)
        or execution.stderr
        or execution.stdout
        or ""
    )
    suffix = f"\n{diagnostics}" if diagnostics else ""
    raise CollectionManifestError(f"isolated manifest collection failed: {message}{suffix}")


def _collect_in_worker(
    paths: Sequence[str],
    supervisor: ProcessSupervisor,
    *,
    analyse_sharding: bool = False,
) -> _CollectionManifest:
    execution = supervisor.execute(
        WorkItem(
            test_id="collection",
            target=_discover_manifest,
            args=(tuple(paths), analyse_sharding),
            timeout=_COLLECTION_TIMEOUT,
        )
    )
    if execution.status is Status.CANCELLED:
        raise asyncio.CancelledError
    if isinstance(execution.value, _CollectionManifest):
        return execution.value
    error = execution.error
    message = error.message if error is not None else "collection worker returned no manifest"
    diagnostics = (
        (error.traceback if error is not None else None)
        or execution.stderr
        or execution.stdout
        or None
    )
    return _CollectionManifest(
        tests=(),
        issues=(
            CollectionIssue(
                path=", ".join(paths),
                message=f"isolated collection failed: {message}",
                traceback=diagnostics,
            ),
        ),
    )


def _verified_trusted_collection(
    trusted_manifest: TrustedCollectionManifest | None,
    paths: Sequence[str],
) -> _CollectionManifest | None:
    """Project a valid explicit manifest, or request normal collection fallback."""

    if trusted_manifest is None:
        return None
    try:
        validated = validate_trusted_collection_manifest(trusted_manifest)
    except (CollectionManifestError, TypeError, ValueError):
        return None
    if not verify_trusted_collection_manifest(validated, paths):
        return None
    return _CollectionManifest(
        tests=validated.tests,
        issues=validated.issues,
        sharding=validated.sharding,
    )


def _resolve_locators_in_worker(
    locators: Sequence[_NativeTestLocator],
) -> tuple[CollectedTest, ...]:
    """Rediscover tests without pickling arbitrary parameter values."""

    requested_names: dict[str, set[str]] = {}
    for locator in locators:
        requested_names.setdefault(locator.path, set()).add(locator.function_name)
    collections: dict[str, CollectionResult] = {}
    indexes: dict[str, dict[str, CollectedTest]] = {}
    resolved: list[CollectedTest] = []
    for locator in locators:
        collection = collections.get(locator.path)
        if collection is None:
            collection = discover_selected(locator.path, requested_names[locator.path])
            collections[locator.path] = collection
            indexes[locator.path] = {item.spec.id: item for item in collection.items}
        if collection.issues:
            details = "; ".join(issue.message for issue in collection.issues)
            raise NativeExecutionError(f"cannot load {locator.path}: {details}")
        item = indexes[locator.path].get(locator.id)
        if item is None:
            candidates = [
                candidate
                for candidate in collection.items
                if candidate.spec.function_name == locator.function_name
                and candidate.spec.case_id == locator.case_id
            ]
            if len(candidates) != 1:
                raise NativeExecutionError(f"cannot resolve native test {locator.id!r}")
            item = candidates[0]
        effective_spec = replace(item.spec, timeout=locator.timeout)
        resolved.append(
            CollectedTest(
                spec=effective_spec,
                function=item.function,
                registry=collection.registry,
            )
        )
    return tuple(resolved)


def _execute_native_batch(
    locators: Sequence[_NativeTestLocator],
    attempt: int,
    *,
    _testenix_result_sink: Callable[[AttemptResult], None] | None = None,
    _testenix_ready_sink: Callable[[], None] | None = None,
) -> tuple[AttemptResult, ...]:
    """Process target returning only parameter-free attempt contracts."""

    def publish(result: TestResult) -> None:
        if _testenix_result_sink is not None:
            _testenix_result_sink(result.attempts[-1])

    resolved = _resolve_locators_in_worker(locators)
    if _testenix_ready_sink is not None:
        _testenix_ready_sink()
    results = execute_tests(
        resolved,
        attempt=attempt,
        on_result=publish if _testenix_result_sink is not None else None,
    )
    attempts = tuple(result.attempts[-1] for result in results)
    # The streamed values are the durable per-test facts used for crash
    # recovery. Re-sending the complete tuple in the final envelope doubles
    # IPC and pickling cost for successful batches. An empty tuple is the final
    # scope-teardown ACK; direct/non-streaming callers still receive all facts.
    return () if _testenix_result_sink is not None else attempts


def _execute_native_one(
    locator: _NativeTestLocator,
    attempt: int,
    *,
    _testenix_ready_sink: Callable[[], None] | None = None,
) -> AttemptResult:
    """Pickle-safe process target used by isolated retries."""

    return _execute_native_batch(
        (locator,),
        attempt,
        _testenix_ready_sink=_testenix_ready_sink,
    )[0]


def _event_log_path(config: TestenixConfig, run_id: str) -> Path | None:
    history_path = config.history_path
    if history_path is None or str(history_path) == ":memory:":
        return None
    return history_path.parent / "runs" / f"{run_id}.jsonl"


def _duration_history(config: TestenixConfig, tests: Sequence[TestSpec]) -> dict[str, float]:
    if config.history_path is None:
        return {}
    with HistoryStore(config.history_path) as history:
        return history.estimated_durations(test.id for test in tests)


def _single_deadline(spec: TestSpec) -> float | None:
    if spec.timeout is None:
        return None
    # The in-process timer owns the exact user deadline. This outer process
    # deadline is a portable fallback (notably for blocking calls on Windows)
    # plus a bounded allowance for fixture cleanup and result transport.
    grace = max(0.1, min(1.0, spec.timeout * 0.25))
    return spec.timeout + grace


def _execution_units(
    specs: Sequence[TestSpec],
    durations: Mapping[str, float],
    *,
    shardable_paths: frozenset[str] = _NO_SHARDABLE_PATHS,
) -> tuple[_ExecutionUnit, ...]:
    """Keep normal modules intact unless an opt-in safety decision allows splitting."""

    known = tuple(value for value in durations.values() if value >= 0.0)
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

    units: list[_ExecutionUnit] = []
    for path, module_specs in modules.items():
        materialized = tuple(module_specs)
        units.append(
            _ExecutionUnit(
                id=f"module:{path}",
                specs=materialized,
                estimated_duration=sum(durations.get(spec.id, fallback) for spec in materialized),
            )
        )
    units.extend(
        _ExecutionUnit(
            id=f"test:{spec.id}",
            specs=(spec,),
            estimated_duration=durations.get(spec.id, fallback),
        )
        for spec in sharded
    )
    units.extend(
        _ExecutionUnit(
            id=f"isolated:{spec.id}",
            specs=(spec,),
            estimated_duration=durations.get(spec.id, fallback),
            isolated=True,
        )
        for spec in isolated
    )
    return tuple(units)


def _initial_work_plan(
    specs: Sequence[TestSpec],
    *,
    worker_count: int,
    durations: Mapping[str, float],
    shardable_paths: frozenset[str] = _NO_SHARDABLE_PATHS,
) -> tuple[tuple[_PlannedWork, ...], ...]:
    units = _execution_units(specs, durations, shardable_paths=shardable_paths)
    if not units:
        return ()
    unit_durations = {unit.id: unit.estimated_duration for unit in units}
    shards = tuple(
        shard
        for shard in schedule_lpt(
            units,
            min(worker_count, len(units)),
            unit_durations,
            key=lambda unit: unit.id,
        )
        if shard.items
    )
    planned_shards: list[tuple[_PlannedWork, ...]] = []
    for shard in shards:
        planned: list[_PlannedWork] = []
        shared_specs = tuple(
            spec for unit in shard.items if not unit.isolated for spec in unit.specs
        )
        if shared_specs:
            planned.append(
                _PlannedWork(
                    specs=shared_specs,
                    item=WorkItem(
                        test_id=f"shard-{shard.shard_id}",
                        target=_execute_native_batch,
                        args=(tuple(_locator(spec) for spec in shared_specs), 1),
                        stream_callback_arg="_testenix_result_sink",
                    ),
                )
            )
        for unit in shard.items:
            if not unit.isolated:
                continue
            spec = unit.specs[0]
            planned.append(
                _PlannedWork(
                    specs=(spec,),
                    item=WorkItem(
                        test_id=spec.id,
                        target=_execute_native_batch,
                        args=((_locator(spec),), 1),
                        timeout=_single_deadline(spec),
                        stream_callback_arg="_testenix_result_sink",
                        ready_callback_arg="_testenix_ready_sink",
                        startup_timeout=_WORKER_STARTUP_TIMEOUT,
                    ),
                )
            )
        planned_shards.append(tuple(planned))
    return tuple(planned_shards)


def _infra_result(
    spec: TestSpec,
    attempt: int,
    *,
    worker_id: str,
    status: Status,
    message: str,
    started_at: float,
    finished_at: float,
    exception_type: str = "testenix.runner.WorkerProtocolError",
    traceback: str | None = None,
    stdout: str = "",
    stderr: str = "",
    phase_kind: Phase = Phase.CALL,
) -> TestResult:
    duration = max(0.0, finished_at - started_at)
    phase_result = PhaseResult(
        phase=phase_kind,
        status=status,
        duration=duration,
        message=message,
        exception_type=exception_type,
        traceback=traceback,
        stdout=stdout,
        stderr=stderr,
    )
    attempt_result = AttemptResult(
        test_id=spec.id,
        attempt=attempt,
        worker_id=worker_id,
        status=status,
        duration=duration,
        phases=(phase_result,),
        started_at=started_at,
        finished_at=finished_at,
    )
    return TestResult(
        test=spec,
        status=status,
        attempts=(attempt_result,),
        duration=duration,
    )


def _failure_for_execution(
    spec: TestSpec,
    attempt: int,
    execution: WorkerExecution,
    *,
    message: str | None = None,
    status_override: Status | None = None,
    phase: Phase = Phase.CALL,
) -> TestResult:
    source = execution.attempt_result
    status = status_override or source.status
    if status in {Status.PASS, Status.CACHED_PASS}:
        status = Status.INFRA_ERROR
    error = execution.error
    return _infra_result(
        spec,
        attempt,
        worker_id=execution.worker_id,
        status=status,
        message=message
        or (error.message if error is not None else "worker returned no test result"),
        started_at=source.started_at,
        finished_at=source.finished_at,
        exception_type=(
            error.exception_type if error is not None else "testenix.runner.WorkerProtocolError"
        ),
        traceback=None if error is None else error.traceback,
        stdout=execution.stdout,
        stderr=execution.stderr,
        phase_kind=phase,
    )


def _merge_outer_output(result: TestResult, execution: WorkerExecution) -> TestResult:
    """Preserve output produced during module import outside executor phase capture."""

    if not execution.stdout and not execution.stderr:
        return replace(result, test=result.test)
    attempt = result.attempts[-1]
    phases = list(attempt.phases)
    if phases:
        first = phases[0]
        phases[0] = replace(
            first,
            stdout=f"{execution.stdout}{first.stdout}",
            stderr=f"{execution.stderr}{first.stderr}",
        )
    else:
        phases.append(
            PhaseResult(
                phase=Phase.SETUP,
                status=Status.PASS,
                duration=0.0,
                stdout=execution.stdout,
                stderr=execution.stderr,
            )
        )
    updated_attempt = replace(attempt, phases=tuple(phases))
    return replace(result, attempts=(*result.attempts[:-1], updated_attempt))


def _results_from_batch_execution(
    specs: Sequence[TestSpec],
    attempt: int,
    execution: WorkerExecution,
) -> tuple[TestResult, ...]:
    candidates: list[AttemptResult] = [
        value for value in execution.streamed_values if isinstance(value, AttemptResult)
    ]
    value = execution.value
    if isinstance(value, AttemptResult):
        candidates.append(value)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        candidates.extend(item for item in value if isinstance(item, AttemptResult))

    # A session finalizer may update and republish an earlier attempt. Last fact
    # wins within this single worker protocol stream; event aggregation remains
    # append-only once these finalized attempt facts reach the coordinator.
    by_id = {result.test_id: result for result in candidates if result.attempt == attempt}
    unacknowledged_owner_id: str | None = None
    if execution.error is not None and specs and all(spec.id in by_id for spec in specs):
        # Streamed results precede session teardown and the final protocol ACK.
        # If teardown kills or stalls the worker after every test was streamed,
        # attach the worker failure to a deterministic owner rather than
        # reporting a false-green run.
        unacknowledged_owner_id = specs[-1].id
        by_id.pop(unacknowledged_owner_id, None)
    results: list[TestResult] = []
    outer_output_merged = False
    for spec in specs:
        attempt_result = by_id.get(spec.id)
        if attempt_result is None:
            missing_scope_ack = spec.id == unacknowledged_owner_id
            results.append(
                _failure_for_execution(
                    spec,
                    attempt,
                    execution,
                    message=(
                        None
                        if spec.id == unacknowledged_owner_id
                        else "worker omitted this test from its batch result"
                    ),
                    status_override=(
                        (Status.TIMEOUT if execution.timed_out else Status.ERROR_TEARDOWN)
                        if missing_scope_ack
                        else None
                    ),
                    phase=Phase.TEARDOWN if missing_scope_ack else Phase.CALL,
                )
            )
            continue
        result = TestResult(
            test=spec,
            status=attempt_result.status,
            attempts=(attempt_result,),
            duration=attempt_result.duration,
        )
        if not outer_output_merged:
            result = _merge_outer_output(result, execution)
            outer_output_merged = True
        results.append(result)
    return tuple(results)


def _execute_initial_attempts(
    specs: Sequence[TestSpec],
    *,
    worker_count: int,
    durations: Mapping[str, float],
    supervisor: ProcessSupervisor,
    shardable_paths: frozenset[str] = _NO_SHARDABLE_PATHS,
) -> tuple[tuple[TestResult, ...], int]:
    plan = _initial_work_plan(
        specs,
        worker_count=worker_count,
        durations=durations,
        shardable_paths=shardable_paths,
    )
    executions = supervisor.execute_shards(
        tuple(tuple(work.item for work in shard) for shard in plan)
    )
    results: list[TestResult] = []
    for planned_shard, shard_executions in zip(plan, executions, strict=True):
        for work, execution in zip(planned_shard, shard_executions, strict=True):
            results.extend(_results_from_batch_execution(work.specs, 1, execution))
    return tuple(results), len(plan)


def _execute_retry_attempts(
    pending: Sequence[_PendingAttempt],
    *,
    worker_count: int,
    durations: Mapping[str, float],
    supervisor: ProcessSupervisor,
) -> tuple[tuple[TestResult, ...], int]:
    shards = tuple(
        shard
        for shard in schedule_lpt(
            pending,
            min(worker_count, len(pending)),
            durations,
            key=lambda item: item.id,
        )
        if shard.items
    )
    work_shards = tuple(
        tuple(
            WorkItem(
                test_id=item.spec.id,
                target=_execute_native_one,
                args=(_locator(item.spec), item.attempt),
                attempt=item.attempt,
                timeout=_single_deadline(item.spec),
                ready_callback_arg=(
                    "_testenix_ready_sink" if item.spec.timeout is not None else None
                ),
                startup_timeout=(
                    _WORKER_STARTUP_TIMEOUT if item.spec.timeout is not None else None
                ),
            )
            for item in shard.items
        )
        for shard in shards
    )
    executions = supervisor.execute_shards(work_shards)
    results: list[TestResult] = []
    for shard, shard_executions in zip(shards, executions, strict=True):
        for item, execution in zip(shard.items, shard_executions, strict=True):
            value = execution.value
            if (
                isinstance(value, AttemptResult)
                and value.test_id == item.spec.id
                and value.attempt == item.attempt
            ):
                result = TestResult(
                    test=item.spec,
                    status=value.status,
                    attempts=(value,),
                    duration=value.duration,
                )
                results.append(_merge_outer_output(result, execution))
            else:
                results.append(_failure_for_execution(item.spec, item.attempt, execution))
    return tuple(results), len(work_shards)


def _emit_attempt(factory: EventFactory, sink: EventSink, result: TestResult) -> None:
    attempt = result.attempts[-1]
    # The coordinator receives an already complete attempt. One self-contained
    # event preserves every phase for replay without emitting a redundant
    # ATTEMPT_STARTED + PHASE_FINISHED sequence after the work has finished.
    sink.emit(
        factory.create(
            EventType.ATTEMPT_FINISHED,
            test_id=result.test.id,
            attempt=attempt.attempt,
            worker_id=attempt.worker_id,
            timestamp=attempt.finished_at,
            payload={"attempt_result": attempt},
        )
    )


def _should_retry(
    status: Status,
    *,
    user_retries_left: int,
    infrastructure_recovery_used: bool,
) -> tuple[bool, bool, bool]:
    """Return retry, consume-user-budget, consume-infrastructure-recovery."""

    if status in _AUTOMATIC_RECOVERY_STATUSES and not infrastructure_recovery_used:
        return True, False, True
    if status in _RETRYABLE_TEST_STATUSES | _AUTOMATIC_RECOVERY_STATUSES and user_retries_left > 0:
        return True, True, False
    return False, False, False


def run(
    paths: Sequence[str] | str | None = None,
    config: TestenixConfig | None = None,
    *,
    event_sink: EventSink | None = None,
    sharding_policy: ShardingPolicy | None = None,
    trusted_manifest: TrustedCollectionManifest | None = None,
) -> RunResult:
    """Discover and execute a native Testenix suite.

    The coordinator is the only component that decides final outcomes. Workers
    return immutable attempts, which are emitted to a versioned event stream and
    reduced after execution.
    """

    return _run(
        paths,
        config,
        event_sink=event_sink,
        sharding_policy=sharding_policy,
        trusted_manifest=trusted_manifest,
    )


def _run(
    paths: Sequence[str] | str | None,
    config: TestenixConfig | None,
    *,
    event_sink: EventSink | None,
    sharding_policy: ShardingPolicy | None = None,
    trusted_manifest: TrustedCollectionManifest | None = None,
    supervisor: ProcessSupervisor | None = None,
) -> RunResult:
    effective_config = config or TestenixConfig()
    policy = (
        ShardingPolicy(intra_module=effective_config.shard_modules)
        if sharding_policy is None
        else sharding_policy
    )
    if not isinstance(policy, ShardingPolicy):
        raise TypeError("sharding_policy must be a ShardingPolicy or None")
    active_trusted_manifest = trusted_manifest
    if active_trusted_manifest is None and effective_config.manifest_path is not None:
        try:
            active_trusted_manifest = deserialize_trusted_collection_manifest(
                effective_config.manifest_path.read_bytes()
            )
        except OSError as error:
            raise CollectionManifestError(
                f"cannot read collection manifest {effective_config.manifest_path}: {error}"
            ) from error
    if paths is None:
        effective_paths = effective_config.paths
    elif isinstance(paths, str):
        effective_paths = (paths,)
    else:
        effective_paths = tuple(paths)
    if not effective_paths:
        effective_paths = effective_config.paths
    active_supervisor = supervisor or ProcessSupervisor(start_method="spawn")

    run_id = uuid.uuid4().hex
    started_at = time.time()
    factory = EventFactory(run_id)
    memory_sink = InMemoryEventSink()
    log_path = _event_log_path(effective_config, run_id)
    log_sink = JsonlEventSink(log_path) if log_path is not None else None
    sink = _RunEventSink(memory_sink, log_sink, event_sink)

    sink.emit(
        factory.create(
            EventType.RUN_STARTED,
            timestamp=started_at,
            payload={"started_at": started_at},
        )
    )
    sink.emit(factory.create(EventType.COLLECTION_STARTED, payload={"paths": effective_paths}))
    collection = _verified_trusted_collection(active_trusted_manifest, effective_paths)
    if collection is None:
        collection = _collect_in_worker(
            effective_paths,
            active_supervisor,
            analyse_sharding=policy.intra_module,
        )
    selected = _effective_specs(collection.tests, effective_config)
    issues = list(collection.issues)
    if not collection.tests and not issues:
        issues.append(_collection_issue_for_empty(effective_paths))
    elif collection.tests and effective_config.tags and not selected:
        issues.append(_collection_issue_for_tags(effective_paths, effective_config.tags))
    for issue in issues:
        sink.emit(factory.create(EventType.COLLECTION_ERROR, payload={"issue": issue}))
    for spec in collection.tests:
        sink.emit(
            factory.create(
                EventType.TEST_DISCOVERED,
                test_id=spec.id,
                payload={"test": spec},
            )
        )
    sink.emit(
        factory.create(
            EventType.COLLECTION_FINISHED,
            payload={"tests": len(collection.tests), "issues": len(issues)},
        )
    )

    selection_events_required = bool(effective_config.tags) or any(
        selected_spec is not discovered_spec
        for selected_spec, discovered_spec in zip(selected, collection.tests, strict=True)
    )
    if selection_events_required:
        selected_by_id = {spec.id: spec for spec in selected}
        for spec in collection.tests:
            selected_spec = selected_by_id.get(spec.id)
            if selected_spec is not None:
                payload = {"test": selected_spec} if selected_spec != spec else {}
                sink.emit(
                    factory.create(
                        EventType.TEST_SELECTED,
                        test_id=spec.id,
                        payload=payload,
                    )
                )
            else:
                sink.emit(
                    factory.create(
                        EventType.TEST_EXCLUDED,
                        test_id=spec.id,
                        payload={
                            "reason": "test does not contain every required tag",
                            "required_tags": effective_config.tags,
                        },
                    )
                )

    attempts_by_test: dict[str, list[AttemptResult]] = {spec.id: [] for spec in selected}
    order = {spec.id: index for index, spec in enumerate(selected)}
    user_retries_left = {spec.id: effective_config.retries for spec in selected}
    infrastructure_recovery_used = {spec.id: False for spec in selected}
    worker_limit = 0
    workers_used = 0
    shardable_paths = _NO_SHARDABLE_PATHS

    if selected:
        durations = _duration_history(effective_config, selected)
        shardable_paths = (
            frozenset(decision.path for decision in collection.sharding if decision.eligible)
            if policy.intra_module
            else _NO_SHARDABLE_PATHS
        )
        schedulable_units = _execution_units(
            selected,
            durations,
            shardable_paths=shardable_paths,
        )
        worker_limit = min(
            len(schedulable_units),
            effective_config.resolve_workers(
                selected,
                durations,
                spawn_method=cast("SpawnMethod", active_supervisor.start_method),
                shardable_paths=shardable_paths,
            ),
        )
        first_results, initial_workers_used = _execute_initial_attempts(
            selected,
            worker_count=worker_limit,
            durations=durations,
            supervisor=active_supervisor,
            shardable_paths=shardable_paths,
        )
        workers_used = max(workers_used, initial_workers_used)
        first_results = tuple(sorted(first_results, key=lambda result: order[result.test.id]))
        for result in first_results:
            attempts_by_test[result.test.id].append(result.attempts[-1])
            _emit_attempt(factory, sink, result)

        latest = {result.test.id: result for result in first_results}
        pending: list[_PendingAttempt] = []
        for spec in selected:
            status = latest[spec.id].status
            retry, consume_user, consume_infra = _should_retry(
                status,
                user_retries_left=user_retries_left[spec.id],
                infrastructure_recovery_used=infrastructure_recovery_used[spec.id],
            )
            if retry:
                if consume_user:
                    user_retries_left[spec.id] -= 1
                if consume_infra:
                    infrastructure_recovery_used[spec.id] = True
                pending.append(_PendingAttempt(spec, 2))

        while pending:
            retry_results, retry_workers_used = _execute_retry_attempts(
                pending,
                worker_count=worker_limit,
                durations=durations,
                supervisor=active_supervisor,
            )
            workers_used = max(workers_used, retry_workers_used)
            retry_results = tuple(sorted(retry_results, key=lambda result: order[result.test.id]))
            next_pending: list[_PendingAttempt] = []
            for result in retry_results:
                test_id = result.test.id
                attempt = result.attempts[-1]
                attempts_by_test[test_id].append(attempt)
                _emit_attempt(factory, sink, result)
                retry, consume_user, consume_infra = _should_retry(
                    result.status,
                    user_retries_left=user_retries_left[test_id],
                    infrastructure_recovery_used=infrastructure_recovery_used[test_id],
                )
                if retry:
                    if consume_user:
                        user_retries_left[test_id] -= 1
                    if consume_infra:
                        infrastructure_recovery_used[test_id] = True
                    next_pending.append(_PendingAttempt(result.test, attempt.attempt + 1))
            pending = next_pending

    for spec in selected:
        attempts = tuple(sorted(attempts_by_test[spec.id], key=lambda item: item.attempt))
        status = finalize_status(attempts)
        sink.emit(
            factory.create(
                EventType.TEST_FINALIZED,
                test_id=spec.id,
                payload={"status": status},
            )
        )

    finished_at = time.time()
    try:
        sink.emit(
            factory.create(
                EventType.RUN_FINISHED,
                timestamp=finished_at,
                payload={
                    "finished_at": finished_at,
                    "workers_used": workers_used,
                    "shardable_paths": tuple(sorted(shardable_paths)),
                },
            )
        )
    finally:
        sink.close()
    run_result = replace(
        reduce_events(memory_sink.events, run_id=run_id),
        workers_used=workers_used,
        shardable_paths=tuple(sorted(shardable_paths)),
    )
    if effective_config.history_path is not None:
        with HistoryStore(effective_config.history_path) as history:
            history.record_run(run_result)
    return run_result


async def run_async(
    paths: Sequence[str] | str | None = None,
    config: TestenixConfig | None = None,
    *,
    event_sink: EventSink | None = None,
    sharding_policy: ShardingPolicy | None = None,
    trusted_manifest: TrustedCollectionManifest | None = None,
) -> RunResult:
    """Cancellable embedding facade around the process-oriented coordinator."""

    supervisor = ProcessSupervisor(start_method="spawn")
    task = asyncio.create_task(
        asyncio.to_thread(
            _run,
            paths,
            config,
            event_sink=event_sink,
            sharding_policy=sharding_policy,
            trusted_manifest=trusted_manifest,
            supervisor=supervisor,
        )
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        supervisor.cancel_all()
        # The worker threads must observe cancellation and reap their children
        # before this facade hands control back to an embedding event loop.
        with suppress(asyncio.CancelledError):
            await asyncio.shield(task)
        raise


__all__ = ["collect_trusted_manifest", "run", "run_async"]
