from __future__ import annotations

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from testenix.cli import EXIT_INTERNAL_ERROR, EXIT_INTERRUPTED, EXIT_USAGE, main
from testenix.pytest_adapter import (
    PytestInvocationError,
    PytestUnavailableError,
    run_pytest,
)


def test_pytest_adapter_overlays_exact_interpreter_command_on_posix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class ProcessReplaced(Exception):
        pass

    def fake_execv(executable: str, command: tuple[str, ...]) -> None:
        captured["executable"] = executable
        captured["command"] = command
        raise ProcessReplaced

    monkeypatch.setattr("testenix.pytest_adapter.importlib.util.find_spec", lambda name: object())
    monkeypatch.setattr("testenix.pytest_adapter._is_windows", lambda: False)
    monkeypatch.setattr("testenix.pytest_adapter.os.execv", fake_execv)

    with pytest.raises(ProcessReplaced):
        run_pytest(("-q", "tests/test_api.py", "-k", "smoke"))
    assert captured == {
        "executable": sys.executable,
        "command": (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_api.py",
            "-k",
            "smoke",
        ),
    }


def test_pytest_adapter_uses_console_entry_point_in_process_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    class FakePytest:
        @staticmethod
        def console_main() -> int:
            captured.extend(sys.argv[1:])
            return 5

    monkeypatch.setattr("testenix.pytest_adapter.importlib.util.find_spec", lambda name: object())
    monkeypatch.setattr("testenix.pytest_adapter._is_windows", lambda: True)
    monkeypatch.setattr(
        "testenix.pytest_adapter.importlib.import_module", lambda name: FakePytest()
    )

    original_argv = sys.argv
    assert run_pytest(("--", "-q", "tests")) == 5
    assert captured == ["--", "-q", "tests"]
    assert sys.argv is original_argv


def test_pytest_adapter_in_process_path_executes_real_pytest() -> None:
    source_root = Path(__file__).parents[1] / "src"
    python_path = os.pathsep.join(filter(None, (str(source_root), os.environ.get("PYTHONPATH"))))
    completed = subprocess.run(
        (
            sys.executable,
            "-c",
            "from testenix.pytest_adapter import _run_pytest_in_process; "
            "raise SystemExit(_run_pytest_in_process(('--version',)))",
        ),
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": python_path},
    )

    assert completed.returncode == 0
    assert completed.stdout.startswith("pytest ")


def test_pytest_cli_forwards_options_without_argparse_interpreting_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, ...]] = []

    def fake_pytest(arguments: tuple[str, ...]) -> int:
        captured.append(arguments)
        return 5

    monkeypatch.setattr("testenix.cli._call_pytest", fake_pytest)

    assert main(["pytest", "-q", "tests", "-k", "smoke", "--maxfail=1"]) == 5
    assert main(["pytest", "--help"]) == 5
    assert main(["pytest", "--", "-q", "tests"]) == 5
    assert captured == [
        ("-q", "tests", "-k", "smoke", "--maxfail=1"),
        ("--help",),
        ("--", "-q", "tests"),
    ]


def test_pytest_cli_handles_interrupt_and_rejects_global_testenix_config(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def interrupted(arguments: tuple[str, ...]) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr("testenix.cli._call_pytest", interrupted)
    assert main(["pytest", "tests"]) == EXIT_INTERRUPTED
    assert "interrupted" in capsys.readouterr().err

    assert main(["--config", "pyproject.toml", "pytest"]) == EXIT_USAGE
    assert "immediately after" in capsys.readouterr().err


def test_pytest_adapter_reports_missing_pytest(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("testenix.pytest_adapter.importlib.util.find_spec", lambda name: None)

    with pytest.raises(PytestUnavailableError, match=r"testenix\[pytest\]"):
        run_pytest()

    def missing_pytest(arguments: tuple[str, ...]) -> int:
        raise PytestUnavailableError("pytest is missing")

    monkeypatch.setattr("testenix.cli._call_pytest", missing_pytest)
    assert main(["pytest", "tests"]) == EXIT_USAGE
    assert "pytest is missing" in capsys.readouterr().err


def test_pytest_adapter_reports_process_start_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("testenix.pytest_adapter.importlib.util.find_spec", lambda name: object())
    monkeypatch.setattr("testenix.pytest_adapter._is_windows", lambda: False)

    def cannot_start(executable: str, command: tuple[str, ...]) -> None:
        raise OSError("process unavailable")

    monkeypatch.setattr("testenix.pytest_adapter.os.execv", cannot_start)
    with pytest.raises(PytestInvocationError, match="process unavailable"):
        run_pytest()

    def broken_pytest(arguments: tuple[str, ...]) -> int:
        raise PytestInvocationError("cannot start pytest")

    monkeypatch.setattr("testenix.cli._call_pytest", broken_pytest)
    assert main(["pytest"]) == EXIT_INTERNAL_ERROR
    assert "cannot start pytest" in capsys.readouterr().err


def test_pytest_adapter_executes_existing_pytest_features(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    suite.mkdir()
    teardown_marker = tmp_path / "fixture-closed"
    process_marker = tmp_path / "pytest-process"
    pytest_config = tmp_path / "pytest.ini"
    pytest_config.write_text(
        "[pytest]\nmarkers =\n    smoke: compatibility acceptance tests\n",
        encoding="utf-8",
    )
    (suite / "conftest.py").write_text(
        f"""
import os
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def record_pytest_process():
    Path({str(process_marker)!r}).write_text(str(os.getpid()), encoding="utf-8")


@pytest.fixture(scope="module")
def factor():
    yield 3
    Path({str(teardown_marker)!r}).write_text("closed", encoding="utf-8")
""",
        encoding="utf-8",
    )
    (suite / "test_existing_pytest_suite.py").write_text(
        """
import os

import pytest


@pytest.mark.smoke
@pytest.mark.parametrize(
    "value, expected",
    [(2, 6), (4, 12)],
    ids=["small", "large"],
)
def test_multiplies_with_conftest_fixture(factor, value, expected):
    assert factor * value == expected


@pytest.mark.smoke
class TestBuiltInFixtures:
    def test_monkeypatch(self, monkeypatch):
        monkeypatch.setenv("TESTENIX_PYTEST_BRIDGE", "works")
        assert os.environ["TESTENIX_PYTEST_BRIDGE"] == "works"


@pytest.mark.smoke
@pytest.mark.skip(reason="compatibility skip")
def test_pytest_skip_semantics():
    raise AssertionError("pytest should not execute a skipped test")


@pytest.mark.smoke
@pytest.mark.xfail(reason="compatibility xfail", strict=True)
def test_pytest_xfail_semantics():
    assert False
""",
        encoding="utf-8",
    )
    report = tmp_path / "pytest.xml"

    command = (
        sys.executable,
        "-m",
        "testenix",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "-c",
        str(pytest_config),
        "-m",
        "smoke",
        f"--junitxml={report}",
        str(suite),
    )
    source_root = Path(__file__).parents[1] / "src"
    python_path = os.pathsep.join(filter(None, (str(source_root), os.environ.get("PYTHONPATH"))))
    process = subprocess.Popen(command, env={**os.environ, "PYTHONPATH": python_path})
    try:
        returncode = process.wait(timeout=60)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise

    assert returncode == 0
    assert process_marker.read_text(encoding="utf-8").isdigit()
    if os.name == "posix":
        # POSIX exec preserves the PID. A Windows virtualenv executable may be
        # a launcher whose PID differs from the in-process Python interpreter.
        assert int(process_marker.read_text(encoding="utf-8")) == process.pid
    assert teardown_marker.read_text(encoding="utf-8") == "closed"
    test_suite = ET.parse(report).find(".//testsuite")
    assert test_suite is not None
    assert test_suite.attrib["tests"] == "5"
    assert test_suite.attrib["failures"] == "0"
    assert test_suite.attrib["errors"] == "0"
    assert test_suite.attrib["skipped"] == "2"
