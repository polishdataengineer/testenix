from __future__ import annotations

import ast
import builtins
import hashlib
import os
from collections import Counter
from pathlib import Path
from textwrap import dedent

import pytest

from testenix.contracts import Status
from testenix.discovery import CollectionResult, discover
from testenix.executor import execute_tests
from testenix.migration_models import ConversionBundle, DiagnosticSeverity, SourceFile
from testenix.migration_pytest import convert_pytest_suite, detect_pytest_module


def source_file(
    project_relative: str,
    source: str,
    *,
    migration_relative: str | None = None,
) -> SourceFile:
    text = dedent(source).lstrip()
    relative = Path(project_relative)
    return SourceFile(
        path=Path("/virtual-project") / relative,
        project_relative=relative,
        migration_relative=Path(migration_relative or project_relative),
        sha256=hashlib.sha256(text.encode()).hexdigest(),
        text=text,
    )


def compile_artifacts(bundle: ConversionBundle) -> None:
    for artifact in bundle.artifacts:
        compile(artifact.content, artifact.relative_path.as_posix(), "exec")


def materialize_and_discover(tmp_path: Path, bundle: ConversionBundle) -> CollectionResult:
    for artifact in bundle.artifacts:
        destination = tmp_path / artifact.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(artifact.content, encoding="utf-8")
    return discover(tmp_path)


def test_plain_function_is_copied_and_suffix_name_becomes_discoverable(
    tmp_path: Path,
) -> None:
    source = source_file(
        "tests/math_test.py",
        """
        def test_addition() -> None:
            assert 2 + 3 == 5
        """,
        migration_relative="math_test.py",
    )

    bundle = convert_pytest_suite((source,))

    assert detect_pytest_module(source)
    assert not detect_pytest_module(source_file("tests/helper.py", "VALUE = 42\n"))
    assert not bundle.blocking_diagnostics
    assert [artifact.relative_path.as_posix() for artifact in bundle.artifacts] == ["test_math.py"]
    assert bundle.mappings[0].source_id == "tests/math_test.py::test_addition"
    assert bundle.mappings[0].target_file == "test_math.py"
    assert source.text == "def test_addition() -> None:\n    assert 2 + 3 == 5\n"
    compile_artifacts(bundle)

    collection = materialize_and_discover(tmp_path, bundle)
    assert not collection.issues
    assert len(collection.items) == 1
    assert execute_tests(collection.items)[0].status is Status.PASS


def test_pytest_default_test_prefix_without_underscore_gets_native_decorator(
    tmp_path: Path,
) -> None:
    source = source_file(
        "tests/test_prefix.py",
        """
        def testfoo() -> None:
            assert 6 * 7 == 42
        """,
        migration_relative="test_prefix.py",
    )

    bundle = convert_pytest_suite((source,))

    assert detect_pytest_module(source)
    assert not bundle.blocking_diagnostics
    assert bundle.mappings[0].source_id == "tests/test_prefix.py::testfoo"
    assert "@_testenix_test" in bundle.artifacts[0].content
    collection = materialize_and_discover(tmp_path, bundle)
    assert not collection.issues
    assert len(collection.items) == 1
    assert execute_tests(collection.items)[0].status is Status.PASS


def test_static_parametrize_preserves_ids_and_runs_native_cases(tmp_path: Path) -> None:
    source = source_file(
        "tests/test_math.py",
        """
        import pytest

        @pytest.mark.parametrize(
            ("left", "right", "expected"),
            [
                (1, 2, 3),
                pytest.param(2, 3, 5, id="five"),
            ],
            ids=["three", None],
        )
        def test_add(left: int, right: int, expected: int) -> None:
            assert left + right == expected
        """,
        migration_relative="test_math.py",
    )

    first = convert_pytest_suite((source,))
    second = convert_pytest_suite((source,))

    assert first == second
    assert not first.blocking_diagnostics
    assert {mapping.case_id for mapping in first.mappings} == {"three", "five"}
    assert {mapping.source_id for mapping in first.mappings} == {
        "tests/test_math.py::test_add[three]",
        "tests/test_math.py::test_add[five]",
    }
    compile_artifacts(first)

    collection = materialize_and_discover(tmp_path, first)
    assert not collection.issues
    assert {item.spec.case_id for item in collection.items} == {"three", "five"}
    assert Counter(result.status for result in execute_tests(collection.items)) == {Status.PASS: 2}


def test_direct_pytest_fixture_import_converts_without_name_collision(tmp_path: Path) -> None:
    source = source_file(
        "tests/test_fixture.py",
        """
        from pytest import fixture

        @fixture(scope="function")
        def factor():
            yield 2

        def test_double(factor) -> None:
            assert factor * 3 == 6
        """,
        migration_relative="test_fixture.py",
    )

    bundle = convert_pytest_suite((source,))

    assert not bundle.blocking_diagnostics
    assert "fixture as _testenix_fixture" in bundle.artifacts[0].content
    compile_artifacts(bundle)
    collection = materialize_and_discover(tmp_path, bundle)
    assert not collection.issues
    assert execute_tests(collection.items)[0].status is Status.PASS


def test_decorator_only_suite_drops_pytest_imports_and_runs_without_pytest_importable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = source_file(
        "tests/test_decorator_only.py",
        """
        import pytest
        import pytest_asyncio

        @pytest.fixture
        def factor():
            return 2

        @pytest.mark.parametrize("value", [2, 3], ids=["two", "three"])
        def test_double(factor, value):
            assert factor * value == value + value

        @pytest.mark.skip(reason="static skip")
        def test_skipped():
            raise AssertionError("must not execute")
        """,
        migration_relative="test_decorator_only.py",
    )

    bundle = convert_pytest_suite((source,))

    assert not bundle.blocking_diagnostics
    content = bundle.artifacts[0].content
    assert "import pytest" not in content
    assert "from pytest" not in content
    assert "pytest_asyncio" not in content
    compile_artifacts(bundle)

    normal_import = builtins.__import__

    def reject_pytest_import(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name.split(".", 1)[0] in {"pytest", "pytest_asyncio"}:
            raise AssertionError(f"generated native suite imported {name}")
        return normal_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", reject_pytest_import)
    collection = materialize_and_discover(tmp_path, bundle)
    assert not collection.issues
    assert Counter(result.status for result in execute_tests(collection.items)) == {
        Status.PASS: 2,
        Status.SKIP: 1,
    }


def test_runtime_pytest_helpers_and_partial_from_import_are_retained(tmp_path: Path) -> None:
    source = source_file(
        "tests/test_runtime_helpers.py",
        """
        import pytest as pt
        from pytest import fixture, raises

        @fixture
        def value():
            return 0.1 + 0.2

        def test_runtime_helpers(value):
            with raises(ValueError):
                raise ValueError("expected")
            assert value == pt.approx(0.3)
        """,
        migration_relative="test_runtime_helpers.py",
    )

    bundle = convert_pytest_suite((source,))

    assert not bundle.blocking_diagnostics
    content = bundle.artifacts[0].content
    tree = ast.parse(content)
    pytest_import = next(
        statement
        for statement in tree.body
        if isinstance(statement, ast.Import)
        and any(alias.name == "pytest" for alias in statement.names)
    )
    assert pytest_import.names[0].asname == "pt"
    direct_import = next(
        statement
        for statement in tree.body
        if isinstance(statement, ast.ImportFrom) and statement.module == "pytest"
    )
    assert [(alias.name, alias.asname) for alias in direct_import.names] == [("raises", None)]
    compile_artifacts(bundle)

    collection = materialize_and_discover(tmp_path, bundle)
    assert not collection.issues
    assert execute_tests(collection.items)[0].status is Status.PASS


def test_adjacent_conftest_fixture_becomes_explicit_helper_import(tmp_path: Path) -> None:
    conftest = source_file(
        "tests/conftest.py",
        """
        import pytest

        @pytest.fixture(scope="module")
        def factor():
            return 2
        """,
        migration_relative="conftest.py",
    )
    test_module = source_file(
        "tests/test_conftest_user.py",
        """
        def test_double(factor) -> None:
            assert factor * 4 == 8
        """,
        migration_relative="test_conftest_user.py",
    )

    bundle = convert_pytest_suite((test_module,), (conftest,))

    assert not bundle.blocking_diagnostics
    paths = {artifact.relative_path.as_posix() for artifact in bundle.artifacts}
    assert "test_conftest_user.py" in paths
    helper_paths = {path for path in paths if path.startswith("_testenix_conftest_")}
    assert len(helper_paths) == 1
    compile_artifacts(bundle)

    collection = materialize_and_discover(tmp_path, bundle)
    assert not collection.issues
    assert len(collection.items) == 1
    assert execute_tests(collection.items)[0].status is Status.PASS


def test_adjacent_conftest_autouse_fixture_stays_implicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment_name = "TESTENIX_CONFTEST_AUTOUSE"
    monkeypatch.delenv(environment_name, raising=False)
    conftest = source_file(
        "tests/conftest.py",
        f"""
        import pytest

        @pytest.fixture(autouse=True)
        def environment(monkeypatch):
            monkeypatch.setenv({environment_name!r}, "ready")
        """,
        migration_relative="conftest.py",
    )
    test_module = source_file(
        "tests/test_conftest_autouse.py",
        f"""
        import os

        def test_environment_is_ready() -> None:
            assert os.environ[{environment_name!r}] == "ready"
        """,
        migration_relative="test_conftest_autouse.py",
    )

    bundle = convert_pytest_suite((test_module,), (conftest,))

    assert not bundle.blocking_diagnostics
    helper = next(
        artifact for artifact in bundle.artifacts if artifact.relative_path.name.startswith("_")
    )
    assert "@_testenix_fixture(autouse=True)" in helper.content
    collection = materialize_and_discover(tmp_path, bundle)
    assert not collection.issues
    assert execute_tests(collection.items)[0].status is Status.PASS
    assert environment_name not in os.environ


@pytest.mark.parametrize(
    ("fixture_location", "expected_code"),
    [
        ("module", "PYT509_EVENT_LOOP_POLICY"),
        ("adjacent", "PYT509_EVENT_LOOP_POLICY"),
        ("ancestor", "PYT213_ANCESTOR_CONFTEST"),
    ],
)
def test_custom_event_loop_policy_fixture_blocks_asyncio_migration(
    fixture_location: str,
    expected_code: str,
) -> None:
    fixture_text = """
        import pytest

        @pytest.fixture
        def event_loop_policy():
            return object()
    """
    test_text = """
        import pytest

        @pytest.mark.asyncio
        async def test_async_value():
            pass
    """
    conftests: tuple[SourceFile, ...] = ()
    if fixture_location == "module":
        selected = source_file(
            "tests/test_policy.py",
            fixture_text + test_text,
            migration_relative="test_policy.py",
        )
    else:
        selected_path = (
            "tests/test_policy.py"
            if fixture_location == "adjacent"
            else "tests/unit/test_policy.py"
        )
        selected = source_file(
            selected_path,
            test_text,
            migration_relative="test_policy.py",
        )
        conftests = (
            source_file(
                "tests/conftest.py",
                fixture_text,
                migration_relative="conftest.py",
            ),
        )

    bundle = convert_pytest_suite((selected,), conftests)

    diagnostic = next(
        diagnostic for diagnostic in bundle.blocking_diagnostics if diagnostic.code == expected_code
    )
    assert "event_loop_policy" in diagnostic.message
    assert not bundle.mappings


def test_skip_skipif_and_plain_marker_have_native_outcomes_and_tags(
    tmp_path: Path,
) -> None:
    source = source_file(
        "tests/test_markers.py",
        """
        import pytest

        @pytest.mark.smoke
        def test_tagged() -> None:
            pass

        @pytest.mark.skip(reason="not supported here")
        def test_skipped() -> None:
            raise AssertionError("must not run")

        @pytest.mark.skipif(True, reason="condition is true")
        def test_conditionally_skipped() -> None:
            raise AssertionError("must not run")

        @pytest.mark.skipif(False, reason="condition is false")
        def test_conditionally_executed() -> None:
            pass
        """,
        migration_relative="test_markers.py",
    )

    bundle = convert_pytest_suite((source,))

    assert not bundle.blocking_diagnostics
    assert any(
        diagnostic.code == "PYT601_MARKER_AS_TAG"
        and diagnostic.severity is DiagnosticSeverity.WARNING
        for diagnostic in bundle.diagnostics
    )
    compile_artifacts(bundle)
    collection = materialize_and_discover(tmp_path, bundle)
    tagged = next(
        item.spec for item in collection.items if item.spec.function_name == "test_tagged"
    )
    assert tagged.tags == frozenset({"smoke"})
    assert Counter(result.status for result in execute_tests(collection.items)) == {
        Status.PASS: 2,
        Status.SKIP: 2,
    }


def test_bare_pytest_asyncio_uses_a_fresh_closed_loop_and_preserves_signature(
    tmp_path: Path,
) -> None:
    source = source_file(
        "tests/test_async.py",
        """
        import asyncio
        import pytest

        LOOPS = []
        CANCELLED = []

        @pytest.mark.asyncio
        async def test_first_loop(tmp_path) -> None:
            assert tmp_path.is_dir()
            LOOPS.append(asyncio.get_running_loop())

            async def linger() -> None:
                try:
                    await asyncio.sleep(60)
                finally:
                    CANCELLED.append(True)

            asyncio.create_task(linger())
            await asyncio.sleep(0)

        @pytest.mark.asyncio
        async def test_second_loop(tmp_path) -> None:
            current = asyncio.get_running_loop()
            assert tmp_path.is_dir()
            assert current is not LOOPS[0]
            assert LOOPS[0].is_closed()
            assert CANCELLED == [True]
        """,
        migration_relative="test_async.py",
    )

    bundle = convert_pytest_suite((source,))

    assert not bundle.blocking_diagnostics
    content = bundle.artifacts[0].content
    assert "pytest.mark.asyncio" not in content
    assert "import pytest" not in content
    assert (
        "from testenix.migration_runtime import isolated_pytest_asyncio as "
        "_testenix_isolated_asyncio"
    ) in content
    assert content.count("@_testenix_isolated_asyncio") == 2
    collection = materialize_and_discover(tmp_path, bundle)
    assert not collection.issues
    generated_tree = ast.parse(content)
    generated_lines = {
        statement.name: min(
            statement.lineno,
            *(decorator.lineno for decorator in statement.decorator_list),
        )
        for statement in generated_tree.body
        if isinstance(statement, ast.AsyncFunctionDef)
    }
    assert {
        item.spec.function_name: item.spec.source_line for item in collection.items
    } == generated_lines
    assert Counter(result.status for result in execute_tests(collection.items)) == {Status.PASS: 2}


def test_asyncio_isolation_composes_with_both_case_orders_and_class_wrappers(
    tmp_path: Path,
) -> None:
    source = source_file(
        "tests/test_async_cases.py",
        """
        import asyncio
        import pytest

        SEEN_LOOPS = []

        def remember_fresh_loop():
            current = asyncio.get_running_loop()
            assert all(previous is not current and previous.is_closed() for previous in SEEN_LOOPS)
            SEEN_LOOPS.append(current)

        @pytest.mark.parametrize("value", [1, 2], ids=["one", "two"])
        @pytest.mark.asyncio
        async def test_parametrize_outside(value, tmp_path):
            remember_fresh_loop()
            assert tmp_path.is_dir()
            assert value in {1, 2}

        @pytest.mark.asyncio
        @pytest.mark.parametrize("value", [3, 4], ids=["three", "four"])
        async def test_asyncio_outside(value):
            remember_fresh_loop()
            assert value in {3, 4}

        class TestAsyncCases:
            @pytest.mark.asyncio
            @pytest.mark.parametrize("value", [5, 6], ids=["five", "six"])
            async def test_cases(self, value, tmp_path):
                remember_fresh_loop()
                assert tmp_path.is_dir()
                assert value in {5, 6}
        """,
        migration_relative="test_async_cases.py",
    )

    bundle = convert_pytest_suite((source,))

    assert not bundle.blocking_diagnostics
    assert len(bundle.mappings) == 6
    assert {mapping.case_id for mapping in bundle.mappings} == {
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
    }
    content = bundle.artifacts[0].content
    assert "pytest.mark" not in content
    assert "import pytest" not in content
    assert content.count("@_testenix_isolated_asyncio") == 3
    collection = materialize_and_discover(tmp_path, bundle)
    assert not collection.issues
    assert Counter(result.status for result in execute_tests(collection.items)) == {Status.PASS: 6}


def test_asyncio_isolation_propagates_test_failures(tmp_path: Path) -> None:
    source = source_file(
        "tests/test_async_failure.py",
        """
        import pytest

        @pytest.mark.asyncio
        async def test_failure():
            raise AssertionError("isolated failure")
        """,
        migration_relative="test_async_failure.py",
    )

    bundle = convert_pytest_suite((source,))

    assert not bundle.blocking_diagnostics
    collection = materialize_and_discover(tmp_path, bundle)
    assert not collection.issues
    result = execute_tests(collection.items)[0]
    assert result.status is Status.FAIL
    assert result.attempts[0].phases[1].exception_type == "builtins.AssertionError"
    assert result.attempts[0].phases[1].message == "isolated failure"


def test_supported_builtin_fixtures_run_without_a_pytest_runtime_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment_name = "TESTENIX_MIGRATED_MONKEYPATCH"
    monkeypatch.delenv(environment_name, raising=False)
    source = source_file(
        "tests/test_builtins.py",
        f"""
        import os

        def test_native_builtins(tmp_path, monkeypatch) -> None:
            destination = tmp_path / "proof.txt"
            destination.write_text("native", encoding="utf-8")
            monkeypatch.setenv({environment_name!r}, destination.read_text(encoding="utf-8"))
            assert os.environ[{environment_name!r}] == "native"
        """,
        migration_relative="test_builtins.py",
    )

    bundle = convert_pytest_suite((source,))

    assert not bundle.blocking_diagnostics
    assert "def test_native_builtins(tmp_path, monkeypatch" in bundle.artifacts[0].content
    assert "pytest" not in bundle.artifacts[0].content
    collection = materialize_and_discover(tmp_path, bundle)
    assert not collection.issues
    assert execute_tests(collection.items)[0].status is Status.PASS
    assert environment_name not in os.environ


def test_migrated_monkeypatch_allows_only_implemented_direct_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment_name = "TESTENIX_MIGRATED_MONKEYPATCH_DIRECT"
    monkeypatch.delenv(environment_name, raising=False)
    source = source_file(
        "tests/test_monkeypatch_direct.py",
        f"""
        import os

        class Target:
            value = "original"

        def test_direct_calls(monkeypatch) -> None:
            monkeypatch.setattr(Target, "value", "changed")
            monkeypatch.setenv({environment_name!r}, Target.value)
            assert Target.value == "changed"
            assert os.environ[{environment_name!r}] == "changed"
            monkeypatch.undo()
            assert Target.value == "original"
            assert {environment_name!r} not in os.environ
        """,
        migration_relative="test_monkeypatch_direct.py",
    )

    bundle = convert_pytest_suite((source,))

    assert not bundle.blocking_diagnostics
    collection = materialize_and_discover(tmp_path, bundle)
    assert not collection.issues
    assert execute_tests(collection.items)[0].status is Status.PASS
    assert environment_name not in os.environ


def test_monkeypatch_can_flow_through_static_helpers_positionally_and_by_keyword(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment_name = "TESTENIX_MIGRATED_MONKEYPATCH_HELPER"
    monkeypatch.delenv(environment_name, raising=False)
    source = source_file(
        "tests/test_monkeypatch_helpers.py",
        f"""
        import os

        class Target:
            value = "original"

        def _set_target(patch, target, value):
            patch.setattr(target, "value", value)

        def _set_environment(*, patch, name, value):
            patch.setenv(name, value)

        def _configure(patch, name):
            _set_environment(patch=patch, name=name, value=Target.value)

        def test_static_helpers(monkeypatch, tmp_path):
            assert tmp_path.is_dir()
            _set_target(monkeypatch, Target, "through-helper")
            _configure(patch=monkeypatch, name={environment_name!r})
            assert os.environ[{environment_name!r}] == "through-helper"
        """,
        migration_relative="test_monkeypatch_helpers.py",
    )

    bundle = convert_pytest_suite((source,))

    assert not bundle.blocking_diagnostics
    collection = materialize_and_discover(tmp_path, bundle)
    assert not collection.issues
    assert execute_tests(collection.items)[0].status is Status.PASS
    assert environment_name not in os.environ


def test_monkeypatch_helper_analysis_has_a_cycle_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment_name = "TESTENIX_MIGRATED_MONKEYPATCH_CYCLE"
    monkeypatch.delenv(environment_name, raising=False)
    source = source_file(
        "tests/test_monkeypatch_cycle.py",
        f"""
        import os

        def _left(patch, depth):
            if depth:
                _right(patch, depth - 1)
            else:
                patch.setenv({environment_name!r}, "left")

        def _right(patch, depth):
            if depth:
                _left(patch, depth - 1)
            else:
                patch.setenv({environment_name!r}, "right")

        def test_recursive_helpers(monkeypatch):
            _left(monkeypatch, 2)
            assert os.environ[{environment_name!r}] == "left"
        """,
        migration_relative="test_monkeypatch_cycle.py",
    )

    bundle = convert_pytest_suite((source,))

    assert not bundle.blocking_diagnostics
    collection = materialize_and_discover(tmp_path, bundle)
    assert not collection.issues
    assert execute_tests(collection.items)[0].status is Status.PASS
    assert environment_name not in os.environ


def test_user_defined_monkeypatch_fixture_is_not_restricted_by_builtin_contract(
    tmp_path: Path,
) -> None:
    source = source_file(
        "tests/test_custom_monkeypatch.py",
        """
        import pytest

        class CustomPatch:
            def delenv(self):
                return "project-owned"

        @pytest.fixture
        def monkeypatch():
            return CustomPatch()

        def test_custom_fixture(monkeypatch) -> None:
            alias = monkeypatch
            assert alias.delenv() == "project-owned"
        """,
        migration_relative="test_custom_monkeypatch.py",
    )

    bundle = convert_pytest_suite((source,))

    assert not bundle.blocking_diagnostics
    collection = materialize_and_discover(tmp_path, bundle)
    assert not collection.issues
    assert execute_tests(collection.items)[0].status is Status.PASS


def test_autouse_fixture_keeps_implicit_setup_and_builtin_dependencies(
    tmp_path: Path,
) -> None:
    source = source_file(
        "tests/test_autouse.py",
        """
        import os
        import pytest

        @pytest.fixture(autouse=True)
        def isolated_environment(monkeypatch, tmp_path):
            marker = tmp_path / "autouse.txt"
            marker.write_text("ready", encoding="utf-8")
            monkeypatch.setenv("TESTENIX_AUTOUSE_PATH", str(marker))
            yield

        def test_implicit_fixture_ran() -> None:
            marker = os.environ["TESTENIX_AUTOUSE_PATH"]
            assert marker.endswith("autouse.txt")

        def test_explicit_request_reuses_autouse(isolated_environment) -> None:
            assert isolated_environment is None
        """,
        migration_relative="test_autouse.py",
    )

    bundle = convert_pytest_suite((source,))

    assert not bundle.blocking_diagnostics
    content = bundle.artifacts[0].content
    assert "@_testenix_fixture(autouse=True)" in content
    assert "import pytest" not in content
    collection = materialize_and_discover(tmp_path, bundle)
    assert not collection.issues
    assert Counter(result.status for result in execute_tests(collection.items)) == {Status.PASS: 2}
    assert "TESTENIX_AUTOUSE_PATH" not in os.environ


def test_simple_pytest_class_becomes_fresh_instance_wrappers_with_stable_mappings(
    tmp_path: Path,
) -> None:
    source = source_file(
        "tests/test_class.py",
        """
        import asyncio
        import pytest

        class TestExample:
            def _double(self, value):
                return value * 2

            def test_first_instance(self, tmp_path):
                assert not hasattr(self, "seen")
                self.seen = tmp_path

            def test_second_instance(self):
                assert not hasattr(self, "seen")

            @pytest.mark.parametrize("value", [2, 3], ids=["two", "three"])
            def test_cases(self, value):
                assert self._double(value) == value + value

            @pytest.mark.asyncio
            async def test_async_method(self, monkeypatch):
                monkeypatch.setenv("TESTENIX_CLASS_ASYNC", "yes")
                await asyncio.sleep(0)
        """,
        migration_relative="test_class.py",
    )

    first = convert_pytest_suite((source,))
    second = convert_pytest_suite((source,))

    assert first == second
    assert not first.blocking_diagnostics
    assert {mapping.source_id for mapping in first.mappings} == {
        "tests/test_class.py::TestExample.test_first_instance",
        "tests/test_class.py::TestExample.test_second_instance",
        "tests/test_class.py::TestExample.test_cases[two]",
        "tests/test_class.py::TestExample.test_cases[three]",
        "tests/test_class.py::TestExample.test_async_method",
    }
    assert all(
        mapping.target_function.startswith("test_TestExample__") for mapping in first.mappings
    )
    content = first.artifacts[0].content
    assert "pytest.mark" not in content
    assert "import pytest" not in content
    collection = materialize_and_discover(tmp_path, first)
    assert not collection.issues
    assert Counter(result.status for result in execute_tests(collection.items)) == {Status.PASS: 5}
    assert "TESTENIX_CLASS_ASYNC" not in os.environ


def test_class_wrapper_drops_parameter_and_return_annotations(tmp_path: Path) -> None:
    source = source_file(
        "tests/test_annotated_class.py",
        """
        from pathlib import Path

        class TestExample:
            def test_path(self, tmp_path: Path) -> None:
                assert tmp_path.is_dir()
        """,
        migration_relative="test_annotated_class.py",
    )

    bundle = convert_pytest_suite((source,))

    assert not bundle.blocking_diagnostics
    tree = ast.parse(bundle.artifacts[0].content)
    wrapper = next(
        statement
        for statement in tree.body
        if isinstance(statement, ast.FunctionDef)
        and statement.name.startswith("test_TestExample__")
    )
    assert wrapper.returns is None
    assert all(
        argument.annotation is None
        for argument in (*wrapper.args.posonlyargs, *wrapper.args.args, *wrapper.args.kwonlyargs)
    )
    collection = materialize_and_discover(tmp_path, bundle)
    assert not collection.issues
    assert execute_tests(collection.items)[0].status is Status.PASS


@pytest.mark.parametrize(
    "source",
    [
        """
        def test_other_method(monkeypatch):
            monkeypatch.delenv("NAME")
        """,
        """
        def test_alias(monkeypatch):
            patcher = monkeypatch
            patcher.setenv("NAME", "value")
        """,
        """
        def consume(value):
            return value
        def test_passed(monkeypatch):
            consume(monkeypatch)
        """,
        """
        def test_object_read(monkeypatch):
            assert monkeypatch
        """,
        """
        def test_method_read(monkeypatch):
            callback = monkeypatch.setenv
            callback("NAME", "value")
        """,
        """
        import pytest
        @pytest.fixture
        def configured(monkeypatch):
            monkeypatch.chdir(".")
        def test_fixture(configured):
            pass
        """,
        """
        class TestExample:
            def test_method(self, monkeypatch):
                monkeypatch.setitem({}, "key", "value")
        """,
        """
        from project_helpers import configure
        def test_imported_helper(monkeypatch):
            configure(monkeypatch)
        """,
        """
        def configure(patch):
            patch.setenv("NAME", "value")
        original = configure
        configure = original
        def test_rebound_helper(monkeypatch):
            configure(monkeypatch)
        """,
        """
        def configure(patch):
            patch.setenv("NAME", "value")
        def test_locally_rebound_helper(monkeypatch):
            configure = lambda value: None
            configure(monkeypatch)
        """,
        """
        def configure(patch):
            patch = None
        def test_rebound_parameter(monkeypatch):
            configure(monkeypatch)
        """,
        """
        def factory():
            return lambda value: None
        def test_dynamic_helper(monkeypatch):
            factory()(monkeypatch)
        """,
    ],
    ids=[
        "unsupported-method",
        "alias",
        "passed-object",
        "object-read",
        "method-read",
        "fixture-dependency",
        "class-method",
        "imported-helper",
        "module-rebound-helper",
        "locally-rebound-helper",
        "helper-parameter-rebound",
        "dynamic-helper",
    ],
)
def test_builtin_monkeypatch_usage_is_fail_closed(source: str) -> None:
    selected = source_file(
        "tests/test_monkeypatch_fail_closed.py",
        source,
        migration_relative="test_monkeypatch_fail_closed.py",
    )

    bundle = convert_pytest_suite((selected,))

    diagnostic = next(
        diagnostic
        for diagnostic in bundle.blocking_diagnostics
        if diagnostic.code == "PYT214_MONKEYPATCH_USAGE"
    )
    assert diagnostic.line is not None
    assert not bundle.artifacts
    assert not bundle.mappings


@pytest.mark.parametrize(
    ("source", "expected_code"),
    [
        (
            """
            class TestExample:
                def test_value(self, value=1):
                    assert value == 1
            """,
            "PYT316_CLASS_SIGNATURE",
        ),
        (
            """
            class TestExample:
                def test_value(self, *, value=1):
                    assert value == 1
            """,
            "PYT316_CLASS_SIGNATURE",
        ),
        (
            """
            class TestExample:
                def helper(self):
                    pass
                setup_method = helper
                def test_value(self):
                    pass
            """,
            "PYT314_CLASS_LIFECYCLE",
        ),
        (
            """
            class TestExample:
                __init__ = object.__init__
                def test_value(self):
                    pass
            """,
            "PYT314_CLASS_LIFECYCLE",
        ),
        (
            """
            class TestExample:
                __new__ = object.__new__
                def test_value(self):
                    pass
            """,
            "PYT314_CLASS_LIFECYCLE",
        ),
        (
            """
            class TestExample:
                setup_method = teardown_method = lambda self: None
                def test_value(self):
                    pass
            """,
            "PYT314_CLASS_LIFECYCLE",
        ),
        (
            """
            class TestExample:
                pytestmark = marker_alias = object()
                def test_value(self):
                    pass
            """,
            "PYT313_CLASS_MARK",
        ),
    ],
    ids=[
        "positional-default",
        "keyword-default",
        "aliased-setup",
        "aliased-init",
        "aliased-new",
        "multi-target-lifecycle",
        "multi-target-pytestmark",
    ],
)
def test_simple_class_conversion_rejects_implicit_semantic_bindings(
    source: str,
    expected_code: str,
) -> None:
    selected = source_file(
        "tests/test_class_fail_closed.py",
        source,
        migration_relative="test_class_fail_closed.py",
    )

    bundle = convert_pytest_suite((selected,))

    assert expected_code in {diagnostic.code for diagnostic in bundle.blocking_diagnostics}
    assert not bundle.artifacts
    assert not bundle.mappings


@pytest.mark.parametrize(
    ("name", "source", "expected_code"),
    [
        (
            "class-inheritance",
            """
            class Base:
                pass
            class TestExample(Base):
                def test_value(self):
                    pass
            """,
            "PYT311_CLASS_INHERITANCE",
        ),
        (
            "class-lifecycle",
            """
            class TestExample:
                def setup_method(self):
                    self.value = 1
                def test_value(self):
                    assert self.value == 1
            """,
            "PYT314_CLASS_LIFECYCLE",
        ),
        (
            "class-staticmethod",
            """
            class TestExample:
                @staticmethod
                def test_value():
                    pass
            """,
            "PYT316_CLASS_SIGNATURE",
        ),
        (
            "xfail",
            """
            import pytest
            @pytest.mark.xfail(reason="known")
            def test_value():
                assert False
            """,
            "PYT301_XFAIL_SEMANTICS",
        ),
        (
            "builtin-fixture",
            """
            def test_value(capsys):
                assert capsys
            """,
            "PYT209_BUILTIN_FIXTURE",
        ),
        (
            "unknown-fixture",
            """
            def test_value(database):
                assert database
            """,
            "PYT210_UNKNOWN_FIXTURE",
        ),
        (
            "autouse",
            """
            import pytest
            ENABLED = True
            @pytest.fixture(autouse=ENABLED)
            def state():
                return object()
            def test_value():
                pass
            """,
            "PYT203_FIXTURE_AUTOUSE",
        ),
        (
            "session-fixture",
            """
            import pytest
            @pytest.fixture(scope="session")
            def state():
                return object()
            def test_value(state):
                assert state is not None
            """,
            "PYT204_FIXTURE_SCOPE",
        ),
        (
            "fixture-params",
            """
            import pytest
            @pytest.fixture(params=[1, 2])
            def value(request):
                return request.param
            def test_value(value):
                assert value
            """,
            "PYT202_FIXTURE_PARAMS",
        ),
        (
            "fixture-request",
            """
            import pytest
            @pytest.fixture
            def value(request):
                return request.param
            def test_value(value):
                assert value
            """,
            "PYT209_BUILTIN_FIXTURE",
        ),
        (
            "event-loop-policy-request",
            """
            def test_value(event_loop_policy):
                assert event_loop_policy
            """,
            "PYT209_BUILTIN_FIXTURE",
        ),
        (
            "runtime-skip",
            """
            import pytest
            def test_value():
                pytest.skip("later")
            """,
            "PYT401_RUNTIME_SKIP",
        ),
        (
            "plugin-registration",
            """
            pytest_plugins = ["custom_plugin"]
            def test_value():
                pass
            """,
            "PYT501_PLUGIN_REGISTRATION",
        ),
        (
            "plugin-hook",
            """
            def pytest_addoption(parser):
                pass
            def test_value():
                pass
            """,
            "PYT502_PLUGIN_HOOK",
        ),
        (
            "stacked-parametrize",
            """
            import pytest
            @pytest.mark.parametrize("x", [1, 2])
            @pytest.mark.parametrize("y", [3, 4])
            def test_value(x, y):
                assert x + y
            """,
            "PYT104_STACKED_PARAMETRIZE",
        ),
        (
            "dynamic-parametrize",
            """
            import pytest
            CASES = [1, 2]
            @pytest.mark.parametrize("value", CASES)
            def test_value(value):
                assert value
            """,
            "PYT101_DYNAMIC_PARAMETRIZE",
        ),
        (
            "indirect-parametrize",
            """
            import pytest
            @pytest.mark.parametrize("value", [1], indirect=True)
            def test_value(value):
                assert value
            """,
            "PYT102_INDIRECT_PARAMETRIZE",
        ),
        (
            "per-case-marker",
            """
            import pytest
            @pytest.mark.parametrize(
                "value",
                [pytest.param(1, marks=pytest.mark.skip(reason="not now"))],
            )
            def test_value(value):
                assert value
            """,
            "PYT108_PARAMETER_MARKS",
        ),
        (
            "usefixtures",
            """
            import pytest
            @pytest.mark.usefixtures("database")
            def test_value():
                pass
            """,
            "PYT302_USEFIXTURES",
        ),
        (
            "async-plugin",
            """
            import pytest
            @pytest.mark.asyncio()
            async def test_value():
                pass
            """,
            "PYT502_ASYNC_PLUGIN",
        ),
        (
            "unmarked-async-test",
            """
            async def test_value():
                pass
            """,
            "PYT508_UNMARKED_ASYNC_TEST",
        ),
        (
            "unmarked-async-class-test",
            """
            class TestAsync:
                async def test_value(self):
                    pass
            """,
            "PYT508_UNMARKED_ASYNC_TEST",
        ),
        (
            "asyncio-runtime-alias-collision",
            """
            import pytest
            _testenix_isolated_asyncio = object()
            @pytest.mark.asyncio
            async def test_value():
                pass
            """,
            "PYT008_GENERATED_IMPORT_COLLISION",
        ),
        (
            "asyncio-on-sync-test",
            """
            import pytest
            @pytest.mark.asyncio
            def test_value():
                pass
            """,
            "PYT502_ASYNC_PLUGIN",
        ),
        (
            "anyio-plugin",
            """
            import pytest
            @pytest.mark.anyio
            async def test_value():
                pass
            """,
            "PYT502_ASYNC_PLUGIN",
        ),
    ],
    ids=lambda value: value if isinstance(value, str) and "\n" not in value else None,
)
def test_unsupported_pytest_semantics_block_artifact(
    name: str,
    source: str,
    expected_code: str,
) -> None:
    selected = source_file(
        f"tests/test_{name.replace('-', '_')}.py",
        source,
        migration_relative=f"test_{name.replace('-', '_')}.py",
    )

    bundle = convert_pytest_suite((selected,))

    assert expected_code in {diagnostic.code for diagnostic in bundle.blocking_diagnostics}
    relevant = next(
        diagnostic for diagnostic in bundle.blocking_diagnostics if diagnostic.code == expected_code
    )
    assert relevant.line is not None
    assert not bundle.artifacts
    assert not bundle.mappings


def test_renamed_target_collision_blocks_both_sources() -> None:
    conventional = source_file(
        "tests/test_api.py",
        "def test_one():\n    pass\n",
        migration_relative="test_api.py",
    )
    suffix = source_file(
        "tests/api_test.py",
        "def test_two():\n    pass\n",
        migration_relative="api_test.py",
    )

    bundle = convert_pytest_suite((conventional, suffix))

    collisions = [
        diagnostic
        for diagnostic in bundle.blocking_diagnostics
        if diagnostic.code == "PYT007_TARGET_COLLISION"
    ]
    assert {diagnostic.source for diagnostic in collisions} == {
        "tests/test_api.py",
        "tests/api_test.py",
    }
    assert not bundle.artifacts
    assert not bundle.mappings


def test_ancestor_conftest_autouse_remains_blocked_until_package_imports_are_supported() -> None:
    ancestor = source_file(
        "tests/conftest.py",
        """
        import pytest

        @pytest.fixture(autouse=True)
        def inherited_state():
            return object()
        """,
        migration_relative="tests/conftest.py",
    )
    nested_test = source_file(
        "tests/unit/test_nested.py",
        """
        def test_nested() -> None:
            pass
        """,
        migration_relative="unit/test_nested.py",
    )

    bundle = convert_pytest_suite((nested_test,), (ancestor,))

    diagnostic = next(
        diagnostic
        for diagnostic in bundle.blocking_diagnostics
        if diagnostic.code == "PYT213_ANCESTOR_CONFTEST"
    )
    assert "inherited_state" in diagnostic.message
    assert all(artifact.relative_path.name.startswith("_") for artifact in bundle.artifacts)
    assert not bundle.mappings
