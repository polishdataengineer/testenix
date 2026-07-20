"""Local process supervision with an explicit, small result protocol.

The worker boundary treats an uncaught adapter exception as infrastructure
failure. Native executors should convert test exceptions to ``TestResult``; a
caller executing a bare test function can opt into ``exception_status=FAIL``.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import io
import math
import multiprocessing as mp
import os
import signal
import subprocess
import threading
import time
import traceback as traceback_module
from collections.abc import Awaitable, Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from multiprocessing.connection import Connection, wait
from typing import Any

from testenix.contracts import (
    AttemptResult,
    EventType,
    Phase,
    PhaseResult,
    Status,
    TestResult,
)
from testenix.events import EventFactory, EventSink

PROTOCOL_VERSION = 2


@dataclass(frozen=True, slots=True)
class WorkItem:
    """Serializable description of one attempt executed in a fresh process."""

    test_id: str
    target: Callable[..., Any]
    args: tuple[Any, ...] = ()
    kwargs: Mapping[str, Any] = field(default_factory=dict)
    attempt: int = 1
    timeout: float | None = None
    exception_status: Status = Status.INFRA_ERROR
    stream_callback_arg: str | None = None
    ready_callback_arg: str | None = None
    startup_timeout: float | None = None

    def __post_init__(self) -> None:
        if not self.test_id:
            raise ValueError("test_id must not be empty")
        if not callable(self.target):
            raise TypeError("target must be callable")
        if self.attempt < 1:
            raise ValueError("attempt must be positive")
        if self.timeout is not None and (not math.isfinite(self.timeout) or self.timeout <= 0):
            raise ValueError("timeout must be positive")
        if self.stream_callback_arg is not None and not self.stream_callback_arg.isidentifier():
            raise ValueError("stream_callback_arg must be a valid Python identifier")
        if self.ready_callback_arg is not None and not self.ready_callback_arg.isidentifier():
            raise ValueError("ready_callback_arg must be a valid Python identifier")
        if (
            self.ready_callback_arg == self.stream_callback_arg
            and self.ready_callback_arg is not None
        ):
            raise ValueError("ready and stream callbacks must use different argument names")
        if self.ready_callback_arg is not None and self.timeout is None:
            raise ValueError("ready_callback_arg requires an execution timeout")
        if self.startup_timeout is not None and (
            not math.isfinite(self.startup_timeout) or self.startup_timeout <= 0
        ):
            raise ValueError("startup_timeout must be positive")
        if self.startup_timeout is not None and self.ready_callback_arg is None:
            raise ValueError("startup_timeout requires ready_callback_arg")
        object.__setattr__(self, "args", tuple(self.args))
        object.__setattr__(self, "kwargs", dict(self.kwargs))
        object.__setattr__(self, "exception_status", Status(self.exception_status))


@dataclass(frozen=True, slots=True)
class RemoteError:
    """Inert diagnostics transferred from the child process."""

    exception_type: str
    message: str
    traceback: str | None = None


@dataclass(frozen=True, slots=True)
class WorkerExecution:
    """Supervisor observation for one work item."""

    test_id: str
    attempt: int
    attempt_result: AttemptResult
    worker_id: str
    exit_code: int | None
    protocol_version: int = PROTOCOL_VERSION
    stdout: str = ""
    stderr: str = ""
    error: RemoteError | None = None
    timed_out: bool = False
    value: Any = None
    streamed_values: tuple[Any, ...] = ()

    @property
    def status(self) -> Status:
        return self.attempt_result.status

    @property
    def duration(self) -> float:
        return self.attempt_result.duration


WorkerResult = WorkerExecution


def _qualified_exception_name(error: BaseException) -> str:
    error_type = type(error)
    return f"{error_type.__module__}.{error_type.__qualname__}"


async def _await_result(awaitable: Awaitable[Any]) -> Any:
    return await awaitable


def _terminate_process_tree(process: Any, *, force: bool = False) -> None:
    """Best-effort termination of a worker and descendants it started."""

    pid = process.pid
    if pid is None:
        return
    if os.name == "posix":
        try:
            os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)
            return
        except OSError:
            # The child may not have reached setsid() yet; fall back to its PID.
            pass
    elif os.name == "nt":  # pragma: no cover - exercised by the Windows CI matrix.
        command = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            command.append("/F")
        completed = subprocess.run(command, capture_output=True, check=False)
        if completed.returncode == 0:
            return
    with contextlib.suppress(OSError, ValueError):
        if force and hasattr(process, "kill"):
            process.kill()
        else:
            process.terminate()


def _child_entry(connection: Connection, item: WorkItem) -> None:
    if os.name == "posix" and hasattr(os, "setsid"):
        with contextlib.suppress(OSError):
            os.setsid()
    started_at = time.time()
    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
            kwargs = dict(item.kwargs)
            if item.ready_callback_arg is not None:
                if item.ready_callback_arg in kwargs:
                    raise ValueError(
                        f"reserved ready callback argument {item.ready_callback_arg!r} "
                        "was supplied explicitly"
                    )

                def send_ready() -> None:
                    connection.send(
                        {
                            "protocol_version": PROTOCOL_VERSION,
                            "kind": "ready",
                            "pid": os.getpid(),
                            "timestamp": time.time(),
                        }
                    )

                kwargs[item.ready_callback_arg] = send_ready
            if item.stream_callback_arg is not None:
                if item.stream_callback_arg in kwargs:
                    raise ValueError(
                        f"reserved stream callback argument {item.stream_callback_arg!r} "
                        "was supplied explicitly"
                    )

                def send_stream_value(value: Any) -> None:
                    connection.send(
                        {
                            "protocol_version": PROTOCOL_VERSION,
                            "kind": "stream",
                            "pid": os.getpid(),
                            "timestamp": time.time(),
                            "value": value,
                        }
                    )

                kwargs[item.stream_callback_arg] = send_stream_value
            value = item.target(*item.args, **kwargs)
            if inspect.isawaitable(value):
                value = asyncio.run(_await_result(value))
        envelope: dict[str, Any] = {
            "protocol_version": PROTOCOL_VERSION,
            "kind": "ok",
            "pid": os.getpid(),
            "started_at": started_at,
            "finished_at": time.time(),
            "stdout": stdout_buffer.getvalue(),
            "stderr": stderr_buffer.getvalue(),
            "value": value,
        }
    except BaseException as error:
        envelope = {
            "protocol_version": PROTOCOL_VERSION,
            "kind": "exception",
            "pid": os.getpid(),
            "started_at": started_at,
            "finished_at": time.time(),
            "stdout": stdout_buffer.getvalue(),
            "stderr": stderr_buffer.getvalue(),
            "exception_type": _qualified_exception_name(error),
            "message": str(error),
            "traceback": "".join(
                traceback_module.format_exception(type(error), error, error.__traceback__)
            ),
        }

    try:
        connection.send(envelope)
    except BaseException as error:
        # A test may return an unpicklable object. Preserve a fixed protocol
        # envelope rather than silently losing the worker.
        fallback = {
            "protocol_version": PROTOCOL_VERSION,
            "kind": "serialization_error",
            "pid": os.getpid(),
            "started_at": started_at,
            "finished_at": time.time(),
            "stdout": stdout_buffer.getvalue(),
            "stderr": stderr_buffer.getvalue(),
            "exception_type": _qualified_exception_name(error),
            "message": f"worker result is not serializable: {error}",
            "traceback": "".join(
                traceback_module.format_exception(type(error), error, error.__traceback__)
            ),
        }
        with contextlib.suppress(BaseException):
            connection.send(fallback)
    finally:
        connection.close()


def _phase_for_status(
    status: Status,
    duration: float,
    *,
    error: RemoteError | None,
    stdout: str,
    stderr: str,
) -> PhaseResult:
    return PhaseResult(
        phase=Phase.CALL,
        status=status,
        duration=duration,
        message=None if error is None else error.message,
        exception_type=None if error is None else error.exception_type,
        traceback=None if error is None else error.traceback,
        stdout=stdout,
        stderr=stderr,
    )


def _synthetic_attempt(
    item: WorkItem,
    status: Status,
    *,
    worker_id: str,
    started_at: float,
    finished_at: float,
    error: RemoteError | None = None,
    stdout: str = "",
    stderr: str = "",
) -> AttemptResult:
    duration = max(0.0, finished_at - started_at)
    phase = _phase_for_status(
        status,
        duration,
        error=error,
        stdout=stdout,
        stderr=stderr,
    )
    return AttemptResult(
        test_id=item.test_id,
        attempt=item.attempt,
        worker_id=worker_id,
        status=status,
        duration=duration,
        phases=(phase,),
        started_at=started_at,
        finished_at=finished_at,
    )


def _attempt_from_value(
    item: WorkItem,
    value: Any,
    *,
    worker_id: str,
    started_at: float,
    finished_at: float,
    stdout: str,
    stderr: str,
) -> AttemptResult:
    if isinstance(value, AttemptResult):
        return value
    if isinstance(value, TestResult):
        matching = [attempt for attempt in value.attempts if attempt.attempt == item.attempt]
        if matching:
            return matching[-1]
        if value.attempts:
            return value.attempts[-1]
        return _synthetic_attempt(
            item,
            value.status,
            worker_id=worker_id,
            started_at=started_at,
            finished_at=finished_at,
            stdout=stdout,
            stderr=stderr,
        )
    status = value if isinstance(value, Status) else Status.PASS
    return _synthetic_attempt(
        item,
        status,
        worker_id=worker_id,
        started_at=started_at,
        finished_at=finished_at,
        stdout=stdout,
        stderr=stderr,
    )


def _default_start_method() -> str:
    available = mp.get_all_start_methods()
    # execute_shards starts workers from coordinator threads. Spawn avoids
    # inheriting arbitrary locked state, which makes fork unsafe in that model.
    if "spawn" in available:
        return "spawn"
    if "forkserver" in available:
        return "forkserver"
    return available[0]


class ProcessSupervisor:
    """Start a fresh interpreter process per attempt and enforce its deadline."""

    def __init__(
        self,
        *,
        default_timeout: float | None = None,
        start_method: str | None = None,
        terminate_grace: float = 0.2,
    ) -> None:
        if default_timeout is not None and (
            not math.isfinite(default_timeout) or default_timeout <= 0
        ):
            raise ValueError("default_timeout must be positive")
        if not math.isfinite(terminate_grace) or terminate_grace < 0:
            raise ValueError("terminate_grace must be non-negative")
        self.default_timeout = default_timeout
        self.start_method = start_method or _default_start_method()
        if self.start_method not in mp.get_all_start_methods():
            raise ValueError(f"unsupported multiprocessing start method {self.start_method!r}")
        self.terminate_grace = terminate_grace
        self._cancelled = threading.Event()
        self._active_lock = threading.Lock()
        self._active_processes: set[Any] = set()

    def cancel_all(self) -> None:
        """Prevent new work and terminate every process currently supervised."""

        self._cancelled.set()
        with self._active_lock:
            processes = tuple(self._active_processes)
        for process in processes:
            if process.is_alive():
                _terminate_process_tree(process)

    def _cancelled_execution(
        self,
        item: WorkItem,
        event_factory: EventFactory | None,
        event_sink: EventSink | None,
    ) -> WorkerExecution:
        now = time.time()
        error = RemoteError(
            exception_type="testenix.worker.WorkerCancelled",
            message="worker execution was cancelled",
        )
        attempt = _synthetic_attempt(
            item,
            Status.CANCELLED,
            worker_id="local-cancelled",
            started_at=now,
            finished_at=now,
            error=error,
        )
        execution = WorkerExecution(
            test_id=item.test_id,
            attempt=item.attempt,
            attempt_result=attempt,
            worker_id=attempt.worker_id,
            exit_code=None,
            error=error,
        )
        self._emit(execution, event_factory, event_sink)
        return execution

    def execute(
        self,
        item: WorkItem,
        *,
        event_factory: EventFactory | None = None,
        event_sink: EventSink | None = None,
    ) -> WorkerExecution:
        if (event_factory is None) != (event_sink is None):
            raise ValueError("event_factory and event_sink must be provided together")
        if self._cancelled.is_set():
            return self._cancelled_execution(item, event_factory, event_sink)

        context = mp.get_context(self.start_method)
        parent_connection, child_connection = context.Pipe(duplex=False)
        process = context.Process(  # type: ignore[attr-defined]
            target=_child_entry,
            args=(child_connection, item),
            name=f"testenix-{item.test_id[:40]}",
        )
        parent_started_at = time.time()
        try:
            process.start()
        except BaseException as error:
            child_connection.close()
            parent_connection.close()
            finished_at = time.time()
            remote_error = RemoteError(
                exception_type=_qualified_exception_name(error),
                message=f"worker failed to start: {error}",
                traceback="".join(
                    traceback_module.format_exception(type(error), error, error.__traceback__)
                ),
            )
            attempt = _synthetic_attempt(
                item,
                Status.INFRA_ERROR,
                worker_id="local-unstarted",
                started_at=parent_started_at,
                finished_at=finished_at,
                error=remote_error,
            )
            execution = WorkerExecution(
                test_id=item.test_id,
                attempt=item.attempt,
                attempt_result=attempt,
                worker_id=attempt.worker_id,
                exit_code=None,
                error=remote_error,
            )
            self._emit(execution, event_factory, event_sink)
            return execution
        finally:
            # Once started the child owns this endpoint. On a start failure the
            # close above is intentionally harmless when repeated.
            with contextlib.suppress(OSError):
                child_connection.close()

        with self._active_lock:
            self._active_processes.add(process)
        if self._cancelled.is_set():
            _terminate_process_tree(process)

        worker_id = f"local-{process.pid}"
        timeout = item.timeout if item.timeout is not None else self.default_timeout
        envelope: Mapping[str, Any] | None = None
        streamed_values: list[Any] = []
        ready_received = item.ready_callback_arg is None
        initial_timeout = item.startup_timeout if not ready_received else timeout
        deadline = None if initial_timeout is None else time.monotonic() + initial_timeout
        timed_out = False
        try:
            while envelope is None:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                ready = wait((parent_connection, process.sentinel), remaining)
                if not ready:
                    timed_out = True
                    break

                # Drain every currently buffered envelope before considering
                # process exit. This prevents both large final values and a
                # stream of partial results from filling the OS pipe.
                if parent_connection in ready:
                    drained = 0
                    while drained < 64:
                        if deadline is not None and time.monotonic() >= deadline:
                            timed_out = True
                            break
                        if self._cancelled.is_set():
                            break
                        received = parent_connection.recv()
                        drained += 1
                        if isinstance(received, Mapping):
                            if (
                                int(received.get("protocol_version", 0)) == PROTOCOL_VERSION
                                and received.get("kind") == "stream"
                            ):
                                streamed_values.append(received.get("value"))
                            elif (
                                int(received.get("protocol_version", 0)) == PROTOCOL_VERSION
                                and received.get("kind") == "ready"
                                and not ready_received
                            ):
                                ready_received = True
                                deadline = None if timeout is None else time.monotonic() + timeout
                            else:
                                envelope = received
                                break
                        if not parent_connection.poll():
                            break

                if timed_out or self._cancelled.is_set():
                    break

                if envelope is None and process.sentinel in ready:
                    # The sentinel and pipe are separate handles. Give the pipe
                    # one final opportunity after a very fast child exit.
                    if parent_connection.poll(0.01):
                        continue
                    break
        except (EOFError, OSError):
            envelope = None
        finally:
            parent_connection.close()
            if envelope is not None and process.is_alive():
                # The result is fully received. End the worker's process group
                # now so fire-and-forget descendants cannot outlive a passing
                # or softly timed-out target.
                _terminate_process_tree(process)
            if timed_out and process.is_alive():
                _terminate_process_tree(process)
                process.join(self.terminate_grace)
                if process.is_alive() and hasattr(process, "kill"):
                    _terminate_process_tree(process, force=True)
                    process.join()
            else:
                process.join(self.terminate_grace)
                if process.is_alive():
                    _terminate_process_tree(process)
                    process.join(self.terminate_grace)
                    if process.is_alive() and hasattr(process, "kill"):
                        _terminate_process_tree(process, force=True)
                        process.join()
            # On POSIX a descendant may keep the process group alive after the
            # worker itself exited between recv() and cleanup.
            _terminate_process_tree(process, force=True)
            with self._active_lock:
                self._active_processes.discard(process)

        if self._cancelled.is_set():
            finished_at = time.time()
            cancellation_error = RemoteError(
                exception_type="testenix.worker.WorkerCancelled",
                message="active worker execution was cancelled",
            )
            attempt = _synthetic_attempt(
                item,
                Status.CANCELLED,
                worker_id=worker_id,
                started_at=parent_started_at,
                finished_at=finished_at,
                error=cancellation_error,
            )
            execution = WorkerExecution(
                test_id=item.test_id,
                attempt=item.attempt,
                attempt_result=attempt,
                worker_id=worker_id,
                exit_code=process.exitcode,
                error=cancellation_error,
                streamed_values=tuple(streamed_values),
            )
            self._emit(execution, event_factory, event_sink)
            return execution

        if timed_out:
            finished_at = time.time()
            timeout_message = (
                f"attempt exceeded timeout of {timeout:.3f}s"
                if ready_received
                else f"worker startup exceeded timeout of {initial_timeout:.3f}s"
            )
            remote_error = RemoteError(
                exception_type="testenix.worker.WorkerTimeout",
                message=timeout_message,
            )
            attempt = _synthetic_attempt(
                item,
                Status.TIMEOUT,
                worker_id=worker_id,
                started_at=parent_started_at,
                finished_at=finished_at,
                error=remote_error,
            )
            execution = WorkerExecution(
                test_id=item.test_id,
                attempt=item.attempt,
                attempt_result=attempt,
                worker_id=worker_id,
                exit_code=process.exitcode,
                error=remote_error,
                timed_out=True,
                streamed_values=tuple(streamed_values),
            )
            self._emit(execution, event_factory, event_sink)
            return execution

        if envelope is None:
            finished_at = time.time()
            if process.exitcode in (0, None):
                status = Status.INFRA_ERROR
                exception_type = "testenix.worker.ProtocolError"
                message = "worker exited without a result envelope"
            else:
                status = Status.CRASH
                exception_type = "testenix.worker.WorkerCrash"
                message = f"worker exited with code {process.exitcode}"
            remote_error = RemoteError(exception_type=exception_type, message=message)
            attempt = _synthetic_attempt(
                item,
                status,
                worker_id=worker_id,
                started_at=parent_started_at,
                finished_at=finished_at,
                error=remote_error,
            )
            execution = WorkerExecution(
                test_id=item.test_id,
                attempt=item.attempt,
                attempt_result=attempt,
                worker_id=worker_id,
                exit_code=process.exitcode,
                error=remote_error,
                streamed_values=tuple(streamed_values),
            )
            self._emit(execution, event_factory, event_sink)
            return execution

        protocol_version = int(envelope.get("protocol_version", 0))
        started_at = float(envelope.get("started_at", parent_started_at))
        finished_at = float(envelope.get("finished_at", time.time()))
        stdout = str(envelope.get("stdout", ""))
        stderr = str(envelope.get("stderr", ""))
        envelope_error: RemoteError | None
        if protocol_version != PROTOCOL_VERSION:
            kind = "protocol_error"
            envelope_error = RemoteError(
                exception_type="testenix.worker.ProtocolVersionError",
                message=(
                    f"worker protocol {protocol_version} is incompatible with "
                    f"supervisor protocol {PROTOCOL_VERSION}"
                ),
            )
        else:
            kind = str(envelope.get("kind", "protocol_error"))
            envelope_error = None

        if kind == "ok":
            value = envelope.get("value")
            attempt = _attempt_from_value(
                item,
                value,
                worker_id=worker_id,
                started_at=started_at,
                finished_at=finished_at,
                stdout=stdout,
                stderr=stderr,
            )
            execution = WorkerExecution(
                test_id=item.test_id,
                attempt=item.attempt,
                attempt_result=attempt,
                worker_id=attempt.worker_id,
                exit_code=process.exitcode,
                stdout=stdout,
                stderr=stderr,
                value=value,
                streamed_values=tuple(streamed_values),
            )
        else:
            if envelope_error is None:
                envelope_error = RemoteError(
                    exception_type=str(
                        envelope.get("exception_type", "testenix.worker.ProtocolError")
                    ),
                    message=str(envelope.get("message", "worker protocol failure")),
                    traceback=(
                        None if envelope.get("traceback") is None else str(envelope["traceback"])
                    ),
                )
            status = item.exception_status if kind == "exception" else Status.INFRA_ERROR
            attempt = _synthetic_attempt(
                item,
                status,
                worker_id=worker_id,
                started_at=started_at,
                finished_at=finished_at,
                error=envelope_error,
                stdout=stdout,
                stderr=stderr,
            )
            execution = WorkerExecution(
                test_id=item.test_id,
                attempt=item.attempt,
                attempt_result=attempt,
                worker_id=worker_id,
                exit_code=process.exitcode,
                stdout=stdout,
                stderr=stderr,
                error=envelope_error,
                streamed_values=tuple(streamed_values),
            )

        self._emit(execution, event_factory, event_sink)
        return execution

    @staticmethod
    def _emit(
        execution: WorkerExecution,
        factory: EventFactory | None,
        sink: EventSink | None,
    ) -> None:
        if factory is None or sink is None:
            return
        result = execution.attempt_result
        sink.emit(
            factory.create(
                EventType.ATTEMPT_STARTED,
                test_id=execution.test_id,
                attempt=execution.attempt,
                worker_id=execution.worker_id,
                timestamp=result.started_at,
                payload={"started_at": result.started_at},
            )
        )
        for phase in result.phases:
            sink.emit(
                factory.create(
                    EventType.PHASE_FINISHED,
                    test_id=execution.test_id,
                    attempt=execution.attempt,
                    worker_id=execution.worker_id,
                    payload={"phase_result": phase},
                )
            )
        if execution.status is Status.CRASH:
            sink.emit(
                factory.create(
                    EventType.WORKER_LOST,
                    test_id=execution.test_id,
                    attempt=execution.attempt,
                    worker_id=execution.worker_id,
                    payload={
                        "status": execution.status,
                        "duration": result.duration,
                        "message": (None if execution.error is None else execution.error.message),
                    },
                )
            )
        sink.emit(
            factory.create(
                EventType.ATTEMPT_FINISHED,
                test_id=execution.test_id,
                attempt=execution.attempt,
                worker_id=execution.worker_id,
                timestamp=result.finished_at,
                payload={"attempt_result": result},
            )
        )

    def execute_shard(
        self,
        items: Sequence[WorkItem],
        *,
        event_factory: EventFactory | None = None,
        event_sink: EventSink | None = None,
    ) -> tuple[WorkerExecution, ...]:
        """Execute one static shard sequentially in deterministic item order."""

        return tuple(
            self.execute(item, event_factory=event_factory, event_sink=event_sink) for item in items
        )

    def execute_shards(
        self,
        shards: Sequence[Sequence[WorkItem]],
        *,
        event_factory: EventFactory | None = None,
        event_sink: EventSink | None = None,
    ) -> tuple[tuple[WorkerExecution, ...], ...]:
        """Execute shards concurrently while returning results in shard order."""

        if not shards:
            return ()
        with ThreadPoolExecutor(
            max_workers=len(shards), thread_name_prefix="testenix-shard"
        ) as pool:
            futures = [
                pool.submit(
                    self.execute_shard,
                    shard,
                    event_factory=event_factory,
                    event_sink=event_sink,
                )
                for shard in shards
            ]
            try:
                return tuple(future.result() for future in futures)
            except BaseException:
                self.cancel_all()
                for future in futures:
                    future.cancel()
                raise


def execute_work_item(
    item: WorkItem,
    *,
    default_timeout: float | None = None,
    start_method: str | None = None,
) -> WorkerExecution:
    """One-shot convenience wrapper around :class:`ProcessSupervisor`."""

    return ProcessSupervisor(
        default_timeout=default_timeout,
        start_method=start_method,
    ).execute(item)


def execute_shard(
    items: Sequence[WorkItem],
    *,
    default_timeout: float | None = None,
    start_method: str | None = None,
) -> tuple[WorkerExecution, ...]:
    return ProcessSupervisor(
        default_timeout=default_timeout,
        start_method=start_method,
    ).execute_shard(items)


def execute_shards(
    shards: Sequence[Sequence[WorkItem]],
    *,
    default_timeout: float | None = None,
    start_method: str | None = None,
) -> tuple[tuple[WorkerExecution, ...], ...]:
    return ProcessSupervisor(
        default_timeout=default_timeout,
        start_method=start_method,
    ).execute_shards(shards)


run_work_item = execute_work_item
run_shard = execute_shard
run_shards = execute_shards


__all__ = [
    "PROTOCOL_VERSION",
    "ProcessSupervisor",
    "RemoteError",
    "WorkItem",
    "WorkerExecution",
    "WorkerResult",
    "execute_shard",
    "execute_shards",
    "execute_work_item",
    "run_shard",
    "run_shards",
    "run_work_item",
]
