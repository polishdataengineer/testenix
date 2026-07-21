from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from testenix.cli import main
from testenix.config import ConfigError, TestenixConfig, load_config
from testenix.contracts import (
    AttemptResult,
    CollectionIssue,
    Phase,
    PhaseResult,
    RunResult,
    Status,
)
from testenix.contracts import (
    TestResult as TestenixTestResult,
)
from testenix.contracts import (
    TestSpec as TestenixTestSpec,
)
from testenix.history import HistoryStore
from testenix.reporters.console import ConsoleReporter
from testenix.reporters.json import JsonReporter
from testenix.reporters.junit import JUnitReporter


def _test_result(
    test_id: str,
    *,
    path: str,
    line: int,
    status: Status,
    duration: float,
    message: str | None = None,
) -> TestenixTestResult:
    phase = PhaseResult(
        phase=Phase.CALL,
        status=status,
        duration=duration,
        message=message,
        exception_type="AssertionError" if message else None,
        traceback=f"trace: {message}" if message else None,
        stdout="captured output" if message else "",
        stderr="captured error" if message else "",
    )
    attempt = AttemptResult(
        test_id=test_id,
        attempt=1,
        worker_id="worker-1",
        status=status,
        duration=duration,
        phases=(phase,),
        started_at=10.0,
        finished_at=10.0 + duration,
    )
    spec = TestenixTestSpec(
        id=test_id,
        path=path,
        module_name=Path(path).stem,
        function_name=test_id.rsplit("::", 1)[-1],
        display_name=test_id.rsplit("::", 1)[-1],
        parameters={"letters": {"b", "a"}},
        tags=frozenset({"unit", "fast"}),
        source_line=line,
    )
    return TestenixTestResult(test=spec, status=status, attempts=(attempt,), duration=duration)


def _run_result(*tests: TestenixTestResult, run_id: str = "run-1") -> RunResult:
    return RunResult(
        run_id=run_id,
        tests=tuple(tests),
        collection_issues=(),
        started_at=10.0,
        finished_at=12.0,
    )


def test_console_includes_collection_traceback() -> None:
    run = RunResult(
        run_id="collection-run",
        tests=(),
        collection_issues=(
            CollectionIssue(
                path="tests/test_broken.py",
                message="import failed",
                traceback="Traceback line\nRuntimeError: broken import",
            ),
        ),
        started_at=1.0,
        finished_at=2.0,
    )

    rendered = ConsoleReporter().render(run)

    assert "COLLECT  tests/test_broken.py" in rendered
    assert "RuntimeError: broken import" in rendered


def test_load_config_and_validate_cli_options(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[tool.testenix]
workers = 4
retries = 2
timeout = 3.5
paths = ["tests/unit", "tests/component"]
tags = ["unit", "fast", "unit"]
json = "reports/results.json"
junit = "reports/junit.xml"
history = false
shard_modules = true
manifest = ".testenix/collection.json"
""",
        encoding="utf-8",
    )

    config = load_config(pyproject)

    assert config == TestenixConfig(
        paths=("tests/unit", "tests/component"),
        workers=4,
        retries=2,
        timeout=3.5,
        tags=("fast", "unit"),
        json_path=Path("reports/results.json"),
        junit_path=Path("reports/junit.xml"),
        history_path=None,
        shard_modules=True,
        manifest_path=Path(".testenix/collection.json"),
    )
    with pytest.raises(ConfigError, match="workers"):
        TestenixConfig(workers=0)

    defaults = TestenixConfig()
    assert defaults.workers == "auto"
    assert defaults.resolved_workers >= 1
    assert defaults.paths == ("tests",)


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf")])
def test_config_rejects_non_finite_timeout(timeout: float) -> None:
    with pytest.raises(ConfigError, match="finite number greater than zero"):
        TestenixConfig(timeout=timeout)


def test_console_and_json_are_stable_and_source_ordered(tmp_path: Path) -> None:
    later = _test_result(
        "suite::later",
        path="tests/test_suite.py",
        line=20,
        status=Status.FAIL,
        duration=0.25,
        message="1 < 0 & false",
    )
    earlier = _test_result(
        "suite::earlier",
        path="tests/test_suite.py",
        line=10,
        status=Status.PASS,
        duration=0.1,
    )
    run = _run_result(later, earlier)

    console = ConsoleReporter().render(run)
    assert console.index("suite::earlier") < console.index("suite::later")
    assert "1 passed" in console
    assert "1 failed" in console
    assert "trace: 1 < 0 & false" in console
    assert "[attempt 1 call stdout]" in console
    assert "captured output" in console
    assert "[attempt 1 call stderr]" in console
    assert "captured error" in console
    assert console == ConsoleReporter().render(run)

    output = tmp_path / "nested" / "run.json"
    reporter = JsonReporter(output)
    rendered = reporter.render(run)
    reporter.write(run)
    document = json.loads(output.read_text(encoding="utf-8"))

    assert output.read_text(encoding="utf-8") == rendered
    assert document["format"] == "testenix.run-result"
    assert document["exit_code"] == 1
    assert [item["test"]["id"] for item in document["tests"]] == [
        "suite::earlier",
        "suite::later",
    ]
    assert document["tests"][0]["test"]["parameters"]["letters"] == ["a", "b"]


def test_junit_maps_failure_and_escapes_text(tmp_path: Path) -> None:
    failed = _test_result(
        "suite::broken",
        path="tests/test_suite.py",
        line=12,
        status=Status.FAIL,
        duration=0.25,
        message="left < right & unexpected",
    )
    passed = _test_result(
        "suite::ok",
        path="tests/test_suite.py",
        line=4,
        status=Status.PASS,
        duration=0.05,
    )
    path = tmp_path / "junit.xml"
    JUnitReporter(path).write(_run_result(failed, passed))

    root = ET.parse(path).getroot()
    assert root.attrib["tests"] == "2"
    assert root.attrib["failures"] == "1"
    cases = root.findall("./testsuite/testcase")
    broken = next(case for case in cases if case.attrib["name"] == "broken")
    failure = broken.find("failure")
    assert failure is not None
    assert failure.attrib["message"] == "left < right & unexpected"
    assert failure.text == "trace: left < right & unexpected"


def test_history_is_idempotent_and_tracks_duration_and_status(tmp_path: Path) -> None:
    test_id = "suite::case"
    first = _test_result(
        test_id,
        path="tests/test_suite.py",
        line=1,
        status=Status.PASS,
        duration=1.0,
    )
    second = _test_result(
        test_id,
        path="tests/test_suite.py",
        line=1,
        status=Status.FAIL,
        duration=3.0,
        message="boom",
    )

    with HistoryStore(tmp_path / "history.sqlite3") as history:
        history.record_run(_run_result(first, run_id="first"))
        history.record_run(_run_result(first, run_id="first"))
        history.record_run(_run_result(second, run_id="second"))

        entry = history.get(test_id)
        assert entry is not None
        assert entry.duration == pytest.approx(2.0)
        assert entry.status is Status.FAIL
        assert entry.samples == 2
        assert history.durations() == {test_id: pytest.approx(2.0)}
        assert history.recent_statuses(test_id) == (Status.FAIL, Status.PASS)


def test_cli_applies_overrides_and_writes_all_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _run_result(
        _test_result(
            "suite::ok",
            path="tests/test_suite.py",
            line=1,
            status=Status.PASS,
            duration=0.1,
        )
    )
    captured: dict[str, object] = {}

    def fake_runner(paths: tuple[str, ...], config: TestenixConfig) -> RunResult:
        captured["paths"] = paths
        captured["config"] = config
        # Persistence belongs to the application runner; the CLI only passes
        # the configured path and must not record the same run a second time.
        if config.history_path is not None:
            with HistoryStore(config.history_path) as history:
                history.record_run(run)
        return run

    monkeypatch.setattr("testenix.cli._call_runner", fake_runner)
    json_path = tmp_path / "run.json"
    junit_path = tmp_path / "junit.xml"
    history_path = tmp_path / "history.sqlite3"

    exit_code = main(
        [
            "run",
            "--workers",
            "3",
            "--retries",
            "1",
            "--timeout",
            "2",
            "--tag",
            "unit",
            "--json",
            str(json_path),
            "--junit",
            str(junit_path),
            "--history",
            str(history_path),
            "--shard-modules",
            "tests/unit",
        ]
    )

    assert exit_code == 0
    assert captured["paths"] == ("tests/unit",)
    config = captured["config"]
    assert isinstance(config, TestenixConfig)
    assert (config.workers, config.retries, config.timeout, config.tags) == (3, 1, 2.0, ("unit",))
    assert config.shard_modules is True
    assert json_path.exists()
    assert junit_path.exists()
    with HistoryStore(history_path) as history:
        assert history.get("suite::ok") is not None
    assert "PASS" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("reporter_arguments", "expected"),
    [
        ((), (0, "auto", False, None)),
        (("-q",), (-1, "auto", False, None)),
        (("--quiet",), (-1, "auto", False, None)),
        (("-v",), (1, "auto", False, None)),
        (("-vv",), (2, "auto", False, None)),
        (("-vvv",), (2, "auto", False, None)),
        (("--verbose", "--verbose"), (2, "auto", False, None)),
        (("--color", "always"), (0, "always", False, None)),
        (("--color", "never"), (0, "never", False, None)),
        (("--no-color",), (0, "never", False, None)),
        (("--show-skips", "--durations", "0"), (0, "auto", True, 0)),
        (("--durations", "7"), (0, "auto", False, 7)),
    ],
)
def test_run_cli_maps_console_reporter_options(
    reporter_arguments: tuple[str, ...],
    expected: tuple[int, str, bool, int | None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run_result(
        _test_result(
            "suite::first",
            path="tests/test_suite.py",
            line=1,
            status=Status.PASS,
            duration=0.1,
        ),
        _test_result(
            "suite::second",
            path="tests/test_suite.py",
            line=2,
            status=Status.PASS,
            duration=0.2,
        ),
    )
    captured: dict[str, object] = {}

    class RecordingConsoleReporter:
        def __init__(
            self,
            *,
            verbosity: int,
            color: str,
            show_skips: bool,
            durations: int | None,
            workers: int,
        ) -> None:
            captured["options"] = (verbosity, color, show_skips, durations)
            captured["workers"] = workers

        def write(self, result: RunResult) -> None:
            captured["result"] = result

    monkeypatch.setattr("testenix.cli._call_runner", lambda paths, config: run)
    monkeypatch.setattr("testenix.cli.ConsoleReporter", RecordingConsoleReporter)

    exit_code = main(["run", "--workers", "8", *reporter_arguments, "tests"])

    assert exit_code == 0
    assert captured == {
        "options": expected,
        "workers": 1,
        "result": run,
    }


def test_run_cli_reports_zero_workers_for_an_empty_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run_result()
    captured: dict[str, object] = {}

    class RecordingConsoleReporter:
        def __init__(self, **options: object) -> None:
            captured.update(options)

        def write(self, result: RunResult) -> None:
            captured["result"] = result

    monkeypatch.setattr("testenix.cli._call_runner", lambda paths, config: run)
    monkeypatch.setattr("testenix.cli.ConsoleReporter", RecordingConsoleReporter)

    assert main(["run", "--workers", "8", "tests"]) == 0
    assert captured["workers"] == 0
    assert captured["result"] is run


@pytest.mark.parametrize(
    "arguments",
    [
        ("-q", "-v"),
        ("--quiet", "--verbose"),
        ("--color", "always", "--no-color"),
        ("--durations", "-1"),
        ("--durations", "not-an-integer"),
    ],
)
def test_run_cli_rejects_conflicting_or_invalid_console_options(
    arguments: tuple[str, ...],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["run", *arguments])

    assert exit_info.value.code == 2


def test_pytest_bridge_forwards_native_console_flag_names_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, ...]] = []

    def fake_pytest(arguments: tuple[str, ...]) -> int:
        captured.append(arguments)
        return 5

    monkeypatch.setattr("testenix.cli._call_pytest", fake_pytest)
    forwarded = (
        "-q",
        "-vv",
        "--color",
        "always",
        "--no-color",
        "--show-skips",
        "--durations",
        "0",
    )

    assert main(["pytest", *forwarded]) == 5
    assert captured == [forwarded]


def test_run_help_describes_configured_default_paths(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["run", "--help"])

    assert exit_info.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "default: [tool.testenix].paths, otherwise tests" in help_text
    assert "show failures and the final summary only" in help_text
