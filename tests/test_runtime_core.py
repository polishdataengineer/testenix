from __future__ import annotations

import multiprocessing as mp
import os
import time
from pathlib import Path

import pytest

from testenix.aggregate import DuplicateEventConflictError, finalize_status, reduce_events
from testenix.contracts import (
    AttemptResult,
    EventType,
    Phase,
    PhaseResult,
    RunResult,
    Status,
)
from testenix.contracts import TestResult as DomainResult
from testenix.contracts import TestSpec as Spec
from testenix.events import (
    EventFactory,
    FanoutEventSink,
    InMemoryEventSink,
    JsonlEventSink,
    UnsupportedEventSchemaError,
    deserialize_event,
    read_events,
    serialize_event,
)
from testenix.history import HistoryStore
from testenix.scheduler import schedule_lpt
from testenix.worker import ProcessSupervisor, WorkItem


def _return_success() -> None:
    print("child output")


async def _return_async_success() -> Status:
    return Status.PASS


def _raise_boundary_error() -> None:
    raise RuntimeError("adapter exploded")


def _crash_process() -> None:
    os._exit(17)


def _sleep_for(seconds: float) -> None:
    time.sleep(seconds)


def _return_large_payload() -> bytes:
    return b"x" * 1_000_000


def _spec(test_id: str, path: str | None = None) -> Spec:
    return Spec(
        id=test_id,
        path=path or f"tests/{test_id}.py",
        module_name=f"tests.{test_id}",
        function_name=test_id,
        display_name=test_id,
    )


def _attempt(
    test_id: str,
    attempt: int,
    status: Status,
    duration: float = 0.1,
) -> AttemptResult:
    phase = PhaseResult(Phase.CALL, status, duration)
    return AttemptResult(
        test_id=test_id,
        attempt=attempt,
        worker_id="worker-1",
        status=status,
        duration=duration,
        phases=(phase,),
        started_at=float(attempt),
        finished_at=float(attempt) + duration,
    )


def test_event_factory_default_ids_use_run_and_sequence() -> None:
    factory = EventFactory("stable-run")

    first = factory.create(EventType.RUN_STARTED)
    second = factory.create(EventType.RUN_FINISHED)

    assert first.event_id == "stable-run:1"
    assert second.event_id == "stable-run:2"


def test_event_factory_and_jsonl_are_versioned_append_only(tmp_path: Path) -> None:
    ids = iter(("event-1", "event-2"))
    timestamps = iter((10.0, 11.0))
    factory = EventFactory(
        "run-1",
        id_factory=lambda: next(ids),
        clock=lambda: next(timestamps),
    )
    sink = JsonlEventSink(tmp_path / "nested" / "events.jsonl")
    test = _spec("test_safe")

    first = factory.create(EventType.RUN_STARTED, payload={"test": test})
    second = factory.create(
        EventType.TEST_DISCOVERED,
        test_id=test.id,
        payload={"test": test, "tags": {"slow", "unit"}, "opaque": object()},
    )
    sink.emit(first)
    sink.emit(second)

    restored = tuple(read_events(sink.path))
    assert [event.event_id for event in restored] == ["event-1", "event-2"]
    assert [event.sequence for event in restored] == [1, 2]
    assert restored[1].payload["test"]["id"] == "test_safe"
    assert restored[1].payload["tags"] == ["slow", "unit"]
    assert restored[1].payload["opaque"]["__testenix_type__"] == "builtins.object"
    assert sink.path.read_text(encoding="utf-8").count("\n") == 2


def test_event_reader_rejects_unknown_schema() -> None:
    event = EventFactory("run", schema_version=99).create(EventType.RUN_STARTED)

    with pytest.raises(UnsupportedEventSchemaError):
        deserialize_event(serialize_event(event))


def test_fanout_gives_every_sink_an_isolated_canonical_event() -> None:
    original = EventFactory("run").create(
        EventType.RUN_STARTED,
        payload={"nested": {"value": "original"}},
    )
    memory = InMemoryEventSink()

    class MutatingSink:
        def emit(self, event):  # type: ignore[no-untyped-def]
            event.payload["nested"]["value"] = "mutated"
            return event

    fanout = FanoutEventSink(MutatingSink(), memory)

    returned = fanout.emit(original)

    assert returned is original
    assert original.payload["nested"]["value"] == "original"
    assert memory.events[0] is not original
    assert memory.events[0].payload["nested"]["value"] == "original"


def test_reducer_is_deterministic_deduplicates_and_marks_test_flaky() -> None:
    factory = EventFactory("run", id_factory=(f"event-{index}" for index in range(20)).__next__)
    spec = _spec("test_flaky")
    events = [
        factory.create(EventType.RUN_STARTED, timestamp=1.0),
        factory.create(
            EventType.TEST_DISCOVERED,
            test_id=spec.id,
            timestamp=1.1,
            payload={"test": spec},
        ),
        factory.create(EventType.TEST_SELECTED, test_id=spec.id, timestamp=1.2),
        factory.create(
            EventType.ATTEMPT_FINISHED,
            test_id=spec.id,
            attempt=1,
            timestamp=2.0,
            payload={"attempt_result": _attempt(spec.id, 1, Status.FAIL)},
        ),
        factory.create(
            EventType.ATTEMPT_FINISHED,
            test_id=spec.id,
            attempt=2,
            timestamp=3.0,
            payload={"attempt_result": _attempt(spec.id, 2, Status.PASS)},
        ),
        factory.create(EventType.RUN_FINISHED, timestamp=4.0),
    ]

    forward = reduce_events(events + [events[3]])
    reverse = reduce_events(reversed(events))

    assert forward == reverse
    assert forward.tests[0].status is Status.FLAKY
    assert [attempt.status for attempt in forward.tests[0].attempts] == [
        Status.FAIL,
        Status.PASS,
    ]
    assert forward.exit_code == 1


def test_reducer_rejects_conflicting_duplicate_event_ids() -> None:
    factory = EventFactory("run")
    first = factory.create(EventType.RUN_STARTED, event_id="same", timestamp=1.0)
    second = factory.create(EventType.RUN_FINISHED, event_id="same", timestamp=2.0)

    with pytest.raises(DuplicateEventConflictError):
        reduce_events([first, second])


def test_reducer_does_not_reintroduce_tests_when_every_test_is_excluded() -> None:
    factory = EventFactory("run")
    spec = _spec("test_slow")
    events = (
        factory.create(EventType.RUN_STARTED, timestamp=1.0),
        factory.create(
            EventType.TEST_DISCOVERED,
            test_id=spec.id,
            payload={"test": spec},
        ),
        factory.create(
            EventType.TEST_EXCLUDED,
            test_id=spec.id,
            payload={"reason": "missing required tag"},
        ),
        factory.create(EventType.RUN_FINISHED, timestamp=2.0),
    )

    result = reduce_events(events)

    assert result.tests == ()
    assert result.exit_code == 0


def test_infrastructure_retry_is_not_flaky_but_process_crash_is() -> None:
    assert (
        finalize_status(
            (
                _attempt("test", 1, Status.INFRA_ERROR),
                _attempt("test", 2, Status.PASS),
            )
        )
        is Status.PASS
    )
    assert (
        finalize_status((_attempt("test", 1, Status.CRASH), _attempt("test", 2, Status.PASS)))
        is Status.FLAKY
    )


def test_reducer_preserves_multiple_teardown_errors_without_summary_copies() -> None:
    ids = (f"event-{index}" for index in range(20))
    factory = EventFactory("run", id_factory=ids.__next__)
    spec = _spec("test_finalizers")
    first_error = PhaseResult(
        Phase.TEARDOWN,
        Status.ERROR_TEARDOWN,
        0.01,
        message="database finalizer failed",
    )
    second_error = PhaseResult(
        Phase.TEARDOWN,
        Status.ERROR_TEARDOWN,
        0.02,
        message="server finalizer failed",
    )
    summary = AttemptResult(
        test_id=spec.id,
        attempt=1,
        worker_id="worker",
        status=Status.ERROR_TEARDOWN,
        duration=0.03,
        phases=(first_error, second_error),
        started_at=1.0,
        finished_at=1.03,
    )
    events = [
        factory.create(EventType.RUN_STARTED, timestamp=0.0),
        factory.create(
            EventType.TEST_DISCOVERED,
            test_id=spec.id,
            payload={"test": spec},
        ),
        factory.create(
            EventType.PHASE_FINISHED,
            test_id=spec.id,
            attempt=1,
            payload={"phase_result": first_error},
        ),
        factory.create(
            EventType.PHASE_FINISHED,
            test_id=spec.id,
            attempt=1,
            payload={"phase_result": second_error},
        ),
        factory.create(
            EventType.ATTEMPT_FINISHED,
            test_id=spec.id,
            attempt=1,
            payload={"attempt_result": summary},
        ),
    ]

    phases = reduce_events(events).tests[0].attempts[0].phases

    assert [phase.message for phase in phases] == [
        "database finalizer failed",
        "server finalizer failed",
    ]


def test_lpt_scheduler_uses_history_and_is_independent_of_input_order() -> None:
    tests = tuple(_spec(name) for name in ("a", "b", "c", "d"))
    history = {"a": 8.0, "b": 7.0, "c": 6.0, "d": 5.0}

    plan = schedule_lpt(tests, 2, history)
    reversed_plan = schedule_lpt(tuple(reversed(tests)), 2, history)

    assert [shard.test_ids for shard in plan] == [("a", "d"), ("b", "c")]
    assert [shard.estimated_duration for shard in plan] == [13.0, 13.0]
    assert plan == reversed_plan


def test_lpt_scheduler_has_deterministic_unknown_duration_and_empty_shards() -> None:
    tests = (_spec("z"), _spec("a"))

    plan = schedule_lpt(tests, 3)

    assert [shard.test_ids for shard in plan] == [("a",), ("z",), ()]
    assert [shard.estimated_duration for shard in plan] == [1.0, 1.0, 0.0]


def test_history_uses_last_substantive_attempt_and_ignores_pure_infra(
    tmp_path: Path,
) -> None:
    recovered = _spec("recovered")
    failed_then_crashed = _spec("failed-then-crashed")
    pure_crash = _spec("pure-crash")
    recovered_result = DomainResult(
        test=recovered,
        status=Status.PASS,
        attempts=(
            _attempt(recovered.id, 1, Status.INFRA_ERROR, 9.0),
            _attempt(recovered.id, 2, Status.PASS, 0.25),
        ),
        duration=9.25,
    )
    failed_result = DomainResult(
        test=failed_then_crashed,
        status=Status.FAIL,
        attempts=(
            _attempt(failed_then_crashed.id, 1, Status.FAIL, 0.4),
            _attempt(failed_then_crashed.id, 2, Status.CRASH, 8.0),
        ),
        duration=8.4,
    )
    crashed_result = DomainResult(
        test=pure_crash,
        status=Status.CRASH,
        attempts=(_attempt(pure_crash.id, 1, Status.CRASH, 12.0),),
        duration=12.0,
    )
    first_run = RunResult(
        run_id="first",
        tests=(recovered_result, failed_result, crashed_result),
        collection_issues=(),
        started_at=1.0,
        finished_at=2.0,
    )

    with HistoryStore(tmp_path / "history.sqlite3") as history:
        history.record_run(first_run)

        assert history.durations() == {
            failed_then_crashed.id: pytest.approx(0.4),
            recovered.id: pytest.approx(0.25),
        }
        assert history.get(pure_crash.id) is None
        assert history.recent_statuses(pure_crash.id) == (Status.CRASH,)

        second_run = RunResult(
            run_id="second",
            tests=(
                DomainResult(
                    test=recovered,
                    status=Status.INFRA_ERROR,
                    attempts=(_attempt(recovered.id, 1, Status.INFRA_ERROR, 20.0),),
                    duration=20.0,
                ),
            ),
            collection_issues=(),
            started_at=3.0,
            finished_at=4.0,
        )
        history.record_run(second_run)

        entry = history.get(recovered.id)
        assert entry is not None
        assert entry.duration == pytest.approx(0.25)
        assert entry.status is Status.INFRA_ERROR
        assert entry.samples == 1
        assert history.recent_statuses(recovered.id) == (
            Status.INFRA_ERROR,
            Status.PASS,
        )


def test_process_supervisor_supports_sync_async_and_boundary_errors() -> None:
    supervisor = ProcessSupervisor(default_timeout=1.0)

    success = supervisor.execute(WorkItem("sync", _return_success))
    async_success = supervisor.execute(WorkItem("async", _return_async_success))
    boundary_error = supervisor.execute(WorkItem("error", _raise_boundary_error))

    assert success.status is Status.PASS
    assert success.stdout == "child output\n"
    assert async_success.status is Status.PASS
    assert boundary_error.status is Status.INFRA_ERROR
    assert boundary_error.error is not None
    assert boundary_error.error.exception_type == "builtins.RuntimeError"


@pytest.mark.skipif(not hasattr(os, "_exit"), reason="requires process exit")
def test_process_supervisor_detects_worker_crash_and_timeout() -> None:
    supervisor = ProcessSupervisor(terminate_grace=0.01)

    crash = supervisor.execute(WorkItem("crash", _crash_process, timeout=1.0))
    timeout = supervisor.execute(WorkItem("timeout", _sleep_for, args=(0.5,), timeout=0.03))

    assert crash.status is Status.CRASH
    assert crash.exit_code == 17
    assert timeout.status is Status.TIMEOUT
    assert timeout.timed_out


def test_process_supervisor_drains_large_result_before_joining_worker() -> None:
    supervisor = ProcessSupervisor(default_timeout=3.0, start_method="spawn")

    result = supervisor.execute(WorkItem("large", _return_large_payload))

    assert result.status is Status.PASS
    assert result.value == b"x" * 1_000_000
    assert result.timed_out is False


def test_worker_events_reduce_to_the_same_attempt() -> None:
    factory = EventFactory("run")
    from testenix.events import InMemoryEventSink

    sink = InMemoryEventSink()
    spec = _spec("worker-test")
    sink.emit(
        factory.create(
            EventType.TEST_DISCOVERED,
            test_id=spec.id,
            payload={"test": spec},
        )
    )

    execution = ProcessSupervisor(default_timeout=1.0).execute(
        WorkItem(spec.id, _return_success),
        event_factory=factory,
        event_sink=sink,
    )
    result = reduce_events(sink.events)

    assert result.tests[0].status is Status.PASS
    assert result.tests[0].attempts == (execution.attempt_result,)


def test_runtime_tests_can_choose_spawn_protocol_explicitly() -> None:
    if "spawn" not in mp.get_all_start_methods():
        pytest.skip("spawn is unavailable")

    execution = ProcessSupervisor(start_method="spawn", default_timeout=2.0).execute(
        WorkItem("spawn", _return_success)
    )

    assert execution.status is Status.PASS
