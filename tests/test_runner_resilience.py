from __future__ import annotations

import textwrap
import time
from pathlib import Path

import pytest

import testenix.runner as runner_module
from testenix.config import TestenixConfig
from testenix.contracts import RunResult, Status
from testenix.contracts import TestResult as TestenixTestResult
from testenix.runner import run


def _suite(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "test_resilience_suite.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


def _by_function(result: RunResult, function_name: str) -> TestenixTestResult:
    return next(test for test in result.tests if test.test.function_name == function_name)


def test_hard_timeout_isolated_from_unbounded_neighbor_under_spawn(tmp_path: Path) -> None:
    """A timed sync test must be a killable work unit next to an unbounded test."""

    neighbor_marker = tmp_path / "neighbor-ran"
    late_side_effect = tmp_path / "timed-test-survived-its-deadline"
    path = _suite(
        tmp_path,
        f"""
        import time
        from pathlib import Path

        from testenix import test

        @test(timeout=0.05)
        def test_01_ignores_soft_timeout():
            time.sleep(3.0)
            Path({str(late_side_effect)!r}).write_text("too late", encoding="utf-8")

        def test_02_unbounded_neighbor():
            Path({str(neighbor_marker)!r}).write_text("ran", encoding="utf-8")
        """,
    )

    started = time.monotonic()
    result = run((str(path),), TestenixConfig(workers=1, retries=0, history_path=None))
    elapsed = time.monotonic() - started

    timed = _by_function(result, "test_01_ignores_soft_timeout")
    neighbor = _by_function(result, "test_02_unbounded_neighbor")
    assert timed.status is Status.TIMEOUT
    assert neighbor.status is Status.PASS
    assert timed.attempts[-1].worker_id != neighbor.attempts[-1].worker_id
    assert neighbor_marker.read_text(encoding="utf-8") == "ran"
    assert not late_side_effect.exists()
    assert elapsed < 5.0


def test_completed_batch_result_survives_later_worker_crash(tmp_path: Path) -> None:
    completed_calls = tmp_path / "completed-calls"
    crash_once = tmp_path / "crash-once"
    path = _suite(
        tmp_path,
        f"""
        import os
        from pathlib import Path

        def test_01_completed_before_crash():
            marker = Path({str(completed_calls)!r})
            with marker.open("a", encoding="utf-8") as destination:
                destination.write("completed\\n")

        def test_02_crashes_worker_once():
            marker = Path({str(crash_once)!r})
            if not marker.exists():
                marker.write_text("crashed", encoding="utf-8")
                os._exit(23)
        """,
    )

    result = run((str(path),), TestenixConfig(workers=1, retries=0, history_path=None))

    completed = _by_function(result, "test_01_completed_before_crash")
    recovered = _by_function(result, "test_02_crashes_worker_once")
    assert completed.status is Status.PASS
    assert [attempt.status for attempt in completed.attempts] == [Status.PASS]
    assert completed_calls.read_text(encoding="utf-8").splitlines() == ["completed"]
    assert recovered.status is Status.FLAKY
    assert [attempt.status for attempt in recovered.attempts] == [
        Status.CRASH,
        Status.PASS,
    ]
    assert result.exit_code == 1


def test_unpicklable_case_parameter_is_rediscovered_inside_worker(tmp_path: Path) -> None:
    executed = tmp_path / "lambda-case-executed"
    path = _suite(
        tmp_path,
        f"""
        from pathlib import Path

        from testenix import case, test

        @test
        @case("lambda-case", operation=lambda value: value + 1)
        def test_lambda_parameter(operation):
            assert operation(41) == 42
            Path({str(executed)!r}).write_text("ran", encoding="utf-8")
        """,
    )

    result = run((str(path),), TestenixConfig(workers=1, retries=0, history_path=None))

    case_result = _by_function(result, "test_lambda_parameter")
    assert case_result.status is Status.PASS
    assert case_result.test.case_id == "lambda-case"
    assert "operation" in case_result.test.parameters
    assert executed.read_text(encoding="utf-8") == "ran"


def test_module_fixture_is_not_duplicated_across_parallel_shards(tmp_path: Path) -> None:
    fixture_processes = tmp_path / "module-fixture-processes"
    tests = "\n\n".join(
        f"""
        def test_{index:02d}(module_token):
            assert module_token > 0
        """.strip()
        for index in range(1, 9)
    )
    source = textwrap.dedent(
        f"""
        import os
        from pathlib import Path

        from testenix import fixture

        @fixture(scope="module")
        def module_token():
            markers = Path({str(fixture_processes)!r})
            markers.mkdir(parents=True, exist_ok=True)
            (markers / f"{{os.getpid()}}.txt").write_text("setup", encoding="utf-8")
            yield os.getpid()
        """,
    )
    path = _suite(tmp_path, f"{source}\n{tests}\n")

    result = run((str(path),), TestenixConfig(workers=4, retries=0, history_path=None))

    assert len(result.tests) == 8
    assert {test.status for test in result.tests} == {Status.PASS}
    marker_files = sorted(fixture_processes.glob("*.txt"))
    assert len(marker_files) == 1, (
        "module-scoped fixture was instantiated by multiple worker shards: "
        f"{[marker.name for marker in marker_files]}"
    )


def test_session_finalizer_crash_cannot_turn_streamed_pass_green(tmp_path: Path) -> None:
    path = _suite(
        tmp_path,
        """
        import os

        from testenix import fixture

        @fixture(scope="session")
        def fatal_resource():
            yield object()
            os._exit(42)

        def test_uses_fatal_resource(fatal_resource):
            assert fatal_resource is not None
        """,
    )

    result = run((str(path),), TestenixConfig(workers=1, retries=0, history_path=None))

    test_result = _by_function(result, "test_uses_fatal_resource")
    assert test_result.status is Status.ERROR_TEARDOWN
    assert [attempt.status for attempt in test_result.attempts] == [Status.ERROR_TEARDOWN]
    assert result.exit_code == 1


def test_timed_session_teardown_hang_overrides_streamed_pass(tmp_path: Path) -> None:
    path = _suite(
        tmp_path,
        """
        import time

        from testenix import fixture, test

        @fixture(scope="session")
        def hanging_resource():
            yield object()
            time.sleep(3)

        @test(timeout=0.05)
        def test_call_passes_before_teardown(hanging_resource):
            assert hanging_resource is not None
        """,
    )

    result = run((str(path),), TestenixConfig(workers=1, retries=0, history_path=None))

    test_result = _by_function(result, "test_call_passes_before_teardown")
    assert test_result.status is Status.TIMEOUT
    assert [attempt.status for attempt in test_result.attempts] == [Status.TIMEOUT]
    assert result.exit_code == 1


def test_timeout_terminates_processes_spawned_by_test(tmp_path: Path) -> None:
    orphan_marker = tmp_path / "orphan-side-effect"
    path = _suite(
        tmp_path,
        f"""
        import subprocess
        import sys
        import time

        from testenix import test

        @test(timeout=0.05)
        def test_spawns_child_then_blocks():
            child_code = (
                "import time; from pathlib import Path; time.sleep(1); "
                "Path({str(orphan_marker)!r}).write_text('orphan', encoding='utf-8')"
            )
            subprocess.Popen(
                [sys.executable, "-c", child_code],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(3)
        """,
    )

    result = run((str(path),), TestenixConfig(workers=1, history_path=None))
    time.sleep(1.2)

    assert result.tests[0].status is Status.TIMEOUT
    assert not orphan_marker.exists()


def test_collection_process_crash_becomes_collection_issue(tmp_path: Path) -> None:
    path = _suite(
        tmp_path,
        """
        import os

        os._exit(42)
        """,
    )

    result = run((str(path),), TestenixConfig(workers=1, history_path=None))

    assert result.tests == ()
    assert result.exit_code == 2
    assert "isolated collection failed" in result.collection_issues[0].message
    assert "code 42" in result.collection_issues[0].message


def test_collection_hang_has_supervised_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _suite(
        tmp_path,
        """
        import time

        time.sleep(3)
        """,
    )
    monkeypatch.setattr(runner_module, "_COLLECTION_TIMEOUT", 0.1)

    started = time.monotonic()
    result = run((str(path),), TestenixConfig(workers=1, history_path=None))

    assert time.monotonic() - started < 2.0
    assert result.exit_code == 2
    assert "exceeded timeout" in result.collection_issues[0].message


def test_test_timeout_starts_after_worker_rediscovery(tmp_path: Path) -> None:
    path = _suite(
        tmp_path,
        """
        import time

        from testenix import test

        time.sleep(0.4)

        @test(timeout=0.05)
        def test_fast_call_after_slow_import():
            pass
        """,
    )

    result = run((str(path),), TestenixConfig(workers=1, history_path=None))

    assert result.tests[0].status is Status.PASS
