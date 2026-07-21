"""Pure, deterministic reduction of immutable runtime events.

Workers report facts.  This module owns the final verdict, including the
important distinction between a flaky test (test failure followed by success)
and an infrastructure retry (worker failure followed by success).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

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
from testenix.events import event_from_dict, serialize_event

INFRASTRUCTURE_STATUSES = frozenset({Status.INFRA_ERROR})
TEST_FAILURE_STATUSES = frozenset(
    {
        Status.FAIL,
        Status.ERROR_SETUP,
        Status.ERROR_TEARDOWN,
        Status.XPASS,
        Status.TIMEOUT,
        Status.CRASH,
        Status.FLAKY,
    }
)
_PHASE_ORDER = {Phase.SETUP: 0, Phase.CALL: 1, Phase.TEARDOWN: 2}


class EventReductionError(ValueError):
    """Raised when an event stream contradicts its immutable contracts."""


class DuplicateEventConflictError(EventReductionError):
    """The same event id was reused for two different facts."""


def _status(value: Any, default: Status | None = None) -> Status | None:
    if value is None:
        return default
    if isinstance(value, Status):
        return value
    try:
        return Status(str(value))
    except ValueError:
        return default


def _finite_number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _duration(value: Any, default: float = 0.0) -> float:
    return max(0.0, _finite_number(value, default))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _test_spec(value: Any, *, fallback_id: str | None = None) -> TestSpec | None:
    if isinstance(value, TestSpec):
        return value
    data = _mapping(value)
    if not data:
        return None
    test_id = data.get("id", fallback_id)
    if test_id is None:
        return None
    test_id = str(test_id)
    path = str(data.get("path", ""))
    function_name = str(data.get("function_name", test_id))
    display_name = str(data.get("display_name", function_name))
    raw_parameters = data.get("parameters", {})
    parameters = dict(raw_parameters) if isinstance(raw_parameters, Mapping) else {}
    raw_tags = data.get("tags", ())
    if isinstance(raw_tags, str):
        tags = frozenset({raw_tags})
    elif isinstance(raw_tags, Iterable):
        tags = frozenset(str(tag) for tag in raw_tags)
    else:
        tags = frozenset()
    source_line = data.get("source_line")
    try:
        parsed_line = None if source_line is None else int(source_line)
    except (TypeError, ValueError):
        parsed_line = None
    timeout = data.get("timeout")
    parsed_timeout = None if timeout is None else _duration(timeout)
    return TestSpec(
        id=test_id,
        path=path,
        module_name=str(data.get("module_name", "")),
        function_name=function_name,
        display_name=display_name,
        parameters=parameters,
        case_id=None if data.get("case_id") is None else str(data["case_id"]),
        tags=tags,
        skip_reason=(None if data.get("skip_reason") is None else str(data.get("skip_reason"))),
        xfail_reason=(None if data.get("xfail_reason") is None else str(data.get("xfail_reason"))),
        timeout=parsed_timeout,
        source_line=parsed_line,
    )


def _placeholder_test(test_id: str) -> TestSpec:
    return TestSpec(
        id=test_id,
        path="",
        module_name="",
        function_name=test_id,
        display_name=test_id,
    )


def _phase_result(value: Any, *, fallback_phase: Any = None) -> PhaseResult | None:
    if isinstance(value, PhaseResult):
        return value
    data = _mapping(value)
    raw_phase = data.get("phase", fallback_phase)
    try:
        phase = raw_phase if isinstance(raw_phase, Phase) else Phase(str(raw_phase))
    except ValueError:
        return None
    status = _status(data.get("status"), Status.PASS)
    assert status is not None
    return PhaseResult(
        phase=phase,
        status=status,
        duration=_duration(data.get("duration")),
        message=None if data.get("message") is None else str(data["message"]),
        exception_type=(
            None if data.get("exception_type") is None else str(data["exception_type"])
        ),
        traceback=None if data.get("traceback") is None else str(data["traceback"]),
        stdout=str(data.get("stdout", "")),
        stderr=str(data.get("stderr", "")),
    )


def _attempt_result(value: Any) -> AttemptResult | None:
    if isinstance(value, AttemptResult):
        return value
    data = _mapping(value)
    if not data or data.get("test_id") is None or data.get("attempt") is None:
        return None
    raw_phases = data.get("phases", ())
    phases = tuple(
        phase
        for phase in (_phase_result(raw_phase) for raw_phase in raw_phases)
        if phase is not None
    )
    status = _status(data.get("status")) or _infer_attempt_status(phases)
    started_at = _finite_number(data.get("started_at"))
    duration = _duration(data.get("duration"), sum(phase.duration for phase in phases))
    finished_at = _finite_number(data.get("finished_at"), started_at + duration)
    return AttemptResult(
        test_id=str(data["test_id"]),
        attempt=max(1, int(data["attempt"])),
        worker_id=str(data.get("worker_id", "unknown")),
        status=status,
        duration=duration,
        phases=phases,
        started_at=started_at,
        finished_at=finished_at,
    )


def _infer_attempt_status(phases: Sequence[PhaseResult]) -> Status:
    if not phases:
        return Status.INFRA_ERROR
    setup_failures = [
        result.status
        for result in phases
        if result.phase is Phase.SETUP and result.status is not Status.PASS
    ]
    if setup_failures:
        setup = setup_failures[-1]
        if setup in {Status.SKIP, Status.XFAIL, Status.CANCELLED}:
            return setup
        return Status.ERROR_SETUP
    teardown_failures = [
        result.status
        for result in phases
        if result.phase is Phase.TEARDOWN and result.status is not Status.PASS
    ]
    if teardown_failures:
        teardown = teardown_failures[-1]
        if teardown in {Status.CRASH, Status.TIMEOUT, Status.INFRA_ERROR}:
            return teardown
        return Status.ERROR_TEARDOWN
    calls = [result.status for result in phases if result.phase is Phase.CALL]
    if calls:
        return calls[-1]
    return Status.PASS


def finalize_status(
    attempts: Sequence[AttemptResult],
    *,
    explicit_status: Status | None = None,
) -> Status:
    """Compute a test verdict without letting infrastructure retries create flakiness.

    ``FAIL -> PASS`` is ``FLAKY``.  ``INFRA_ERROR -> PASS`` and
    ``CRASH -> PASS`` are plain ``PASS`` because no test failure was observed.
    If a retry is lost after a real result, the last real test result still wins.
    """

    if not attempts:
        return explicit_status or Status.NOT_RUN

    ordered = sorted(attempts, key=lambda result: result.attempt)
    substantive = [attempt for attempt in ordered if attempt.status not in INFRASTRUCTURE_STATUSES]
    if not substantive:
        # A controller's optimistic TEST_FINALIZED event cannot erase the fact
        # that no test attempt completed. Infrastructure truth wins here.
        return ordered[-1].status

    last = substantive[-1]
    if last.status in {Status.PASS, Status.CACHED_PASS}:
        previous_test_failure = any(
            attempt.status in TEST_FAILURE_STATUSES for attempt in substantive[:-1]
        )
        if previous_test_failure:
            return Status.FLAKY
    if explicit_status is Status.FLAKY:
        return Status.FLAKY
    return last.status


@dataclass(slots=True)
class _AttemptBuilder:
    test_id: str
    attempt: int
    worker_id: str = "unknown"
    status: Status | None = None
    duration: float | None = None
    started_at: float | None = None
    finished_at: float | None = None
    first_timestamp: float | None = None
    last_timestamp: float | None = None
    # Keep a multiset rather than Phase -> result: several fixture finalizers can
    # fail and every error is part of the lossless outcome.
    phases: list[tuple[bool, int, int, PhaseResult]] = field(default_factory=list)
    _phase_index: int = 0
    worker_lost_status: Status | None = None

    def observe(self, event: Event) -> None:
        if event.worker_id:
            self.worker_id = event.worker_id
        if self.first_timestamp is None or event.timestamp < self.first_timestamp:
            self.first_timestamp = event.timestamp
        if self.last_timestamp is None or event.timestamp > self.last_timestamp:
            self.last_timestamp = event.timestamp

    def add_phase(self, result: PhaseResult, event: Event, *, direct: bool) -> None:
        self._phase_index += 1
        self.phases.append((direct, event.sequence, self._phase_index, result))

    def merge_result(self, result: AttemptResult, event: Event) -> None:
        self.worker_id = result.worker_id or self.worker_id
        self.status = result.status
        self.duration = result.duration
        self.started_at = result.started_at
        self.finished_at = result.finished_at
        for phase in result.phases:
            self.add_phase(phase, event, direct=False)

    def build(self) -> AttemptResult:
        direct = [fact for fact in self.phases if fact[0]]
        summaries = [fact for fact in self.phases if not fact[0]]

        # ATTEMPT_FINISHED commonly repeats phase facts already emitted as
        # PHASE_FINISHED. Match copies one-for-one, so two equal finalizer errors
        # still survive as two separate facts.
        unmatched_summaries = list(summaries)
        for direct_fact in direct:
            for index, summary_fact in enumerate(unmatched_summaries):
                if summary_fact[3] == direct_fact[3]:
                    unmatched_summaries.pop(index)
                    break
        phase_facts = direct + unmatched_summaries
        phase_facts.sort(key=lambda fact: (_PHASE_ORDER[fact[3].phase], fact[1], fact[2]))
        phases = tuple(fact[3] for fact in phase_facts)
        duration = self.duration
        if duration is None:
            duration = sum(phase.duration for phase in phases)
            if not phases and self.started_at is not None and self.finished_at is not None:
                duration = max(0.0, self.finished_at - self.started_at)
        started_at = self.started_at
        if started_at is None:
            anchor = self.first_timestamp if self.first_timestamp is not None else 0.0
            started_at = anchor
        finished_at = self.finished_at
        if finished_at is None:
            finished_at = self.last_timestamp
            if finished_at is None or finished_at < started_at:
                finished_at = started_at + duration
        return AttemptResult(
            test_id=self.test_id,
            attempt=self.attempt,
            worker_id=self.worker_id,
            status=self.worker_lost_status or self.status or _infer_attempt_status(phases),
            duration=_duration(duration),
            phases=phases,
            started_at=started_at,
            finished_at=finished_at,
        )


def _canonical_events(events: Iterable[Event | Mapping[str, Any]]) -> tuple[Event, ...]:
    by_id: dict[str, Event] = {}
    ordered = True
    previous_key: tuple[int, float, str] | None = None
    for raw_event in events:
        event = raw_event if isinstance(raw_event, Event) else event_from_dict(raw_event)
        existing = by_id.get(event.event_id)
        if existing is not None:
            # Unique event IDs are overwhelmingly the hot path. Defer the
            # canonical JSON work until a duplicate actually needs semantic
            # comparison instead of serializing every event in every run.
            if existing != event and serialize_event(existing) != serialize_event(event):
                raise DuplicateEventConflictError(
                    f"event_id {event.event_id!r} identifies different facts"
                )
            continue
        by_id[event.event_id] = event
        key = (event.sequence, event.timestamp, event.event_id)
        if previous_key is not None and key < previous_key:
            ordered = False
        previous_key = key

    canonical = tuple(by_id.values())
    if ordered:
        return canonical
    return tuple(
        sorted(
            canonical,
            key=lambda event: (event.sequence, event.timestamp, event.event_id),
        )
    )


def _payload_value(payload: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in payload:
            return payload[name]
    return payload


def _collection_issue(payload: Mapping[str, Any]) -> CollectionIssue:
    raw_issue = payload.get("issue", payload)
    if isinstance(raw_issue, CollectionIssue):
        return raw_issue
    data = _mapping(raw_issue)
    return CollectionIssue(
        path=str(data.get("path", "")),
        message=str(data.get("message", "collection failed")),
        traceback=None if data.get("traceback") is None else str(data["traceback"]),
    )


def reduce_events(
    events: Iterable[Event | Mapping[str, Any]],
    *,
    run_id: str | None = None,
) -> RunResult:
    """Reduce an event stream to a stable :class:`~testenix.contracts.RunResult`.

    Input order does not matter. Duplicate deliveries with the same ``event_id``
    are ignored; conflicting reuse of an id is rejected rather than resolved by
    arrival order.
    """

    canonical = _canonical_events(events)
    observed_run_ids = {event.run_id for event in canonical}
    if run_id is not None:
        observed_run_ids.add(run_id)
    if not observed_run_ids:
        raise EventReductionError("cannot infer run_id from an empty event stream")
    if len(observed_run_ids) != 1:
        raise EventReductionError(f"event stream contains multiple runs: {observed_run_ids}")
    resolved_run_id = next(iter(observed_run_ids))

    specs: dict[str, TestSpec] = {}
    discovery_order: dict[str, tuple[int, str]] = {}
    selected_order: dict[str, tuple[int, str]] = {}
    explicit_statuses: dict[str, Status] = {}
    attempts: dict[tuple[str, int], _AttemptBuilder] = {}
    collection_issues: list[CollectionIssue] = []
    selection_observed = False
    started_at: float | None = None
    finished_at: float | None = None
    workers_used: int | None = None
    shardable_paths: tuple[str, ...] = ()

    def remember_spec(
        spec: TestSpec | None, event: Event, *, replace_existing: bool = False
    ) -> None:
        if spec is None:
            return
        if replace_existing or spec.id not in specs:
            specs[spec.id] = spec
        discovery_order.setdefault(spec.id, (event.sequence, event.event_id))

    def builder_for(test_id: str, attempt: int, event: Event) -> _AttemptBuilder:
        builder = attempts.setdefault((test_id, attempt), _AttemptBuilder(test_id, attempt))
        builder.observe(event)
        return builder

    for event in canonical:
        payload = event.payload
        if event.event_type is EventType.RUN_STARTED:
            candidate = _finite_number(payload.get("started_at"), event.timestamp)
            started_at = candidate if started_at is None else min(started_at, candidate)
            continue
        if event.event_type is EventType.RUN_FINISHED:
            candidate = _finite_number(payload.get("finished_at"), event.timestamp)
            finished_at = candidate if finished_at is None else max(finished_at, candidate)
            raw_workers = payload.get("workers_used")
            if (
                isinstance(raw_workers, int)
                and not isinstance(raw_workers, bool)
                and raw_workers >= 0
            ):
                workers_used = raw_workers
            raw_shardable = payload.get("shardable_paths", ())
            if isinstance(raw_shardable, Iterable) and not isinstance(raw_shardable, (str, bytes)):
                shardable_paths = tuple(sorted(str(path) for path in raw_shardable))
            continue
        if event.event_type is EventType.COLLECTION_ERROR:
            collection_issues.append(_collection_issue(payload))
            continue
        if event.event_type is EventType.TEST_DISCOVERED:
            raw_spec = _payload_value(payload, "test", "spec")
            remember_spec(_test_spec(raw_spec, fallback_id=event.test_id), event)
            continue
        if event.event_type is EventType.TEST_SELECTED:
            selection_observed = True
            if event.test_id is not None:
                selected_order.setdefault(event.test_id, (event.sequence, event.event_id))
            # Selection may apply runtime defaults (for example a global
            # timeout). The selected contract is the one reporters and history
            # must retain, not the earlier raw discovery snapshot.
            if "test" in payload or "spec" in payload:
                raw_spec = _payload_value(payload, "test", "spec")
                remember_spec(
                    _test_spec(raw_spec, fallback_id=event.test_id),
                    event,
                    replace_existing=True,
                )
            continue
        if event.event_type is EventType.TEST_EXCLUDED:
            selection_observed = True
            continue
        if event.event_type is EventType.TEST_FINALIZED:
            raw_result = payload.get("result")
            if isinstance(raw_result, TestResult):
                remember_spec(raw_result.test, event)
                explicit_statuses[raw_result.test.id] = raw_result.status
                for result in raw_result.attempts:
                    builder_for(result.test_id, result.attempt, event).merge_result(result, event)
                continue
            if isinstance(raw_result, Mapping):
                remember_spec(_test_spec(raw_result.get("test"), fallback_id=event.test_id), event)
            if event.test_id is not None:
                parsed = _status(payload.get("status"))
                if parsed is not None:
                    explicit_statuses[event.test_id] = parsed
            continue

        if event.event_type not in {
            EventType.ATTEMPT_STARTED,
            EventType.PHASE_FINISHED,
            EventType.ATTEMPT_FINISHED,
            EventType.WORKER_LOST,
        }:
            continue

        target_ids: list[str] = []
        if event.test_id is not None:
            target_ids.append(event.test_id)
        elif event.event_type is EventType.WORKER_LOST:
            raw_ids = payload.get("test_ids", ())
            if isinstance(raw_ids, Iterable) and not isinstance(raw_ids, (str, bytes)):
                target_ids.extend(str(test_id) for test_id in raw_ids)
        if not target_ids:
            continue

        attempt_number = event.attempt
        if attempt_number is None:
            attempt_number = max(1, int(payload.get("attempt", 1)))

        for test_id in target_ids:
            builder = builder_for(test_id, attempt_number, event)
            if event.event_type is EventType.ATTEMPT_STARTED:
                builder.started_at = _finite_number(payload.get("started_at"), event.timestamp)
            elif event.event_type is EventType.PHASE_FINISHED:
                raw_phase = _payload_value(payload, "phase_result", "result")
                phase = _phase_result(raw_phase, fallback_phase=payload.get("phase"))
                if phase is not None:
                    builder.add_phase(phase, event, direct=True)
            elif event.event_type is EventType.ATTEMPT_FINISHED:
                raw_result = _payload_value(payload, "attempt_result", "result")
                parsed_result = _attempt_result(raw_result)
                if parsed_result is not None:
                    builder.merge_result(parsed_result, event)
                else:
                    builder.status = _status(payload.get("status"), builder.status)
                    if payload.get("duration") is not None:
                        builder.duration = _duration(payload.get("duration"))
                    builder.finished_at = _finite_number(
                        payload.get("finished_at"), event.timestamp
                    )
                    raw_phases = payload.get("phases", ())
                    if isinstance(raw_phases, Iterable):
                        for raw_phase in raw_phases:
                            phase = _phase_result(raw_phase)
                            if phase is not None:
                                builder.add_phase(phase, event, direct=False)
            else:  # WORKER_LOST
                builder.worker_lost_status = _status(payload.get("status"), Status.CRASH)
                builder.finished_at = event.timestamp
                if builder.started_at is None:
                    builder.started_at = _finite_number(payload.get("started_at"), event.timestamp)
                builder.duration = _duration(
                    payload.get("duration"), event.timestamp - builder.started_at
                )

    if started_at is None:
        started_at = min((event.timestamp for event in canonical), default=0.0)
    if finished_at is None:
        finished_at = max((event.timestamp for event in canonical), default=started_at)
    if finished_at < started_at:
        finished_at = started_at

    built_attempts: dict[str, list[AttemptResult]] = {}
    for builder in attempts.values():
        built_attempts.setdefault(builder.test_id, []).append(builder.build())
    for attempt_results in built_attempts.values():
        attempt_results.sort(key=lambda result: result.attempt)

    if selection_observed:
        included_ids = set(selected_order)
    else:
        included_ids = set(specs) | set(built_attempts) | set(explicit_statuses)

    def test_order(test_id: str) -> tuple[int, int, str, int, str]:
        if test_id in discovery_order:
            event_order = discovery_order[test_id]
            group = 0
        elif test_id in selected_order:
            event_order = selected_order[test_id]
            group = 1
        else:
            event_order = (2**63 - 1, test_id)
            group = 2
        spec = specs.get(test_id, _placeholder_test(test_id))
        return (group, event_order[0], spec.path, spec.source_line or 0, test_id)

    test_results: list[TestResult] = []
    for test_id in sorted(included_ids, key=test_order):
        spec = specs.get(test_id, _placeholder_test(test_id))
        test_attempts = tuple(built_attempts.get(test_id, ()))
        explicit = explicit_statuses.get(test_id)
        if not test_attempts and explicit is None and spec.skip_reason is not None:
            explicit = Status.SKIP
        status = finalize_status(test_attempts, explicit_status=explicit)
        test_results.append(
            TestResult(
                test=spec,
                status=status,
                attempts=test_attempts,
                duration=sum(attempt.duration for attempt in test_attempts),
            )
        )

    return RunResult(
        run_id=resolved_run_id,
        tests=tuple(test_results),
        collection_issues=tuple(collection_issues),
        started_at=started_at,
        finished_at=finished_at,
        workers_used=workers_used,
        shardable_paths=shardable_paths,
    )


@dataclass(frozen=True, slots=True)
class EventReducer:
    """Configured callable facade over the pure :func:`reduce_events` function."""

    run_id: str | None = None

    def reduce(self, events: Iterable[Event | Mapping[str, Any]]) -> RunResult:
        return reduce_events(events, run_id=self.run_id)

    __call__ = reduce


aggregate_events = reduce_events
reduce = reduce_events


__all__ = [
    "DuplicateEventConflictError",
    "EventReducer",
    "EventReductionError",
    "INFRASTRUCTURE_STATUSES",
    "TEST_FAILURE_STATUSES",
    "aggregate_events",
    "finalize_status",
    "reduce",
    "reduce_events",
]
