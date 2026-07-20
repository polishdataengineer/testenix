from __future__ import annotations

import asyncio
from pathlib import Path
from textwrap import dedent

import pytest

from testenix.contracts import Phase, Scope, Status
from testenix.discovery import CollectionResult, discover
from testenix.executor import execute_test, execute_test_async, execute_tests


def write_test_module(tmp_path: Path, source: str, name: str = "test_example.py") -> Path:
    path = tmp_path / name
    path.write_text(dedent(source), encoding="utf-8")
    return path


def test_discovery_materialises_decorated_cases_and_metadata(tmp_path: Path) -> None:
    path = write_test_module(
        tmp_path,
        """
        from testenix.api import case, cases, fixture, skip, test, xfail

        @fixture(scope="module")
        def database():
            return object()

        @test("user can log in", tags={"auth", "unit"}, timeout=0.25)
        @cases(
            case(id="admin", role="admin"),
            case(id="editor", role="editor"),
        )
        def login(database, role):
            pass

        @skip("not available on this platform")
        def test_skipped():
            pass

        @xfail("known defect")
        @test
        def differently_named():
            pass
        """,
    )

    result = discover(path)

    assert isinstance(result, CollectionResult)
    assert not result.issues
    assert len(result.fixtures) == 1
    assert result.fixtures[0].scope is Scope.MODULE
    login_specs = [spec for spec in result.tests if spec.function_name == "login"]
    assert {spec.case_id for spec in login_specs} == {"admin", "editor"}
    assert {spec.parameters["role"] for spec in login_specs} == {"admin", "editor"}
    assert all(spec.display_name.startswith("user can log in") for spec in login_specs)
    assert all(spec.tags == frozenset({"auth", "unit"}) for spec in login_specs)
    assert all(spec.timeout == 0.25 for spec in login_specs)
    skipped = next(spec for spec in result.tests if spec.function_name == "test_skipped")
    expected = next(spec for spec in result.tests if spec.function_name == "differently_named")
    assert skipped.skip_reason == "not available on this platform"
    assert expected.xfail_reason == "known defect"


def test_directory_discovery_uses_test_file_pattern_and_ignores_imported_tests(
    tmp_path: Path,
) -> None:
    (tmp_path / "helper.py").write_text(
        "def test_imported():\n    raise AssertionError('must not be collected')\n",
        encoding="utf-8",
    )
    write_test_module(
        tmp_path,
        """
        from helper import test_imported

        def test_local():
            pass
        """,
    )
    write_test_module(
        tmp_path,
        """
        from testenix.api import test

        @test
        def explicit_but_file_is_not_a_test():
            pass
        """,
        name="support.py",
    )

    directory_result = discover(tmp_path)
    explicit_result = discover(tmp_path / "support.py")

    assert [spec.function_name for spec in directory_result.tests] == ["test_local"]
    assert [spec.function_name for spec in explicit_result.tests] == [
        "explicit_but_file_is_not_a_test"
    ]


def test_specs_use_checkout_relative_paths_and_stable_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    write_test_module(tests_dir, "def test_portable():\n    pass\n", name="test_portable.py")
    monkeypatch.chdir(tmp_path)

    result = discover("tests")

    assert result.tests[0].path == "tests/test_portable.py"
    assert result.tests[0].id == "tests/test_portable.py::test_portable"


def test_implicit_case_id_is_stable_across_fresh_module_imports(tmp_path: Path) -> None:
    path = write_test_module(
        tmp_path,
        """
        from testenix import case

        @case(value=object())
        def test_dynamic_value(value):
            assert value is not None
        """,
    )

    first = discover(path).tests[0]
    second = discover(path).tests[0]

    assert first.case_id == second.case_id == "case-1"
    assert first.id == second.id


def test_explicitly_imported_fixture_is_available_to_the_test_module(tmp_path: Path) -> None:
    (tmp_path / "shared.py").write_text(
        dedent(
            """
            from testenix.api import fixture

            @fixture
            def shared_value():
                return 42
            """
        ),
        encoding="utf-8",
    )
    path = write_test_module(
        tmp_path,
        """
        from shared import shared_value

        def test_imported_fixture(shared_value):
            assert shared_value == 42
        """,
    )

    collection = discover(path)
    result = execute_test(collection.items[0])

    assert not collection.issues
    assert result.status is Status.PASS


def test_discovery_does_not_convert_keyboard_interrupt_to_collection_issue(
    tmp_path: Path,
) -> None:
    path = write_test_module(tmp_path, "raise KeyboardInterrupt\n")

    with pytest.raises(KeyboardInterrupt):
        discover(path)


def test_imported_fixture_alias_brings_its_typed_dependency_closure(tmp_path: Path) -> None:
    (tmp_path / "shared_closure_fixtures.py").write_text(
        dedent(
            """
            from testenix import fixture

            class Database:
                value = 42

            @fixture
            def database() -> Database:
                return Database()

            @fixture
            def service(db: Database) -> int:
                return db.value
            """
        ),
        encoding="utf-8",
    )
    path = write_test_module(
        tmp_path,
        """
        from shared_closure_fixtures import service as api_client

        def test_shared_alias(api_client):
            assert api_client == 42
        """,
    )

    collection = discover(path)
    result = execute_test(collection.items[0])

    assert not collection.issues
    assert {definition.name for definition in collection.fixtures} == {
        "api_client",
        "database",
    }
    assert result.status is Status.PASS


def test_sync_and_async_tests_capture_output(tmp_path: Path) -> None:
    path = write_test_module(
        tmp_path,
        """
        import sys
        from testenix.api import test

        def test_sync():
            print("sync stdout")
            print("sync stderr", file=sys.stderr)

        @test("async works")
        async def arbitrary_async_name():
            print("async stdout")
        """,
    )
    collection = discover(path)

    results = execute_tests(collection.items)

    assert [result.status for result in results] == [Status.PASS, Status.PASS]
    call_phases = [
        next(phase for phase in result.attempts[0].phases if phase.phase is Phase.CALL)
        for result in results
    ]
    assert {phase.stdout.strip() for phase in call_phases} == {"sync stdout", "async stdout"}
    sync_call = next(phase for phase in call_phases if "sync stdout" in phase.stdout)
    assert sync_call.stderr.strip() == "sync stderr"


def test_fixture_dag_resolves_by_name_and_type_and_reuses_scopes(tmp_path: Path) -> None:
    path = write_test_module(
        tmp_path,
        """
        from collections.abc import AsyncIterator, Iterator
        from testenix.api import fixture, test

        EVENTS = []

        class Token:
            pass

        @fixture(scope="session")
        def root() -> Iterator[Token]:
            EVENTS.append("root setup")
            yield Token()
            EVENTS.append("root teardown")

        @fixture(scope="module")
        async def middle(token: Token) -> AsyncIterator[str]:
            EVENTS.append("middle setup")
            yield "middle"
            EVENTS.append("middle teardown")

        @fixture
        def leaf(middle):
            EVENTS.append("leaf setup")
            yield middle + " leaf"
            EVENTS.append("leaf teardown")

        @test
        async def first(leaf):
            EVENTS.append("first " + leaf)

        @test
        def second(token: Token):
            EVENTS.append("second token")
        """,
    )
    collection = discover(path)

    results = execute_tests(collection.items)

    assert [result.status for result in results] == [Status.PASS, Status.PASS]
    events = collection.items[0].function.__globals__["EVENTS"]
    assert events == [
        "root setup",
        "middle setup",
        "leaf setup",
        "first middle leaf",
        "leaf teardown",
        "second token",
        "middle teardown",
        "root teardown",
    ]


def test_batch_executor_accepts_plain_specs_and_collects_each_module_once(tmp_path: Path) -> None:
    path = write_test_module(
        tmp_path,
        """
        from testenix.api import fixture

        @fixture(scope="module")
        def value():
            return 7

        def test_one(value):
            assert value == 7

        def test_two(value):
            assert value == 7
        """,
    )
    collection = discover(path)

    results = execute_tests(collection.tests, attempt=3)

    assert [result.status for result in results] == [Status.PASS, Status.PASS]
    assert [result.attempts[0].attempt for result in results] == [3, 3]


def test_imported_session_fixture_is_shared_between_test_modules(tmp_path: Path) -> None:
    (tmp_path / "shared_scope_fixtures.py").write_text(
        dedent(
            """
            from testenix.api import fixture

            EVENTS = []

            @fixture(scope="session")
            def resource():
                EVENTS.append("setup")
                yield object()
                EVENTS.append("teardown")
            """
        ),
        encoding="utf-8",
    )
    for suffix in ("a", "b"):
        write_test_module(
            tmp_path,
            f"""
            from shared_scope_fixtures import EVENTS, resource

            def test_{suffix}(resource):
                EVENTS.append("test {suffix}")
            """,
            name=f"test_{suffix}.py",
        )
    collection = discover(tmp_path)

    results = execute_tests(collection.items)

    assert [result.status for result in results] == [Status.PASS, Status.PASS]
    shared_events = collection.fixtures[0].function.__globals__["EVENTS"]
    assert shared_events == ["setup", "test a", "test b", "teardown"]


def test_session_teardown_failure_is_assigned_to_its_last_actual_user(
    tmp_path: Path,
) -> None:
    path = write_test_module(
        tmp_path,
        """
        from testenix import fixture

        @fixture(scope="session")
        def fragile_session():
            yield object()
            raise RuntimeError("session cleanup failed")

        @fixture(scope="module")
        def bridge(fragile_session):
            return fragile_session

        def test_first_user(bridge):
            pass

        def test_last_user_through_cached_module_fixture(bridge):
            pass

        def test_unrelated():
            pass
        """,
    )
    collection = discover(path)
    notifications = []

    results = execute_tests(collection.items, on_result=notifications.append)

    by_name = {result.test.function_name: result for result in results}
    owner = by_name["test_last_user_through_cached_module_fixture"]
    assert by_name["test_first_user"].status is Status.PASS
    assert owner.status is Status.ERROR_TEARDOWN
    assert by_name["test_unrelated"].status is Status.PASS
    teardown = [
        phase
        for phase in owner.attempts[0].phases
        if phase.phase is Phase.TEARDOWN and phase.status is Status.ERROR_TEARDOWN
    ]
    assert len(teardown) == 1
    assert "session cleanup failed" in (teardown[0].message or "")
    assert [notification.test.function_name for notification in notifications] == [
        "test_first_user",
        "test_last_user_through_cached_module_fixture",
        "test_unrelated",
        "test_last_user_through_cached_module_fixture",
    ]
    assert notifications[-1].status is Status.ERROR_TEARDOWN


def test_module_teardown_failure_is_assigned_to_its_last_actual_user(
    tmp_path: Path,
) -> None:
    path = write_test_module(
        tmp_path,
        """
        from testenix import fixture

        @fixture(scope="module")
        def fragile_module():
            yield object()
            raise RuntimeError("module cleanup failed")

        def test_fixture_user(fragile_module):
            pass

        def test_unrelated():
            pass
        """,
    )
    collection = discover(path)
    notifications = []

    results = execute_tests(collection.items, on_result=notifications.append)

    by_name = {result.test.function_name: result for result in results}
    assert by_name["test_fixture_user"].status is Status.ERROR_TEARDOWN
    assert by_name["test_unrelated"].status is Status.PASS
    assert [notification.test.function_name for notification in notifications] == [
        "test_fixture_user",
        "test_unrelated",
        "test_fixture_user",
    ]
    assert notifications[-1].status is Status.ERROR_TEARDOWN


def test_async_test_cancellation_propagates_to_embedder(tmp_path: Path) -> None:
    path = write_test_module(
        tmp_path,
        """
        import asyncio

        async def test_waits_forever():
            await asyncio.sleep(30)
        """,
    )
    collected = discover(path).items[0]

    async def cancel_call() -> None:
        task = asyncio.create_task(execute_test_async(collected))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_call())


def test_async_fixture_teardown_cancellation_propagates(tmp_path: Path) -> None:
    path = write_test_module(
        tmp_path,
        """
        import asyncio
        from testenix import fixture

        teardown_started = False

        @fixture
        async def resource():
            global teardown_started
            yield object()
            teardown_started = True
            await asyncio.sleep(30)

        async def test_uses_resource(resource):
            assert resource is not None
        """,
    )
    collected = discover(path).items[0]

    async def cancel_teardown() -> None:
        task = asyncio.create_task(execute_test_async(collected))
        deadline = asyncio.get_running_loop().time() + 2.0
        while not collected.function.__globals__["teardown_started"]:
            if asyncio.get_running_loop().time() >= deadline:
                task.cancel()
                raise AssertionError("fixture teardown did not start")
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_teardown())


def test_sync_tests_and_fixtures_do_not_inherit_internal_event_loop(tmp_path: Path) -> None:
    path = write_test_module(
        tmp_path,
        """
        import asyncio
        from testenix import fixture

        async def answer():
            await asyncio.sleep(0)
            return 42

        @fixture
        def resource():
            return asyncio.run(answer())

        def test_can_own_an_event_loop(resource):
            assert resource == 42
            assert asyncio.run(answer()) == 42
        """,
    )

    result = execute_test(discover(path).items[0])

    assert result.status is Status.PASS


def test_unawaited_async_task_is_contained_and_fails_its_owner(tmp_path: Path) -> None:
    path = write_test_module(
        tmp_path,
        """
        import asyncio

        async def explode_later():
            await asyncio.sleep(0.1)
            raise RuntimeError("background exploded")

        async def test_leaks_task():
            asyncio.create_task(explode_later())

        async def test_neighbor_stays_clean():
            await asyncio.sleep(0.15)
        """,
    )

    results = execute_tests(discover(path).items)

    assert [result.status for result in results] == [Status.FAIL, Status.PASS]
    first_message = next(
        phase.message for phase in results[0].attempts[0].phases if phase.phase is Phase.CALL
    )
    assert first_message is not None
    assert "unfinished asyncio background task" in first_message
    assert all(
        "background exploded" not in phase.stderr
        for result in results
        for phase in result.attempts[0].phases
    )


def test_fixture_cycle_is_a_setup_error_with_readable_path(tmp_path: Path) -> None:
    path = write_test_module(
        tmp_path,
        """
        from testenix.api import fixture

        @fixture
        def alpha(beta):
            return beta

        @fixture
        def beta(alpha):
            return alpha

        def test_cycle(alpha):
            pass
        """,
    )
    collection = discover(path)

    result = execute_test(collection.items[0])

    assert result.status is Status.ERROR_SETUP
    setup = next(phase for phase in result.attempts[0].phases if phase.phase is Phase.SETUP)
    assert setup.status is Status.ERROR_SETUP
    assert "alpha -> beta -> alpha" in (setup.message or "")


def test_scope_violation_is_reported_during_setup(tmp_path: Path) -> None:
    path = write_test_module(
        tmp_path,
        """
        from testenix.api import fixture

        @fixture
        def short_lived():
            return object()

        @fixture(scope="session")
        def too_broad(short_lived):
            return short_lived

        def test_invalid(too_broad):
            pass
        """,
    )
    collection = discover(path)

    result = execute_test(collection.items[0])

    assert result.status is Status.ERROR_SETUP
    setup = result.attempts[0].phases[0]
    assert "cannot depend on shorter-lived" in (setup.message or "")


def test_call_failure_and_all_teardown_failures_are_preserved(tmp_path: Path) -> None:
    path = write_test_module(
        tmp_path,
        """
        import sys
        from testenix.api import fixture

        @fixture
        def inner():
            yield "inner"
            print("inner cleanup", file=sys.stderr)
            raise ValueError("inner exploded")

        @fixture
        def outer(inner):
            yield inner
            print("outer cleanup")
            raise RuntimeError("outer exploded")

        def test_everything(outer):
            print("call output")
            raise AssertionError("call exploded")
        """,
    )
    collection = discover(path)

    result = execute_test(collection.items[0])

    assert result.status is Status.ERROR_TEARDOWN
    phases = result.attempts[0].phases
    call = next(phase for phase in phases if phase.phase is Phase.CALL)
    teardown_errors = [
        phase
        for phase in phases
        if phase.phase is Phase.TEARDOWN and phase.status is Status.ERROR_TEARDOWN
    ]
    assert call.status is Status.FAIL
    assert "call exploded" in (call.message or "")
    assert call.stdout.strip() == "call output"
    assert len(teardown_errors) == 2
    assert "outer exploded" in (teardown_errors[0].message or "")
    assert "inner exploded" in (teardown_errors[1].message or "")
    assert "outer cleanup" in teardown_errors[0].stdout
    assert "inner cleanup" in teardown_errors[0].stderr


def test_skip_xfail_and_xpass_have_distinct_outcomes(tmp_path: Path) -> None:
    path = write_test_module(
        tmp_path,
        """
        from testenix.api import fixture, skip, xfail

        EVENTS = []

        @fixture
        def should_not_start():
            EVENTS.append("started")
            raise RuntimeError("boom")

        @skip("later")
        def test_skipped(should_not_start):
            pass

        @xfail("known")
        def test_expected_failure():
            raise ValueError("expected boom")

        @xfail("known")
        def test_unexpected_pass():
            pass
        """,
    )
    collection = discover(path)

    results = execute_tests(collection.items)
    by_name = {result.test.function_name: result for result in results}

    assert by_name["test_skipped"].status is Status.SKIP
    assert by_name["test_expected_failure"].status is Status.XFAIL
    assert by_name["test_unexpected_pass"].status is Status.XPASS
    assert collection.items[0].function.__globals__["EVENTS"] == []


def test_async_timeout_is_a_first_class_status(tmp_path: Path) -> None:
    path = write_test_module(
        tmp_path,
        """
        import asyncio
        from testenix.api import test

        @test(timeout=0.01)
        async def eventually():
            await asyncio.sleep(1)
        """,
    )
    collection = discover(path)

    result = execute_test(collection.items[0])

    assert result.status is Status.TIMEOUT
    call = next(phase for phase in result.attempts[0].phases if phase.phase is Phase.CALL)
    assert call.status is Status.TIMEOUT


def test_invalid_case_is_a_collection_issue_not_a_runtime_surprise(tmp_path: Path) -> None:
    path = write_test_module(
        tmp_path,
        """
        from testenix.api import case

        @case(unknown=1)
        def test_bad(expected):
            pass
        """,
    )

    collection = discover(path)

    assert not collection.items
    assert len(collection.issues) == 1
    assert "unknown parameters: unknown" in collection.issues[0].message


def test_decorator_validation_is_eager() -> None:
    from testenix.api import cases, test

    with pytest.raises(TypeError, match="tags must be strings"):
        test(tags={"unit", 42})
    with pytest.raises(ValueError, match="greater than zero"):
        test(timeout=0)
    with pytest.raises(ValueError, match="number of case ids"):
        cases({"x": 1}, {"x": 2}, ids=["one"])
