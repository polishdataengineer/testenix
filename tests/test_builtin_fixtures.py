from __future__ import annotations

import os
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import pytest

import testenix.builtin_fixtures as builtin_fixtures
from testenix.api import fixture, get_fixture_metadata
from testenix.builtin_fixtures import MonkeyPatch
from testenix.config import TestenixConfig
from testenix.contracts import Scope, Status
from testenix.discovery import discover
from testenix.executor import execute_tests
from testenix.fixtures import FixtureRegistry
from testenix.runner import run


def write_test_module(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "test_builtins.py"
    path.write_text(dedent(source), encoding="utf-8")
    return path


def test_tmp_path_is_unique_absolute_and_removed_after_each_test(tmp_path: Path) -> None:
    path = write_test_module(
        tmp_path,
        """
        from pathlib import Path

        SEEN = []

        def test_first(tmp_path):
            assert isinstance(tmp_path, Path)
            assert tmp_path.is_absolute()
            assert tmp_path.is_dir()
            artifact = tmp_path / "read-only.txt"
            artifact.write_text("temporary", encoding="utf-8")
            artifact.chmod(0o400)
            SEEN.append(tmp_path)

        def test_second(tmp_path):
            assert not SEEN[0].exists()
            assert tmp_path != SEEN[0]
            assert tmp_path.is_dir()
            SEEN.append(tmp_path)
        """,
    )
    collection = discover(path)

    results = execute_tests(collection.items)

    assert not collection.issues
    assert [result.status for result in results] == [Status.PASS, Status.PASS]
    seen = collection.items[0].function.__globals__["SEEN"]
    assert len(set(seen)) == 2
    assert all(not temporary_path.exists() for temporary_path in seen)


def test_user_fixtures_override_name_only_builtins(tmp_path: Path) -> None:
    path = write_test_module(
        tmp_path,
        """
        from pathlib import Path
        from testenix import fixture

        @fixture
        def tmp_path():
            return Path("user-path")

        @fixture
        def monkeypatch():
            return "user-monkeypatch"

        def test_overrides(tmp_path, monkeypatch):
            assert tmp_path == Path("user-path")
            assert monkeypatch == "user-monkeypatch"
        """,
    )
    collection = discover(path)

    results = execute_tests(collection.items)

    assert not collection.issues
    assert [definition.name for definition in collection.fixtures] == [
        "tmp_path",
        "monkeypatch",
    ]
    assert results[0].status is Status.PASS


def test_monkeypatch_rolls_back_lifo_after_a_test_failure(tmp_path: Path) -> None:
    environment_name = f"TESTENIX_BUILTIN_{os.getpid()}_{tmp_path.name}"
    os.environ.pop(environment_name, None)
    path = write_test_module(
        tmp_path,
        f"""
        import os
        import sys

        VALUE = "original"
        ENVIRONMENT_NAME = {environment_name!r}

        class Settings:
            value = "class-original"

        def test_mutates_then_fails(monkeypatch):
            module = sys.modules[__name__]
            monkeypatch.setattr(module, "VALUE", "first")
            monkeypatch.setattr(module, "VALUE", "second")
            monkeypatch.setattr(module, "CREATED", 42, raising=False)
            monkeypatch.setattr(f"{{__name__}}.Settings.value", "class-patched")
            monkeypatch.setenv(ENVIRONMENT_NAME, "first")
            monkeypatch.setenv(ENVIRONMENT_NAME, "second")
            assert VALUE == "second"
            assert Settings.value == "class-patched"
            assert os.environ[ENVIRONMENT_NAME] == "second"
            raise AssertionError("intentional failure")

        def test_observes_restored_state():
            assert VALUE == "original"
            assert "CREATED" not in globals()
            assert Settings.value == "class-original"
            assert ENVIRONMENT_NAME not in os.environ
        """,
    )
    collection = discover(path)

    results = execute_tests(collection.items)

    assert not collection.issues
    assert [result.status for result in results] == [Status.FAIL, Status.PASS]
    assert environment_name not in os.environ


def test_monkeypatch_dependency_rolls_back_after_autouse_setup_failure(
    tmp_path: Path,
) -> None:
    path = write_test_module(
        tmp_path,
        """
        import sys
        from testenix import fixture

        VALUE = "original"
        ATTEMPT = 0

        @fixture(autouse=True)
        def sometimes_broken(monkeypatch):
            global ATTEMPT
            assert VALUE == "original"
            monkeypatch.setattr(sys.modules[__name__], "VALUE", "patched")
            ATTEMPT += 1
            if ATTEMPT == 1:
                raise RuntimeError("setup failed")

        def test_first():
            raise AssertionError("call must not run")

        def test_second():
            assert VALUE == "patched"
        """,
    )
    collection = discover(path)

    results = execute_tests(collection.items)

    assert not collection.issues
    assert [result.status for result in results] == [Status.ERROR_SETUP, Status.PASS]
    assert collection.items[0].function.__globals__["VALUE"] == "original"


def test_monkeypatch_raising_prepend_manual_undo_and_created_attribute() -> None:
    environment_name = f"TESTENIX_MONKEYPATCH_{os.getpid()}"
    os.environ[environment_name] = "old"
    target = type("Target", (), {})()
    patcher = MonkeyPatch()
    try:
        with pytest.raises(AttributeError):
            patcher.setattr(target, "missing", 1)

        patcher.setattr(target, "missing", 1, raising=False)
        patcher.setenv(environment_name, "new", prepend=os.pathsep)
        assert target.missing == 1
        assert os.environ[environment_name] == f"new{os.pathsep}old"

        patcher.undo()
        patcher.undo()
        assert not hasattr(target, "missing")
        assert os.environ[environment_name] == "old"
    finally:
        os.environ.pop(environment_name, None)


def test_monkeypatch_undo_attempts_later_actions_after_a_failure() -> None:
    environment_name = f"TESTENIX_MONKEYPATCH_FAILURE_{os.getpid()}"
    os.environ.pop(environment_name, None)

    class Fragile:
        fail_restore = False
        value = "original"

        def __setattr__(self, name: str, value: object) -> None:
            if name == "value" and value == "original" and self.fail_restore:
                raise RuntimeError("restore failed")
            object.__setattr__(self, name, value)

    target = Fragile()
    patcher = MonkeyPatch()
    patcher.setenv(environment_name, "patched")
    patcher.setattr(target, "value", "changed")
    target.fail_restore = True

    with pytest.raises(RuntimeError, match="restore failed"):
        patcher.undo()

    assert environment_name not in os.environ
    patcher.undo()


def test_monkeypatch_does_not_record_rejected_attribute_mutation() -> None:
    assignments: list[object] = []

    class RejectingTarget:
        value = "original"

        def __setattr__(self, name: str, value: object) -> None:
            if name == "value":
                assignments.append(value)
                if value == "rejected":
                    raise RuntimeError("patch rejected")
            object.__setattr__(self, name, value)

    target = RejectingTarget()
    patcher = MonkeyPatch()

    with pytest.raises(RuntimeError, match="patch rejected"):
        patcher.setattr(target, "value", "rejected")

    patcher.undo()
    assert target.value == "original"
    assert assignments == ["rejected"]


def test_monkeypatch_does_not_record_rejected_environment_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes: list[tuple[str, str]] = []
    removals: list[str] = []

    class RejectingEnvironment:
        def __contains__(self, name: object) -> bool:
            return False

        def get(self, name: str, default: object = None) -> object:
            return default

        def __setitem__(self, name: str, value: str) -> None:
            writes.append((name, value))
            raise RuntimeError("environment write rejected")

        def pop(self, name: str, default: object = None) -> object:
            removals.append(name)
            return default

    fake_environment = RejectingEnvironment()
    monkeypatch.setattr(
        builtin_fixtures,
        "os",
        SimpleNamespace(environ=fake_environment),
    )
    patcher = MonkeyPatch()

    with pytest.raises(RuntimeError, match="environment write rejected"):
        patcher.setenv("TESTENIX_REJECTED_WRITE", "value")

    patcher.undo()
    assert writes == [("TESTENIX_REJECTED_WRITE", "value")]
    assert removals == []


def test_function_and_module_autouse_run_without_explicit_test_parameters(
    tmp_path: Path,
) -> None:
    path = write_test_module(
        tmp_path,
        """
        from testenix import fixture

        EVENTS = []

        @fixture(scope="module", autouse=True)
        def module_state():
            EVENTS.append("module setup")
            yield "module"
            EVENTS.append("module teardown")

        @fixture(autouse=True)
        def function_state(module_state):
            EVENTS.append("function setup")
            yield module_state + " function"
            EVENTS.append("function teardown")

        def test_first():
            EVENTS.append("first")

        def test_second(function_state):
            assert function_state == "module function"
            EVENTS.append("second")
        """,
    )
    collection = discover(path)

    results = execute_tests(collection.items)

    assert not collection.issues
    assert [result.status for result in results] == [Status.PASS, Status.PASS]
    assert collection.items[0].function.__globals__["EVENTS"] == [
        "module setup",
        "function setup",
        "first",
        "function teardown",
        "function setup",
        "second",
        "function teardown",
        "module teardown",
    ]


def test_spawn_runner_executes_builtin_dependencies_of_autouse(tmp_path: Path) -> None:
    teardown_marker = tmp_path / "autouse-teardown.txt"
    environment_name = f"TESTENIX_SPAWN_{os.getpid()}_{tmp_path.name}"
    path = write_test_module(
        tmp_path,
        f"""
        import os
        from pathlib import Path
        from testenix import fixture

        @fixture(autouse=True)
        def isolated_state(monkeypatch, tmp_path):
            monkeypatch.setenv({environment_name!r}, "worker-value")
            assert tmp_path.is_dir()
            yield
            assert os.environ[{environment_name!r}] == "worker-value"
            assert tmp_path.is_dir()
            Path({str(teardown_marker)!r}).write_text("finished", encoding="utf-8")

        def test_uses_implicit_state():
            assert os.environ[{environment_name!r}] == "worker-value"
        """,
    )

    result = run((str(path),), TestenixConfig(workers=1, history_path=None))

    assert result.exit_code == 0
    assert [test.status for test in result.tests] == [Status.PASS]
    assert teardown_marker.read_text(encoding="utf-8") == "finished"
    assert environment_name not in os.environ


def test_local_non_autouse_fixture_suppresses_global_autouse_override(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    @fixture(name="state", autouse=True)
    def global_state() -> None:
        events.append("global autouse ran")

    path = write_test_module(
        tmp_path,
        """
        from testenix import fixture

        @fixture(name="state")
        def local_state():
            return "local"

        def test_override_is_not_implicit():
            pass
        """,
    )
    collection = discover(path)
    registry = FixtureRegistry()
    registry.register(global_state)

    results = execute_tests(collection.items, registry=registry)

    assert not collection.issues
    assert results[0].status is Status.PASS
    assert events == []


def test_fixture_autouse_metadata_and_validation() -> None:
    @fixture(scope="module", autouse=True)
    def implicit() -> None:
        return None

    metadata = get_fixture_metadata(implicit)

    assert metadata is not None
    assert metadata.scope is Scope.MODULE
    assert metadata.autouse is True
    with pytest.raises(TypeError, match="autouse must be a boolean"):
        fixture(autouse=1)  # type: ignore[arg-type]
