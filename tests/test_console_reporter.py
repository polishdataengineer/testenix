from __future__ import annotations

from io import StringIO

import pytest

from testenix.contracts import (
    AttemptResult,
    CollectionIssue,
    Phase,
    PhaseResult,
    RunResult,
    Status,
    TestResult,
    TestSpec,
)
from testenix.reporters.console import ConsoleReporter


class _Stream(StringIO):
    def __init__(self, *, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _result(
    test_id: str,
    *,
    path: str = "tests/test_sample.py",
    line: int = 1,
    status: Status = Status.PASS,
    duration: float = 0.001,
    message: str | None = None,
    stdout: str = "",
    stderr: str = "",
    skip_reason: str | None = None,
    xfail_reason: str | None = None,
    worker: str = "worker-1",
    attempt_number: int = 1,
) -> TestResult:
    phase = PhaseResult(
        phase=Phase.CALL,
        status=status,
        duration=duration,
        message=message,
        exception_type="AssertionError" if message else None,
        traceback="Traceback line\nAssertionError: boom" if message else None,
        stdout=stdout,
        stderr=stderr,
    )
    attempt = AttemptResult(
        test_id=test_id,
        attempt=attempt_number,
        worker_id=worker,
        status=status,
        duration=duration,
        phases=(phase,),
        started_at=10.0,
        finished_at=10.0 + duration,
    )
    spec = TestSpec(
        id=test_id,
        path=path,
        module_name="test_sample",
        function_name=test_id.rsplit("::", 1)[-1],
        display_name=test_id.rsplit("::", 1)[-1],
        skip_reason=skip_reason,
        xfail_reason=xfail_reason,
        source_line=line,
    )
    return TestResult(test=spec, status=status, attempts=(attempt,), duration=duration)


def _run(
    *tests: TestResult,
    issues: tuple[CollectionIssue, ...] = (),
    duration: float = 2.5,
) -> RunResult:
    return RunResult(
        run_id="run-1",
        tests=tuple(tests),
        collection_issues=issues,
        started_at=10.0,
        finished_at=10.0 + duration,
    )


def test_default_reporter_preserves_the_legacy_plain_format() -> None:
    passed = _result("tests/test_sample.py::test_ok", line=10)
    failed = _result(
        "tests/test_sample.py::test_bad",
        line=20,
        status=Status.FAIL,
        duration=0.25,
        message="boom",
        stdout="captured stdout",
        stderr="captured stderr",
    )

    assert ConsoleReporter().render(_run(failed, passed)) == (
        "Testenix run run-1\n"
        "PASS      tests/test_sample.py::test_ok [0.001s]\n"
        "FAIL      tests/test_sample.py::test_bad [0.250s]\n"
        "          attempt 1, call: boom\n"
        "          Traceback line\n"
        "          AssertionError: boom\n"
        "          [attempt 1 call stdout]\n"
        "          captured stdout\n"
        "          [attempt 1 call stderr]\n"
        "          captured stderr\n"
        "2 tests, 1 passed, 1 failed in 2.500s\n"
    )


def test_compact_groups_by_path_and_keeps_complete_problem_details() -> None:
    passed = _result("tests/unit/test_api.py::test_ok", path="tests/unit/test_api.py", line=1)
    failed = _result(
        "tests/unit/test_api.py::test_bad",
        path="tests/unit/test_api.py",
        line=2,
        status=Status.FAIL,
        duration=0.025,
        message="boom",
        stdout="out",
        stderr="err",
    )
    skipped = _result(
        "tests/unit/test_auth.py::test_optional",
        path="tests/unit/test_auth.py",
        status=Status.SKIP,
        duration=0.000004,
        message="needs service",
    )

    rendered = ConsoleReporter(verbosity=0).render(_run(skipped, failed, passed))

    lines = rendered.splitlines()
    assert lines[:2] == ["Testenix  |  3 tests  |  2 files", ""]
    assert rendered.count("PASS  ") == 0
    api_line = next(line for line in lines if line.startswith("FAIL  tests/unit/test_api.py"))
    auth_line = next(line for line in lines if line.startswith("SKIP  tests/unit/test_auth.py"))
    assert api_line.endswith("1 passed, 1 failed [26ms]")
    assert "1 skipped" in auth_line and auth_line.endswith("[4us]")
    assert api_line.index("1 passed") == auth_line.index("1 skipped")
    assert "Problems (1)" in rendered
    assert "\n\nProblems (1)\n" in rendered
    assert "tests/unit/test_api.py::test_bad" in rendered
    assert "Traceback line" in rendered
    assert "[attempt 1 call stdout]" in rendered
    assert "out" in rendered
    assert "[attempt 1 call stderr]" in rendered
    assert "err" in rendered
    assert "Skipped tests" not in rendered


def test_quiet_hides_header_and_successes_but_not_problems_or_summary() -> None:
    passed = _result("tests/test_sample.py::test_ok")
    failed = _result(
        "tests/test_sample.py::test_bad",
        line=2,
        status=Status.FAIL,
        message="boom",
    )

    rendered = ConsoleReporter(verbosity=-1).render(_run(passed, failed))

    assert "Testenix run" not in rendered
    assert "test_ok" not in rendered
    assert "Problems (1)" in rendered
    assert "test_bad" in rendered
    assert "AssertionError: boom" in rendered
    assert rendered.endswith("2 tests, 1 passed, 1 failed in 2.500s\n")


def test_debug_includes_worker_attempt_phase_capture_and_adaptive_duration() -> None:
    failed = _result(
        "tests/test_sample.py::test_bad",
        status=Status.FAIL,
        duration=0.000125,
        message="boom",
        stdout="debug out",
        stderr="debug err",
        worker="worker-7",
        attempt_number=2,
    )

    rendered = ConsoleReporter(verbosity=2, workers=8).render(_run(failed))

    assert "Testenix run run-1 [workers=8]" in rendered
    assert "[125us]" in rendered
    assert "attempt 2: fail, worker=worker-7, duration=125us" in rendered
    assert "call: fail, duration=125us: boom" in rendered
    assert "Traceback line" in rendered
    assert "[call stdout]" in rendered and "debug out" in rendered
    assert "[call stderr]" in rendered and "debug err" in rendered


def test_show_skips_reports_native_reasons_in_source_order() -> None:
    xfailed = _result(
        "tests/test_sample.py::test_later",
        line=20,
        status=Status.XFAIL,
        xfail_reason="known bug",
    )
    skipped = _result(
        "tests/test_sample.py::test_earlier",
        line=10,
        status=Status.SKIP,
        skip_reason="linux only",
    )

    rendered = ConsoleReporter(verbosity=-1, show_skips=True).render(_run(xfailed, skipped))

    assert "Skipped tests (2)" in rendered
    assert rendered.index("test_earlier") < rendered.index("test_later")
    assert "test_earlier - linux only" in rendered
    assert "test_later - known bug" in rendered


def test_durations_selects_slowest_or_all_with_adaptive_units() -> None:
    slow = _result("suite::slow", line=1, duration=2.5)
    medium = _result("suite::medium", line=2, duration=0.025)
    fast = _result("suite::fast", line=3, duration=0.000004)

    slowest = ConsoleReporter(verbosity=-1, durations=2).render(_run(fast, slow, medium))
    all_durations = ConsoleReporter(verbosity=-1, durations=0).render(_run(fast, slow, medium))

    assert "Slowest durations (2)" in slowest
    assert slowest.index("2.5s") < slowest.index("25ms")
    assert "suite::fast" not in slowest
    assert "Durations (all)" in all_durations
    assert "4us" in all_durations
    assert all_durations.index("suite::slow") < all_durations.index("suite::medium")
    assert all_durations.index("suite::medium") < all_durations.index("suite::fast")


@pytest.mark.parametrize(
    ("environment", "tty", "expected_color"),
    [
        ({}, True, True),
        ({}, False, False),
        ({"NO_COLOR": "1", "FORCE_COLOR": "1"}, True, False),
        ({"FORCE_COLOR": "1", "CI": "1", "TERM": "dumb"}, False, True),
        ({"CI": "1"}, True, False),
        ({"TERM": "dumb"}, True, False),
    ],
)
def test_auto_color_precedence_and_fake_tty(
    monkeypatch: pytest.MonkeyPatch,
    environment: dict[str, str],
    tty: bool,
    expected_color: bool,
) -> None:
    for name in ("NO_COLOR", "FORCE_COLOR", "CI", "TERM"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    stream = _Stream(tty=tty)

    ConsoleReporter(verbosity=0, color="auto").write(_run(_result("suite::ok")), stream=stream)
    rendered = stream.getvalue()

    assert ("\x1b[" in rendered) is expected_color
    assert "\x1b[" not in rendered.splitlines()[-1]


def test_explicit_color_overrides_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    always = _Stream(tty=False)
    ConsoleReporter(color="always").write(_run(_result("suite::ok")), always)

    monkeypatch.setenv("FORCE_COLOR", "1")
    never = _Stream(tty=True)
    ConsoleReporter(color="never").write(_run(_result("suite::ok")), never)

    assert "\x1b[" in always.getvalue()
    assert "\x1b[" not in never.getvalue()
    assert "\x1b[" not in always.getvalue().splitlines()[-1]


def test_compact_truncates_a_long_path_to_requested_width() -> None:
    long_path = "tests/" + "deeply_nested/" * 8 + "test_terminal_output.py"
    rendered = ConsoleReporter(verbosity=0, width=60).render(
        _run(_result(f"{long_path}::test_ok", path=long_path))
    )
    group_line = rendered.splitlines()[2]

    assert "..." in group_line
    assert long_path not in group_line
    assert len(group_line) <= 60
    assert group_line.endswith("1 passed [1ms]")

    max_width_line = (
        ConsoleReporter(verbosity=0, width=1_000)
        .render(_run(_result(f"{long_path}::test_ok", path=long_path)))
        .splitlines()[2]
    )
    assert len(max_width_line) == 120


def test_render_is_deterministic_and_sorts_files_then_source_lines() -> None:
    later_file = _result("z.py::test_z", path="z.py", line=1)
    later_line = _result("a.py::test_later", path="a.py", line=20)
    earlier_line = _result("a.py::test_earlier", path="a.py", line=10, status=Status.FAIL)
    run = _run(later_file, later_line, earlier_line)
    reporter = ConsoleReporter(verbosity=0)

    first = reporter.render(run)
    second = reporter.render(run)

    assert first == second
    assert first.index("a.py") < first.index("z.py")
    assert "Problems (1)" in first
    assert "a.py::test_earlier" in first


def test_compact_output_is_bounded_for_100k_passing_tests_in_one_file() -> None:
    passed = _result("tests/test_bulk.py::test_case", path="tests/test_bulk.py")
    run = _run(*(passed,) * 100_000)

    rendered = ConsoleReporter(verbosity=0).render(run)

    assert rendered.count("tests/test_bulk.py") == 1
    assert rendered.count("\n") == 5
    assert len(rendered) < 250
    assert "100000 tests, 100000 passed" in rendered


def test_collection_issues_are_visible_even_in_quiet_mode() -> None:
    issue = CollectionIssue(
        path="tests/test_broken.py",
        message="import failed",
        traceback="Traceback\nRuntimeError: broken",
    )

    rendered = ConsoleReporter(verbosity=-1).render(_run(issues=(issue,)))

    assert rendered.startswith("COLLECT  tests/test_broken.py\n")
    assert "RuntimeError: broken" in rendered
    assert rendered.endswith("0 tests, 1 collection errors in 2.500s\n")


def test_every_status_can_be_rendered_with_color() -> None:
    results = tuple(
        _result(
            f"tests/test_status.py::test_{status.value}",
            line=index,
            status=status,
            message="detail" if status not in {Status.PASS, Status.CACHED_PASS} else None,
        )
        for index, status in enumerate(Status)
    )

    rendered = ConsoleReporter(verbosity=2, color="always").render(_run(*results))

    assert rendered.count("\x1b[") >= len(Status)
    assert "1 passed" in rendered
    assert "1 infra errors" in rendered
    assert "1 cached" in rendered
    assert "\x1b[" not in rendered.splitlines()[-1]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"verbosity": 3},
        {"verbosity": True},
        {"color": "sometimes"},
        {"show_skips": 1},
        {"durations": -1},
        {"durations": True},
        {"workers": -1},
        {"width": 0},
    ],
)
def test_rejects_invalid_options(kwargs: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        ConsoleReporter(**kwargs)  # type: ignore[arg-type]


def test_debug_accepts_zero_workers_for_an_empty_run() -> None:
    rendered = ConsoleReporter(verbosity=2, workers=0).render(_run())

    assert "Testenix run run-1 [workers=0]" in rendered
