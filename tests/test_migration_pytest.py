from __future__ import annotations

import ast
import builtins
import hashlib
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


@pytest.mark.parametrize(
    ("name", "source", "expected_code"),
    [
        (
            "class",
            """
            class TestExample:
                def test_value(self):
                    pass
            """,
            "PYT301_CLASS_TEST",
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
            def test_value(tmp_path):
                assert tmp_path.exists()
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
            @pytest.fixture(autouse=True)
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
            @pytest.mark.asyncio
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
