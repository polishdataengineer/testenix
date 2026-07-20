from __future__ import annotations

import asyncio
import multiprocessing as mp
import textwrap
import time
from pathlib import Path

import pytest

from testenix.aggregate import reduce_events
from testenix.config import TestenixConfig
from testenix.contracts import EventType, Status
from testenix.events import InMemoryEventSink, read_events
from testenix.runner import run, run_async


def _suite(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "test_sample.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


def test_runner_executes_native_sync_async_cases_and_scoped_fixtures(tmp_path: Path) -> None:
    path = _suite(
        tmp_path,
        """
        from collections.abc import AsyncIterator
        from testenix import case, cases, fixture, test

        calls = 0

        @fixture(scope="module")
        async def factor() -> AsyncIterator[int]:
            global calls
            calls += 1
            yield calls * 2

        @test("typed async case")
        @cases(case(id="one", value=1), case(id="two", value=2))
        async def multiply(factor: int, value: int):
            assert factor == 2
            assert factor * value == value * 2

        def test_scope_was_reused(factor: int):
            assert factor == 2
            assert calls == 1
        """,
    )

    result = run((str(path),), TestenixConfig(workers=1, history_path=None))

    assert [test.status for test in result.tests] == [Status.PASS] * 3
    assert result.exit_code == 0


def test_runner_preserves_failed_attempt_and_finalizes_retry_as_flaky(tmp_path: Path) -> None:
    state = tmp_path / "flaky-state"
    path = _suite(
        tmp_path,
        f"""
        from pathlib import Path

        def test_flaky():
            state = Path({str(state)!r})
            if not state.exists():
                state.write_text("failed once", encoding="utf-8")
                assert False, "first attempt"
        """,
    )

    result = run((str(path),), TestenixConfig(workers=1, retries=1, history_path=None))

    test_result = result.tests[0]
    assert test_result.status is Status.FLAKY
    assert [attempt.status for attempt in test_result.attempts] == [Status.FAIL, Status.PASS]
    assert result.exit_code == 1


def test_runner_recovers_worker_crash_but_keeps_it_gating(tmp_path: Path) -> None:
    state = tmp_path / "crash-state"
    path = _suite(
        tmp_path,
        f"""
        import os
        from pathlib import Path

        def test_worker_recovery():
            state = Path({str(state)!r})
            if not state.exists():
                state.write_text("crashed once", encoding="utf-8")
                os._exit(17)
        """,
    )

    result = run((str(path),), TestenixConfig(workers=1, retries=0, history_path=None))

    test_result = result.tests[0]
    assert test_result.status is Status.FLAKY
    assert [attempt.status for attempt in test_result.attempts] == [
        Status.CRASH,
        Status.PASS,
    ]
    assert result.exit_code == 1


def test_runner_enforces_timeout_and_tag_selection(tmp_path: Path) -> None:
    path = _suite(
        tmp_path,
        """
        import time
        from testenix import test

        @test(tags={"fast"}, timeout=0.05)
        def test_timeout():
            time.sleep(0.2)

        @test(tags={"slow"})
        def test_not_selected():
            raise AssertionError("must not run")
        """,
    )

    result = run(
        (str(path),),
        TestenixConfig(workers=2, tags=("fast",), history_path=None),
    )

    assert len(result.tests) == 1
    assert result.tests[0].test.function_name == "test_timeout"
    assert result.tests[0].status is Status.TIMEOUT

    empty = run(
        (str(path),),
        TestenixConfig(workers=2, tags=("missing",), history_path=None),
    )
    assert empty.tests == ()
    assert empty.exit_code == 2
    assert "no tests selected" in empty.collection_issues[0].message


def test_runner_retains_effective_global_timeout_in_result(tmp_path: Path) -> None:
    path = _suite(tmp_path, "def test_passes():\n    assert True\n")

    result = run(
        (str(path),),
        TestenixConfig(workers=1, timeout=1.25, history_path=None),
    )

    assert result.tests[0].status is Status.PASS
    assert result.tests[0].test.timeout == 1.25


def test_runner_persists_replayable_events_and_duration_history(tmp_path: Path) -> None:
    path = _suite(tmp_path, "def test_passes():\n    assert True\n")
    history_path = tmp_path / ".testenix" / "history.sqlite3"

    result = run((str(path),), TestenixConfig(workers=1, history_path=history_path))

    event_path = history_path.parent / "runs" / f"{result.run_id}.jsonl"
    events = tuple(read_events(event_path))
    assert events[0].event_type is EventType.RUN_STARTED
    assert events[-1].event_type is EventType.RUN_FINISHED
    assert history_path.is_file()


def test_runner_emits_compact_lossless_events_for_unfiltered_run(tmp_path: Path) -> None:
    path = _suite(
        tmp_path,
        """
        def test_one():
            assert True

        def test_two():
            assert True
        """,
    )
    sink = InMemoryEventSink()

    result = run(
        (str(path),),
        TestenixConfig(workers=1, history_path=None),
        event_sink=sink,
    )

    event_types = [event.event_type for event in sink.events]
    assert event_types.count(EventType.ATTEMPT_FINISHED) == 2
    assert EventType.ATTEMPT_STARTED not in event_types
    assert EventType.PHASE_FINISHED not in event_types
    assert EventType.TEST_SELECTED not in event_types
    assert reduce_events(sink.events) == result


def test_run_async_cancellation_reaps_active_worker(tmp_path: Path) -> None:
    pid_path = tmp_path / "worker.pid"
    path = _suite(
        tmp_path,
        f"""
        import os
        import time
        from pathlib import Path

        def test_blocks_until_cancelled():
            pid_path = Path({str(pid_path)!r})
            pending_pid_path = pid_path.with_suffix(".pending")
            pending_pid_path.write_text(str(os.getpid()), encoding="utf-8")
            pending_pid_path.replace(pid_path)
            time.sleep(30)
        """,
    )

    async def cancel_running_suite() -> int:
        task = asyncio.create_task(
            run_async((str(path),), TestenixConfig(workers=1, history_path=None))
        )
        deadline = time.monotonic() + 5.0
        while not pid_path.exists():
            if time.monotonic() >= deadline:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                raise AssertionError("worker did not start before cancellation deadline")
            await asyncio.sleep(0.01)
        worker_pid = int(pid_path.read_text(encoding="utf-8"))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return worker_pid

    started = time.monotonic()
    worker_pid = asyncio.run(cancel_running_suite())

    assert time.monotonic() - started < 5.0
    assert worker_pid not in {child.pid for child in mp.active_children()}


def test_run_async_cancellation_reaps_collection_worker(tmp_path: Path) -> None:
    pid_path = tmp_path / "collection.pid"
    path = _suite(
        tmp_path,
        f"""
        import os
        import time
        from pathlib import Path

        pid_path = Path({str(pid_path)!r})
        pending_pid_path = pid_path.with_suffix(".pending")
        pending_pid_path.write_text(str(os.getpid()), encoding="utf-8")
        pending_pid_path.replace(pid_path)
        time.sleep(30)

        def test_never_collected():
            pass
        """,
    )

    async def cancel_collection() -> int:
        task = asyncio.create_task(
            run_async((str(path),), TestenixConfig(workers=1, history_path=None))
        )
        deadline = time.monotonic() + 5.0
        while not pid_path.exists():
            if time.monotonic() >= deadline:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                raise AssertionError("collection worker did not start")
            await asyncio.sleep(0.01)
        worker_pid = int(pid_path.read_text(encoding="utf-8"))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return worker_pid

    started = time.monotonic()
    worker_pid = asyncio.run(cancel_collection())

    assert time.monotonic() - started < 5.0
    assert worker_pid not in {child.pid for child in mp.active_children()}
