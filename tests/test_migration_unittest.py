from __future__ import annotations

import hashlib
from pathlib import Path
from textwrap import dedent

import pytest

from testenix.config import TestenixConfig
from testenix.contracts import RunResult, Status, TestResult
from testenix.discovery import CollectionResult, discover
from testenix.executor import execute_test
from testenix.migration_models import ConversionBundle, SourceFile
from testenix.migration_runtime import (
    UnittestSourceChangedError,
    load_unittest_case,
)
from testenix.migration_unittest import (
    convert_unittest_suite,
    detect_unittest_module,
)
from testenix.runner import run


def _source_file(
    tmp_path: Path,
    source: str,
    *,
    filename: str = "test_legacy.py",
    migration_filename: str | None = None,
) -> tuple[SourceFile, bytes]:
    text = dedent(source).lstrip()
    path = tmp_path / "legacy" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    original = path.read_bytes()
    relative = path.relative_to(tmp_path)
    return (
        SourceFile(
            path=path,
            project_relative=relative,
            migration_relative=Path(migration_filename or filename),
            sha256=hashlib.sha256(original).hexdigest(),
            text=text,
        ),
        original,
    )


def _write_generated(tmp_path: Path, bundle: ConversionBundle) -> Path:
    generated = tmp_path / "generated"
    for artifact in bundle.artifacts:
        if artifact.relative_path.suffix == ".py":
            compile(artifact.content, artifact.relative_path.as_posix(), "exec")
        target = generated / artifact.relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(artifact.content, encoding="utf-8")
    return generated


def _discover_and_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bundle: ConversionBundle,
) -> tuple[CollectionResult, RunResult]:
    generated = _write_generated(tmp_path, bundle)
    monkeypatch.chdir(tmp_path)
    collection = discover(generated.relative_to(tmp_path))
    assert not collection.issues
    result = run(
        (str(generated.relative_to(tmp_path)),),
        TestenixConfig(workers=1, retries=0, history_path=None),
    )
    assert not result.collection_issues
    return collection, result


def _result_by_source(
    bundle: ConversionBundle,
    result: RunResult,
) -> dict[str, TestResult]:
    by_function = {test.test.function_name: test for test in result.tests}
    return {mapping.source_id: by_function[mapping.target_function] for mapping in bundle.mappings}


def test_converted_testcase_preserves_lifecycle_assertions_and_mock_patch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = tmp_path / "events.txt"
    source, original = _source_file(
        tmp_path,
        f"""
        import unittest
        from pathlib import Path
        from unittest import mock

        VALUE = 1
        EVENTS = Path({str(events)!r})

        def record(value):
            with EVENTS.open("a", encoding="utf-8") as stream:
                stream.write(value + "\\n")

        class TestAssertions(unittest.TestCase):
            def setUp(self):
                record("setup")
                self.payload = {{"answer": 42}}
                self.addCleanup(record, "cleanup")

            def tearDown(self):
                record("teardown")

            @mock.patch(__name__ + ".VALUE", 9)
            def test_everything(self):
                record("call")
                self.assertEqual(VALUE, 9)
                self.assertEqual(self.payload["answer"], 42)
                self.assertTrue(self.payload)
                self.assertIn("answer", self.payload)
                self.assertIsNone(None)
                with self.assertRaises(ValueError):
                    int("not-an-integer")
        """,
    )

    bundle = convert_unittest_suite((source,))
    collection, result = _discover_and_run(tmp_path, monkeypatch, bundle)

    assert detect_unittest_module(source)
    assert not bundle.blocking_diagnostics
    assert len(bundle.artifacts) == 2
    assert len(bundle.mappings) == len(collection.items) == 1
    assert result.tests[0].status is Status.PASS
    assert events.read_text(encoding="utf-8").splitlines() == [
        "setup",
        "call",
        "teardown",
        "cleanup",
    ]
    assert source.path.read_bytes() == original


def test_static_skip_expected_failure_and_unexpected_success_map_losslessly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, original = _source_file(
        tmp_path,
        """
        import unittest

        class TestOutcomes(unittest.TestCase):
            @unittest.skip("not available")
            def test_skipped(self):
                self.fail("static skip must prevent execution")

            @unittest.expectedFailure
            def test_expected_failure(self):
                self.assertEqual(1, 2)

            @unittest.expectedFailure
            def test_unexpected_success(self):
                self.assertEqual(2, 2)
        """,
    )

    bundle = convert_unittest_suite((source,))
    _, result = _discover_and_run(tmp_path, monkeypatch, bundle)
    by_source = _result_by_source(bundle, result)

    prefix = "legacy/test_legacy.py::TestOutcomes."
    assert by_source[f"{prefix}test_skipped"].status is Status.SKIP
    assert by_source[f"{prefix}test_expected_failure"].status is Status.XFAIL
    assert by_source[f"{prefix}test_unexpected_success"].status is Status.XPASS
    assert result.exit_code == 1
    assert source.path.read_bytes() == original


def test_isolated_asyncio_testcase_uses_its_complete_async_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = tmp_path / "async-events.txt"
    source, original = _source_file(
        tmp_path,
        f"""
        import asyncio
        import unittest
        from pathlib import Path

        EVENTS = Path({str(events)!r})

        def record(value):
            with EVENTS.open("a", encoding="utf-8") as stream:
                stream.write(value + "\\n")

        class TestAsync(unittest.IsolatedAsyncioTestCase):
            async def asyncSetUp(self):
                record("async-setup")
                self.value = 41
                self.addAsyncCleanup(self.cleanup)

            async def cleanup(self):
                await asyncio.sleep(0)
                record("async-cleanup")

            async def test_async_value(self):
                await asyncio.sleep(0)
                record("call")
                self.assertEqual(self.value + 1, 42)

            async def asyncTearDown(self):
                record("async-teardown")
        """,
    )

    bundle = convert_unittest_suite((source,))
    _, result = _discover_and_run(tmp_path, monkeypatch, bundle)

    assert not bundle.blocking_diagnostics
    assert [test.status for test in result.tests] == [Status.PASS]
    assert events.read_text(encoding="utf-8").splitlines() == [
        "async-setup",
        "call",
        "async-teardown",
        "async-cleanup",
    ]
    assert source.path.read_bytes() == original


def test_unittest_filename_is_renamed_for_native_directory_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, original = _source_file(
        tmp_path,
        """
        import unittest

        class TestLegacyName(unittest.TestCase):
            def testRuns(self):
                self.assertEqual(6 * 7, 42)
        """,
        filename="testlegacy.py",
        migration_filename="testlegacy.py",
    )

    first = convert_unittest_suite((source,))
    second = convert_unittest_suite((source,))
    collection, result = _discover_and_run(tmp_path, monkeypatch, first)

    assert first == second
    generated_test = next(
        artifact for artifact in first.artifacts if artifact.relative_path.suffix == ".py"
    )
    assert generated_test.relative_path == Path("test_testlegacy.py")
    assert first.mappings[0].target_file == "test_testlegacy.py"
    assert len(collection.items) == 1
    assert result.tests[0].status is Status.PASS
    assert source.path.read_bytes() == original


def test_generated_wrapper_resolves_original_from_an_unrelated_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, original = _source_file(
        tmp_path,
        """
        import unittest

        class TestPortableWrapper(unittest.TestCase):
            def test_value(self):
                self.assertEqual(6 * 7, 42)
        """,
    )
    bundle = convert_unittest_suite((source,))
    generated = _write_generated(tmp_path, bundle)
    unrelated = tmp_path / "unrelated-cwd"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)

    collection = discover(generated)
    result = run(
        (str(generated),),
        TestenixConfig(workers=1, retries=0, history_path=None),
    )

    assert not collection.issues
    assert len(collection.items) == 1
    assert not result.collection_issues
    assert [test.status for test in result.tests] == [Status.PASS]
    assert source.path.read_bytes() == original


def test_generated_wrapper_rejects_drift_in_an_imported_python_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper, _ = _source_file(
        tmp_path,
        "VALUE = 42\n",
        filename="helper.py",
    )
    source, _ = _source_file(
        tmp_path,
        """
        import unittest
        from helper import VALUE

        class TestPinnedHelper(unittest.TestCase):
            def test_value(self):
                self.assertEqual(VALUE, 42)
        """,
    )
    bundle = convert_unittest_suite(
        (source,),
        manifest_files=(source, helper),
    )
    generated = _write_generated(tmp_path, bundle)
    monkeypatch.chdir(tmp_path)

    first = run(
        (str(generated),),
        TestenixConfig(workers=1, retries=0, history_path=None),
    )
    helper.path.write_text("VALUE = 99\n", encoding="utf-8")
    second = run(
        (str(generated),),
        TestenixConfig(workers=1, retries=0, history_path=None),
    )

    assert [test.status for test in first.tests] == [Status.PASS]
    assert not first.collection_issues
    assert second.exit_code == 2
    assert second.collection_issues
    assert "source changed since migration" in second.collection_issues[0].message


def test_runtime_rechecks_source_hash_after_a_cached_module_was_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, original = _source_file(
        tmp_path,
        """
        import unittest

        class TestPinnedSource(unittest.TestCase):
            def test_value(self):
                self.assertEqual(40 + 2, 42)
        """,
    )
    bundle = convert_unittest_suite((source,))
    generated = _write_generated(tmp_path, bundle)
    monkeypatch.chdir(tmp_path)
    collection = discover(generated.relative_to(tmp_path))
    assert not collection.issues

    first = load_unittest_case(
        source.project_relative,
        "TestPinnedSource",
        source.sha256,
    )
    second = load_unittest_case(
        source.project_relative,
        "TestPinnedSource",
        source.sha256,
    )
    assert first is second

    source.path.write_bytes(original + b"\n# concurrent edit\n")
    try:
        with pytest.raises(UnittestSourceChangedError, match="source changed"):
            load_unittest_case(
                source.project_relative,
                "TestPinnedSource",
                source.sha256,
            )
        result = execute_test(collection.items[0])
        assert result.status is Status.FAIL
        assert any(
            phase.exception_type == ("testenix.migration_runtime.UnittestSourceChangedError")
            for phase in result.attempts[-1].phases
        )
    finally:
        source.path.write_bytes(original)
    assert source.path.read_bytes() == original


@pytest.mark.parametrize(
    ("source_text", "expected_code"),
    [
        pytest.param(
            """
            import unittest

            class TestSubtest(unittest.TestCase):
                def test_values(self):
                    with self.subTest(value=1):
                        self.assertEqual(1, 1)
            """,
            "UNIT001",
            id="subTest",
        ),
        pytest.param(
            """
            import unittest

            class TestDynamicSkip(unittest.TestCase):
                def test_value(self):
                    self.skipTest("runtime condition")
            """,
            "UNIT002",
            id="skipTest",
        ),
        pytest.param(
            """
            import unittest

            class TestClassLifecycle(unittest.TestCase):
                @classmethod
                def setUpClass(cls):
                    cls.value = 42

                @classmethod
                def tearDownClass(cls):
                    del cls.value

                def test_value(self):
                    self.assertEqual(self.value, 42)
            """,
            "UNIT003",
            id="class-lifecycle",
        ),
        pytest.param(
            """
            import unittest

            def setUpModule():
                pass

            def tearDownModule():
                pass

            class TestModuleLifecycle(unittest.TestCase):
                def test_value(self):
                    self.assertTrue(True)
            """,
            "UNIT004",
            id="module-lifecycle",
        ),
        pytest.param(
            """
            import unittest

            class TestCustomRun(unittest.TestCase):
                def run(self, result=None):
                    return super().run(result)

                def test_value(self):
                    self.assertTrue(True)
            """,
            "UNIT005",
            id="custom-run",
        ),
        pytest.param(
            """
            import unittest

            class TestCustomClassCleanup(unittest.TestCase):
                @classmethod
                def doClassCleanups(cls):
                    return True

                def test_value(self):
                    self.assertTrue(True)
            """,
            "UNIT005",
            id="custom-class-cleanup",
        ),
        pytest.param(
            """
            import unittest

            class Mixin:
                pass

            class TestMixed(Mixin, unittest.TestCase):
                def test_value(self):
                    self.assertTrue(True)
            """,
            "UNIT006",
            id="mixin",
        ),
        pytest.param(
            """
            import unittest

            class Base(unittest.TestCase):
                pass

            class TestInherited(Base):
                def test_value(self):
                    self.assertTrue(True)
            """,
            "UNIT006",
            id="indirect-inheritance",
        ),
        pytest.param(
            """
            import unittest

            class TestLoaded(unittest.TestCase):
                def test_value(self):
                    self.assertTrue(True)

            def load_tests(loader, tests, pattern):
                return tests
            """,
            "UNIT007",
            id="load-tests",
        ),
        pytest.param(
            """
            import unittest

            def generated():
                pass

            class TestDynamic(unittest.TestCase):
                pass

            setattr(TestDynamic, "test_generated", generated)
            """,
            "UNIT008",
            id="dynamic-generation",
        ),
        pytest.param(
            """
            import unittest

            def check_value():
                assert 6 * 7 == 42

            FUNCTION_CASE = unittest.FunctionTestCase(check_value)
            """,
            "UNIT009",
            id="function-test-case",
        ),
    ],
)
def test_unsafe_unittest_constructs_are_blocked_without_touching_the_source(
    tmp_path: Path,
    source_text: str,
    expected_code: str,
) -> None:
    source, original = _source_file(tmp_path, source_text)

    bundle = convert_unittest_suite((source,))

    assert detect_unittest_module(source)
    assert expected_code in {diagnostic.code for diagnostic in bundle.blocking_diagnostics}
    assert all(diagnostic.line is not None for diagnostic in bundle.blocking_diagnostics)
    assert not bundle.artifacts
    assert not bundle.mappings
    assert source.path.read_bytes() == original
