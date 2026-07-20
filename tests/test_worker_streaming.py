from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from testenix.contracts import Status
from testenix.worker import ProcessSupervisor, WorkItem


def _stream_large_values(*, report) -> str:  # type: ignore[no-untyped-def]
    report(("first", b"x" * 1_000_000))
    report(("second", b"y" * 1_000_000))
    return "complete"


def _stream_then_crash(*, report) -> None:  # type: ignore[no-untyped-def]
    report("completed-before-crash")
    os._exit(19)


def _touch(path: str) -> None:
    Path(path).touch()


def _slow_start_then_ready(*, ready) -> None:  # type: ignore[no-untyped-def]
    time.sleep(0.2)
    ready()


def _never_ready(*, ready) -> None:  # type: ignore[no-untyped-def]
    del ready
    time.sleep(0.2)


def test_supervisor_drains_streamed_values_before_final_result() -> None:
    execution = ProcessSupervisor(start_method="spawn").execute(
        WorkItem(
            "stream",
            _stream_large_values,
            stream_callback_arg="report",
            timeout=5.0,
        )
    )

    assert execution.status is Status.PASS
    assert execution.value == "complete"
    assert [value[0] for value in execution.streamed_values] == ["first", "second"]


def test_supervisor_preserves_partial_stream_when_worker_crashes() -> None:
    execution = ProcessSupervisor(start_method="spawn").execute(
        WorkItem(
            "stream-crash",
            _stream_then_crash,
            stream_callback_arg="report",
            timeout=5.0,
        )
    )

    assert execution.status is Status.CRASH
    assert execution.exit_code == 19
    assert execution.streamed_values == ("completed-before-crash",)


def test_cancelled_supervisor_does_not_start_more_work(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-run"
    supervisor = ProcessSupervisor(start_method="spawn")
    supervisor.cancel_all()

    execution = supervisor.execute(WorkItem("cancelled", _touch, args=(str(marker),)))

    assert execution.status is Status.CANCELLED
    assert not marker.exists()


def test_ready_handshake_separates_startup_and_execution_deadlines() -> None:
    supervisor = ProcessSupervisor(start_method="spawn")

    success = supervisor.execute(
        WorkItem(
            "ready",
            _slow_start_then_ready,
            timeout=0.05,
            ready_callback_arg="ready",
            startup_timeout=1.0,
        )
    )
    startup_timeout = supervisor.execute(
        WorkItem(
            "never-ready",
            _never_ready,
            timeout=1.0,
            ready_callback_arg="ready",
            startup_timeout=0.05,
        )
    )

    assert success.status is Status.PASS
    assert startup_timeout.status is Status.TIMEOUT
    assert startup_timeout.error is not None
    assert "startup exceeded" in startup_timeout.error.message


def test_active_cancellation_is_reported_as_cancelled_not_crash() -> None:
    supervisor = ProcessSupervisor(start_method="spawn")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            supervisor.execute,
            WorkItem("active-cancel", time.sleep, args=(30.0,)),
        )
        time.sleep(0.1)
        supervisor.cancel_all()
        execution = future.result(timeout=5.0)

    assert execution.status is Status.CANCELLED
