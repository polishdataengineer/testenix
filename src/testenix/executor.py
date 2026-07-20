"""In-process executor for native Testenix tests."""

from __future__ import annotations

import asyncio
import inspect
import io
import os
import threading
import time
import traceback as traceback_module
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import redirect_stderr, redirect_stdout, suppress
from dataclasses import replace
from typing import Any

from testenix.contracts import AttemptResult, Phase, PhaseResult, Status, TestResult, TestSpec
from testenix.discovery import CollectedTest, discover
from testenix.fixtures import FixtureRegistry, FixtureRuntime, TeardownFailure


class NativeExecutionError(RuntimeError):
    """Base error raised before a valid native attempt can be created."""


class TestTimeoutError(TimeoutError):
    """Raised internally when a native test exceeds its declared timeout."""


class BackgroundTaskLeakError(RuntimeError):
    """An async test returned while tasks it created were still running."""


def _worker_id() -> str:
    return f"local-{os.getpid()}"


async def _await_with_timeout(awaitable: Awaitable[Any], timeout: float | None) -> Any:
    if timeout is None:
        return await awaitable
    try:
        async with asyncio.timeout(timeout):
            return await awaitable
    except TimeoutError as error:
        raise TestTimeoutError(f"test exceeded its {timeout:g}s timeout") from error


async def _call_sync_in_daemon_thread(
    function: Callable[..., Any],
    kwargs: Mapping[str, Any],
    timeout: float | None,
) -> Any:
    """Run sync user code without exposing the executor's internal event loop."""

    if timeout is None:
        # Untimed sync tests are executed sequentially inside a worker, so the
        # event loop's reusable executor thread is both safe and substantially
        # cheaper than creating one OS thread per test. Timed calls retain the
        # daemon-thread path below: a timed-out Python thread cannot be killed,
        # and asyncio.run() must not wait for it during executor shutdown before
        # the supervising process can enforce the hard deadline.
        return await asyncio.to_thread(function, **dict(kwargs))

    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()

    def invoke() -> None:
        try:
            value = function(**kwargs)
        except BaseException as error:
            captured_error = error

            def fail(error_to_set: BaseException = captured_error) -> None:
                if not future.done():
                    future.set_exception(error_to_set)

            with suppress(RuntimeError):
                loop.call_soon_threadsafe(fail)
        else:

            def succeed() -> None:
                if not future.done():
                    future.set_result(value)

            with suppress(RuntimeError):
                loop.call_soon_threadsafe(succeed)

    threading.Thread(
        target=invoke,
        name=f"testenix-sync-{getattr(function, '__name__', 'call')}",
        daemon=True,
    ).start()
    return await _await_with_timeout(future, timeout)


async def _invoke_test_body(
    function: Callable[..., Any],
    kwargs: Mapping[str, Any],
    timeout: float | None,
) -> None:
    started = time.monotonic()
    if inspect.iscoroutinefunction(function):
        result = function(**kwargs)
        await _await_with_timeout(result, timeout)
        return

    result = await _call_sync_in_daemon_thread(function, kwargs, timeout)
    elapsed = time.monotonic() - started
    if inspect.isawaitable(result):
        remaining = None if timeout is None else max(0.0, timeout - elapsed)
        await _await_with_timeout(result, remaining)


async def _drain_owned_tasks(tasks: set[asyncio.Future[Any]]) -> tuple[BaseException, ...]:
    observed: set[asyncio.Future[Any]] = set()
    errors: list[BaseException] = []
    while batch := tuple(task for task in tasks if task not in observed):
        observed.update(batch)
        pending_count = sum(not task.done() for task in batch)
        if pending_count:
            errors.append(
                BackgroundTaskLeakError(
                    f"test left {pending_count} unfinished asyncio background task(s)"
                )
            )
        for task in batch:
            if not task.done():
                task.cancel()
        outcomes = await asyncio.gather(*batch, return_exceptions=True)
        errors.extend(
            outcome
            for outcome in outcomes
            if isinstance(outcome, BaseException)
            and not isinstance(outcome, asyncio.CancelledError)
        )
    return tuple(errors)


async def _invoke_test(
    function: Callable[..., Any],
    kwargs: Mapping[str, Any],
    timeout: float | None,
) -> None:
    """Invoke one test and contain every asyncio task it creates."""

    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    owned_tasks: set[asyncio.Future[Any]] = set()

    def tracking_factory(
        target_loop: asyncio.AbstractEventLoop,
        coroutine: Any,
        **factory_kwargs: Any,
    ) -> asyncio.Future[Any]:
        context = factory_kwargs.get("context")
        task: asyncio.Future[Any]
        if previous_factory is None:
            task = asyncio.Task(coroutine, loop=target_loop, context=context)
        else:
            task = previous_factory(target_loop, coroutine)
        owned_tasks.add(task)
        return task

    loop.set_task_factory(tracking_factory)
    primary_error: BaseException | None = None
    try:
        await _invoke_test_body(function, kwargs, timeout)
    except BaseException as error:
        primary_error = error
    finally:
        background_errors = await _drain_owned_tasks(owned_tasks)
        loop.set_task_factory(previous_factory)

    if isinstance(primary_error, asyncio.CancelledError):
        raise primary_error
    failures = ([primary_error] if primary_error is not None else []) + list(background_errors)
    if len(failures) == 1:
        raise failures[0]
    if failures:
        raise BaseExceptionGroup("test and background tasks failed", failures)


def _exception_fields(error: BaseException) -> tuple[str, str, str]:
    message = str(error) or repr(error)
    exception_type = f"{type(error).__module__}.{type(error).__qualname__}"
    formatted = "".join(traceback_module.format_exception(type(error), error, error.__traceback__))
    return message, exception_type, formatted


def _failed_phase(
    phase: Phase,
    status: Status,
    error: BaseException,
    duration: float,
    stdout: str,
    stderr: str,
    *,
    message_prefix: str | None = None,
) -> PhaseResult:
    message, exception_type, formatted = _exception_fields(error)
    if message_prefix:
        message = f"{message_prefix}: {message}"
    return PhaseResult(
        phase=phase,
        status=status,
        duration=duration,
        message=message,
        exception_type=exception_type,
        traceback=formatted,
        stdout=stdout,
        stderr=stderr,
    )


async def _capture_teardown(
    callback: Callable[[], Awaitable[tuple[TeardownFailure, ...]]],
) -> tuple[tuple[TeardownFailure, ...], float, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    started = time.monotonic()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            failures = await callback()
    except asyncio.CancelledError:
        raise
    except BaseException as error:
        failures = (
            TeardownFailure(
                fixture_name="<fixture runtime>",
                exception=error,
                traceback="".join(
                    traceback_module.format_exception(type(error), error, error.__traceback__)
                ),
            ),
        )
    return failures, time.monotonic() - started, stdout.getvalue(), stderr.getvalue()


def _teardown_phases(
    failures: Sequence[TeardownFailure],
    duration: float,
    stdout: str,
    stderr: str,
) -> tuple[PhaseResult, ...]:
    if not failures:
        return (
            PhaseResult(
                phase=Phase.TEARDOWN,
                status=Status.PASS,
                duration=duration,
                stdout=stdout,
                stderr=stderr,
            ),
        )
    phases: list[PhaseResult] = []
    for index, failure in enumerate(failures):
        message, exception_type, _formatted = _exception_fields(failure.exception)
        phases.append(
            PhaseResult(
                phase=Phase.TEARDOWN,
                status=Status.ERROR_TEARDOWN,
                duration=duration if index == 0 else 0.0,
                message=f"fixture {failure.fixture_name!r} teardown failed: {message}",
                exception_type=exception_type,
                traceback=failure.traceback,
                stdout=stdout if index == 0 else "",
                stderr=stderr if index == 0 else "",
            )
        )
    return tuple(phases)


def _coerce_collected(
    test: CollectedTest | TestSpec,
    collection_cache: dict[str, Any] | None = None,
) -> CollectedTest:
    if isinstance(test, CollectedTest):
        return test
    collection = collection_cache.get(test.path) if collection_cache is not None else None
    if collection is None:
        collection = discover(test.path)
        if collection_cache is not None:
            collection_cache[test.path] = collection
    try:
        discovered = collection.by_id(test.id)
    except KeyError:
        candidates = [
            item
            for item in collection.items
            if item.spec.function_name == test.function_name and item.spec.case_id == test.case_id
        ]
        if len(candidates) != 1:
            issue_text = "; ".join(issue.message for issue in collection.issues)
            detail = f" ({issue_text})" if issue_text else ""
            raise NativeExecutionError(f"cannot load native test {test.id!r}{detail}") from None
        discovered = candidates[0]
    return CollectedTest(test, discovered.function, collection.registry)


async def _execute_one(
    collected: CollectedTest,
    runtime: FixtureRuntime,
    *,
    attempt: int,
    worker_id: str,
    close_module: bool,
    close_session: bool,
) -> TestResult:
    spec = collected.spec
    started_at = time.time()
    started_monotonic = time.monotonic()
    phases: list[PhaseResult] = []

    if spec.skip_reason is not None:
        phases.append(
            PhaseResult(
                phase=Phase.CALL,
                status=Status.SKIP,
                duration=0.0,
                message=spec.skip_reason,
            )
        )
        finished_at = time.time()
        duration = time.monotonic() - started_monotonic
        attempt_result = AttemptResult(
            test_id=spec.id,
            attempt=attempt,
            worker_id=worker_id,
            status=Status.SKIP,
            duration=duration,
            phases=tuple(phases),
            started_at=started_at,
            finished_at=finished_at,
        )
        return TestResult(spec, Status.SKIP, (attempt_result,), duration)

    setup_stdout = io.StringIO()
    setup_stderr = io.StringIO()
    setup_started = time.monotonic()
    runtime_started = False
    setup_error: BaseException | None = None
    kwargs: dict[str, Any] = {}
    try:
        runtime.begin_test(spec.id, spec.module_name)
        runtime_started = True
        with redirect_stdout(setup_stdout), redirect_stderr(setup_stderr):
            kwargs = await runtime.resolve_arguments(
                collected.function,
                spec.parameters,
                module_name=spec.module_name,
            )
    except asyncio.CancelledError:
        raise
    except BaseException as error:
        setup_error = error
        phases.append(
            _failed_phase(
                Phase.SETUP,
                Status.ERROR_SETUP,
                error,
                time.monotonic() - setup_started,
                setup_stdout.getvalue(),
                setup_stderr.getvalue(),
            )
        )
    else:
        phases.append(
            PhaseResult(
                phase=Phase.SETUP,
                status=Status.PASS,
                duration=time.monotonic() - setup_started,
                stdout=setup_stdout.getvalue(),
                stderr=setup_stderr.getvalue(),
            )
        )

    call_status: Status | None = None
    if setup_error is None:
        call_stdout = io.StringIO()
        call_stderr = io.StringIO()
        call_started = time.monotonic()
        try:
            with redirect_stdout(call_stdout), redirect_stderr(call_stderr):
                await _invoke_test(collected.function, kwargs, spec.timeout)
        except TestTimeoutError as error:
            call_status = Status.TIMEOUT
            phases.append(
                _failed_phase(
                    Phase.CALL,
                    Status.TIMEOUT,
                    error,
                    time.monotonic() - call_started,
                    call_stdout.getvalue(),
                    call_stderr.getvalue(),
                )
            )
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            call_status = Status.XFAIL if spec.xfail_reason is not None else Status.FAIL
            phases.append(
                _failed_phase(
                    Phase.CALL,
                    call_status,
                    error,
                    time.monotonic() - call_started,
                    call_stdout.getvalue(),
                    call_stderr.getvalue(),
                    message_prefix=spec.xfail_reason,
                )
            )
        else:
            call_status = Status.XPASS if spec.xfail_reason is not None else Status.PASS
            phases.append(
                PhaseResult(
                    phase=Phase.CALL,
                    status=call_status,
                    duration=time.monotonic() - call_started,
                    message=spec.xfail_reason if call_status is Status.XPASS else None,
                    stdout=call_stdout.getvalue(),
                    stderr=call_stderr.getvalue(),
                )
            )

    teardown_failures: list[TeardownFailure] = []
    teardown_duration = 0.0
    teardown_stdout = ""
    teardown_stderr = ""
    if runtime_started:
        failures, duration, stdout, stderr = await _capture_teardown(runtime.finish_test)
        teardown_failures.extend(failures)
        teardown_duration += duration
        teardown_stdout += stdout
        teardown_stderr += stderr
    if close_module:
        failures, duration, stdout, stderr = await _capture_teardown(
            lambda: runtime.close_module(spec.module_name)
        )
        teardown_failures.extend(failures)
        teardown_duration += duration
        teardown_stdout += stdout
        teardown_stderr += stderr
    if close_session:
        failures, duration, stdout, stderr = await _capture_teardown(runtime.close_session)
        teardown_failures.extend(failures)
        teardown_duration += duration
        teardown_stdout += stdout
        teardown_stderr += stderr
    phases.extend(
        _teardown_phases(
            teardown_failures,
            teardown_duration,
            teardown_stdout,
            teardown_stderr,
        )
    )

    if setup_error is not None:
        status = Status.ERROR_SETUP
    elif teardown_failures:
        status = Status.ERROR_TEARDOWN
    else:
        status = call_status or Status.INFRA_ERROR
    finished_at = time.time()
    duration = time.monotonic() - started_monotonic
    attempt_result = AttemptResult(
        test_id=spec.id,
        attempt=attempt,
        worker_id=worker_id,
        status=status,
        duration=duration,
        phases=tuple(phases),
        started_at=started_at,
        finished_at=finished_at,
    )
    return TestResult(spec, status, (attempt_result,), duration)


async def execute_test_async(
    test: CollectedTest | TestSpec,
    *,
    registry: FixtureRegistry | None = None,
    runtime: FixtureRuntime | None = None,
    attempt: int = 1,
    worker_id: str | None = None,
) -> TestResult:
    """Execute one test and close all fixture scopes owned by the call."""

    collected = _coerce_collected(test)
    effective_registry = registry or collected.registry
    own_runtime = runtime is None
    effective_runtime = runtime or FixtureRuntime(effective_registry)
    return await _execute_one(
        CollectedTest(collected.spec, collected.function, effective_registry),
        effective_runtime,
        attempt=attempt,
        worker_id=worker_id or _worker_id(),
        close_module=own_runtime,
        close_session=own_runtime,
    )


def execute_test(
    test: CollectedTest | TestSpec,
    *,
    registry: FixtureRegistry | None = None,
    attempt: int = 1,
    worker_id: str | None = None,
) -> TestResult:
    """Synchronous entry point for one native test."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "execute_test() cannot run inside an event loop; use execute_test_async()"
        )
    return asyncio.run(
        execute_test_async(
            test,
            registry=registry,
            attempt=attempt,
            worker_id=worker_id,
        )
    )


def _merge_registries(
    tests: Sequence[CollectedTest], explicit: FixtureRegistry | None
) -> FixtureRegistry:
    registry = FixtureRegistry(explicit.definitions if explicit is not None else ())
    for test in tests:
        for definition in test.registry.definitions:
            if definition in registry.definitions:
                continue
            registry.add(definition)
    return registry


def _append_scope_teardown(
    result: TestResult,
    failures: Sequence[TeardownFailure],
    duration: float,
    stdout: str,
    stderr: str,
) -> TestResult:
    phases = _teardown_phases(failures, duration, stdout, stderr)
    previous_attempt = result.attempts[-1]
    status = (
        previous_attempt.status
        if previous_attempt.status is Status.ERROR_SETUP or not failures
        else Status.ERROR_TEARDOWN
    )
    updated_attempt = replace(
        previous_attempt,
        status=status,
        duration=previous_attempt.duration + duration,
        phases=(*previous_attempt.phases, *phases),
        finished_at=time.time(),
    )
    return replace(
        result,
        status=status,
        attempts=(*result.attempts[:-1], updated_attempt),
        duration=result.duration + duration,
    )


async def _notify_result(
    callback: Callable[[TestResult], Any] | None,
    result: TestResult,
) -> None:
    if callback is None:
        return
    notification = callback(result)
    if inspect.isawaitable(notification):
        await notification


async def execute_tests_async(
    tests: Iterable[CollectedTest | TestSpec],
    *,
    registry: FixtureRegistry | None = None,
    attempt: int = 1,
    worker_id: str | None = None,
    on_result: Callable[[TestResult], Any] | None = None,
) -> tuple[TestResult, ...]:
    """Execute tests sequentially while reusing module and session fixtures."""

    collection_cache: dict[str, Any] = {}
    collected = tuple(_coerce_collected(test, collection_cache) for test in tests)
    effective_registry = _merge_registries(collected, registry)
    runtime = FixtureRuntime(effective_registry)
    remaining = Counter(test.spec.module_name for test in collected)
    results: list[TestResult] = []
    effective_worker = worker_id or _worker_id()

    for item in collected:
        bound = CollectedTest(item.spec, item.function, effective_registry)
        result = await _execute_one(
            bound,
            runtime,
            attempt=attempt,
            worker_id=effective_worker,
            close_module=False,
            close_session=False,
        )
        results.append(result)
        current_index = len(results) - 1
        updated_indices: list[int] = []
        remaining[item.spec.module_name] -= 1
        if remaining[item.spec.module_name] == 0:
            module_name = item.spec.module_name

            async def close_current_module(
                name: str = module_name,
            ) -> tuple[TeardownFailure, ...]:
                return await runtime.close_module(name)

            failures, duration, stdout, stderr = await _capture_teardown(close_current_module)
            if failures or stdout or stderr:
                result_index = {completed.test.id: index for index, completed in enumerate(results)}
                grouped: dict[int, list[TeardownFailure]] = {}
                for failure in failures:
                    owner_test_id = failure.owner_test_id
                    owner_index = (
                        current_index
                        if owner_test_id is None
                        else result_index.get(owner_test_id, current_index)
                    )
                    grouped.setdefault(owner_index, []).append(failure)
                if not grouped:
                    grouped[current_index] = []
                first_update = True
                for owner_index, owned_failures in grouped.items():
                    results[owner_index] = _append_scope_teardown(
                        results[owner_index],
                        owned_failures,
                        duration if first_update else 0.0,
                        stdout if first_update else "",
                        stderr if first_update else "",
                    )
                    updated_indices.append(owner_index)
                    first_update = False
        if current_index not in updated_indices:
            await _notify_result(on_result, results[current_index])
        for updated_index in updated_indices:
            await _notify_result(on_result, results[updated_index])

    failures, duration, stdout, stderr = await _capture_teardown(runtime.close_session)
    if results and (failures or stdout or stderr):
        result_index = {result.test.id: index for index, result in enumerate(results)}
        session_grouped: dict[int, list[TeardownFailure]] = {}
        fallback_index = len(results) - 1
        for failure in failures:
            owner_test_id = failure.owner_test_id
            index = (
                fallback_index
                if owner_test_id is None
                else result_index.get(owner_test_id, fallback_index)
            )
            session_grouped.setdefault(index, []).append(failure)
        if not session_grouped:
            session_grouped[fallback_index] = []

        first_update = True
        for index, owned_failures in session_grouped.items():
            results[index] = _append_scope_teardown(
                results[index],
                owned_failures,
                duration if first_update else 0.0,
                stdout if first_update else "",
                stderr if first_update else "",
            )
            first_update = False
            await _notify_result(on_result, results[index])
    return tuple(results)


def execute_tests(
    tests: Iterable[CollectedTest | TestSpec],
    *,
    registry: FixtureRegistry | None = None,
    attempt: int = 1,
    worker_id: str | None = None,
    on_result: Callable[[TestResult], Any] | None = None,
) -> tuple[TestResult, ...]:
    """Synchronous batch entry point preserving shared fixture scopes."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "execute_tests() cannot run inside an event loop; use execute_tests_async()"
        )
    return asyncio.run(
        execute_tests_async(
            tests,
            registry=registry,
            attempt=attempt,
            worker_id=worker_id,
            on_result=on_result,
        )
    )


run_test = execute_test
run_tests = execute_tests


__all__ = [
    "NativeExecutionError",
    "TestTimeoutError",
    "execute_test",
    "execute_test_async",
    "execute_tests",
    "execute_tests_async",
    "run_test",
    "run_tests",
]
