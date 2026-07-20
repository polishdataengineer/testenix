from __future__ import annotations

import json
import os
import signal
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from textwrap import dedent

import pytest

from testenix.cli import main
from testenix.migration_service import (
    MIGRATION_FORMAT,
    MIGRATION_SCHEMA_VERSION,
    MigrationOptions,
    MigrationReport,
    MigrationStatus,
    ValidationSummary,
    _candidate_problem,
    _run_process,
    migrate,
    render_migration_summary,
)


def write_project(tmp_path: Path, files: Mapping[str, str | bytes]) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    for relative, content in files.items():
        destination = project / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            destination.write_bytes(content)
        else:
            destination.write_text(dedent(content).lstrip(), encoding="utf-8")
    return project


def source_bytes(project: Path) -> dict[str, bytes]:
    return {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in sorted(project.rglob("*"))
        if path.is_file() and "converted" not in path.parts and ".testenix" not in path.parts
    }


def options(
    project: Path,
    framework: str,
    *,
    output: str = "converted",
    dry_run: bool = False,
    check_only: bool = False,
) -> MigrationOptions:
    return MigrationOptions(
        framework=framework,  # type: ignore[arg-type] - exercised through public validation
        sources=(Path("tests"),),
        output=Path(output),
        workers=2,
        validation_timeout=30.0,
        dry_run=dry_run,
        check_only=check_only,
        project_root=project,
    )


def assert_equal_outcomes(report: MigrationReport) -> None:
    baseline = report.baseline
    native_serial = report.native_serial
    native_parallel = report.native_parallel
    assert baseline is not None
    assert native_serial is not None
    assert native_parallel is not None
    assert baseline.outcome_signature() == native_serial.outcome_signature()
    assert baseline.outcome_signature() == native_parallel.outcome_signature()


def test_pytest_migration_validates_publishes_and_preserves_original_bytes(
    tmp_path: Path,
) -> None:
    project = write_project(
        tmp_path,
        {
            "tests/test_example.py": """
                import pytest

                def test_plain() -> None:
                    assert 2 + 2 == 4

                @pytest.mark.parametrize("value", [1, 2], ids=["one", "two"])
                def test_positive(value: int) -> None:
                    assert value > 0

                @pytest.mark.skip(reason="portable static skip")
                def test_skipped() -> None:
                    raise AssertionError("must not execute")
            """,
        },
    )
    before = source_bytes(project)

    report = migrate(options(project, "pytest"))

    assert report.status is MigrationStatus.PUBLISHED
    assert report.exit_code == 0
    assert report.framework == "pytest"
    assert report.published
    assert not report.originals_modified
    assert report.converted_tests == 4
    assert (project / "converted" / "test_example.py").is_file()
    assert source_bytes(project) == before
    assert report.baseline is not None
    assert report.baseline.passed == 3
    assert report.baseline.skipped == 1
    assert any(
        diagnostic.code == "MIG006" and diagnostic.severity.value == "warning"
        for diagnostic in report.diagnostics
    )
    assert_equal_outcomes(report)


def test_unittest_migration_validates_publishes_and_preserves_original_bytes(
    tmp_path: Path,
) -> None:
    project = write_project(
        tmp_path,
        {
            "tests/test_calculator.py": """
                import unittest

                class TestCalculator(unittest.TestCase):
                    def test_addition(self) -> None:
                        self.assertEqual(2 + 3, 5)
            """,
        },
    )
    before = source_bytes(project)

    report = migrate(options(project, "unittest"))

    assert report.status is MigrationStatus.PUBLISHED
    assert report.exit_code == 0
    assert report.framework == "unittest"
    assert report.converted_tests == 1
    assert report.published
    assert (project / "converted" / "test_calculator.py").is_file()
    assert source_bytes(project) == before
    assert_equal_outcomes(report)


def test_auto_mode_combines_separate_pytest_and_unittest_modules(tmp_path: Path) -> None:
    project = write_project(
        tmp_path,
        {
            "tests/test_function.py": """
                def test_function_style() -> None:
                    assert sum([1, 2]) == 3
            """,
            "tests/test_case.py": """
                import unittest

                class TestCaseStyle(unittest.TestCase):
                    def test_method(self) -> None:
                        self.assertTrue(True)
            """,
        },
    )
    before = source_bytes(project)

    report = migrate(options(project, "auto"))

    assert report.status is MigrationStatus.PUBLISHED
    assert report.exit_code == 0
    assert report.framework == "mixed"
    assert report.converted_tests == 2
    assert report.published
    assert source_bytes(project) == before
    assert_equal_outcomes(report)


def test_auto_mode_copies_test_named_helper_without_blocking_migration(tmp_path: Path) -> None:
    project = write_project(
        tmp_path,
        {
            "tests/test_real.py": "def test_real():\n    assert True\n",
            "tests/test_helpers.py": "VALUE = 42\n",
        },
    )

    report = migrate(options(project, "auto"))

    assert report.status is MigrationStatus.PUBLISHED
    assert report.converted_tests == 1
    assert (project / "converted" / "test_helpers.py").read_text(encoding="utf-8") == (
        "VALUE = 42\n"
    )
    assert_equal_outcomes(report)


def test_unittest_outcome_mapping_distinguishes_same_module_names_in_packages(
    tmp_path: Path,
) -> None:
    passing_test = """
        import unittest

        class TestSame(unittest.TestCase):
            def test_value(self):
                self.assertTrue(True)
    """
    skipped_test = """
        import unittest

        class TestSame(unittest.TestCase):
            @unittest.skip("package-specific skip")
            def test_value(self):
                self.fail("must remain skipped")
    """
    project = write_project(
        tmp_path,
        {
            "tests/__init__.py": "",
            "tests/a/__init__.py": "",
            "tests/a/test_same.py": passing_test,
            "tests/b/__init__.py": "",
            "tests/b/test_same.py": skipped_test,
        },
    )

    report = migrate(options(project, "unittest"))

    assert report.status is MigrationStatus.PUBLISHED
    assert report.converted_tests == 2
    assert report.baseline is not None
    assert report.baseline.outcomes == {
        "tests/a/test_same.py::TestSame.test_value": "pass",
        "tests/b/test_same.py::TestSame.test_value": "skip",
    }
    assert_equal_outcomes(report)


def test_dry_run_analyzes_without_subprocess_or_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = write_project(
        tmp_path,
        {"tests/test_plain.py": "def test_plain():\n    assert True\n"},
    )
    before = source_bytes(project)

    def unexpected_process(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("dry-run must not launch validation subprocesses")

    monkeypatch.setattr("testenix.migration_service._run_process", unexpected_process)

    report = migrate(options(project, "pytest", dry_run=True))

    assert report.status is MigrationStatus.ANALYZED
    assert report.exit_code == 0
    assert report.baseline is None
    assert report.native_serial is None
    assert report.native_parallel is None
    assert not report.published
    assert not any(diagnostic.code == "MIG006" for diagnostic in report.diagnostics)
    summary = render_migration_summary(report)
    assert "analyzed candidate: 1 tests in 1 files" in summary
    assert "  converted:" not in summary
    assert not (project / "converted").exists()
    assert source_bytes(project) == before


def test_check_mode_runs_all_validations_without_publishing(tmp_path: Path) -> None:
    project = write_project(
        tmp_path,
        {"tests/test_plain.py": "def test_plain():\n    assert True\n"},
    )
    before = source_bytes(project)

    report = migrate(options(project, "pytest", check_only=True))

    assert report.status is MigrationStatus.VALIDATED
    assert report.exit_code == 0
    assert report.mode == "check"
    assert not report.published
    assert not (project / "converted").exists()
    assert source_bytes(project) == before
    assert_equal_outcomes(report)


def test_unsupported_source_exits_four_without_output(tmp_path: Path) -> None:
    project = write_project(
        tmp_path,
        {
            "tests/test_capture.py": """
                def test_capture(capsys) -> None:
                    pass
            """,
        },
    )
    before = source_bytes(project)

    report = migrate(options(project, "pytest"))

    assert report.status is MigrationStatus.UNSUPPORTED
    assert report.exit_code == 4
    assert any(diagnostic.code == "PYT209_BUILTIN_FIXTURE" for diagnostic in report.diagnostics)
    assert not report.published
    assert not (project / "converted").exists()
    assert source_bytes(project) == before


def test_unsupported_summary_groups_diagnostics_and_names_partial_work_honestly(
    tmp_path: Path,
) -> None:
    project = write_project(
        tmp_path,
        {
            "tests/test_plain.py": "def test_plain():\n    assert True\n",
            "tests/test_capture.py": """
                def test_first(capsys) -> None:
                    pass

                def test_second(capsys) -> None:
                    pass
            """,
        },
    )

    report = migrate(options(project, "pytest", dry_run=True))
    summary = render_migration_summary(report)
    document = report.to_dict()

    assert report.status is MigrationStatus.UNSUPPORTED
    assert report.converted_tests == 1
    assert not any(diagnostic.code == "MIG006" for diagnostic in report.diagnostics)
    assert "statically convertible subset: 1 tests in 1 files" in summary
    assert "  converted:" not in summary
    assert "PYT209_BUILTIN_FIXTURE: 2 occurrence(s) in 1 file(s)" in summary
    assert summary.count("PYT209_BUILTIN_FIXTURE") == 1
    assert "--report-json FILE|- retains every line-addressed entry" in summary
    assert len(document["diagnostics"]) == 2
    assert {entry["line"] for entry in document["diagnostics"]} == {1, 4}


def test_failing_source_baseline_exits_one_without_output(tmp_path: Path) -> None:
    project = write_project(
        tmp_path,
        {
            "tests/test_failure.py": """
                def test_failure() -> None:
                    assert False, "source baseline failure"
            """,
        },
    )
    before = source_bytes(project)

    report = migrate(options(project, "pytest"))

    assert report.status is MigrationStatus.VALIDATION_FAILED
    assert report.exit_code == 1
    assert report.baseline is not None
    assert report.baseline.failed == 1
    assert report.native_serial is None
    assert not report.published
    assert not (project / "converted").exists()
    assert source_bytes(project) == before


def test_native_candidate_failure_rolls_back_and_preserves_relative_asset(
    tmp_path: Path,
) -> None:
    project = write_project(
        tmp_path,
        {
            "tests/test_asset.py": """
                from pathlib import Path

                def test_adjacent_asset() -> None:
                    asset = Path(__file__).with_name("asset.txt")
                    assert asset.read_text(encoding="utf-8") == "available"
            """,
            "tests/asset.txt": b"available",
        },
    )
    before = source_bytes(project)

    report = migrate(options(project, "pytest"))

    assert report.status is MigrationStatus.VALIDATION_FAILED
    assert report.exit_code == 1
    assert report.baseline is not None and report.baseline.passed == 1
    assert report.native_serial is not None
    assert report.native_serial.gating == 1
    assert report.native_parallel is None
    assert not report.published
    assert not (project / "converted").exists()
    assert (project / "tests" / "asset.txt").read_bytes() == b"available"
    assert source_bytes(project) == before


@pytest.mark.parametrize("output", ["converted", "tests/generated"])
def test_existing_or_overlapping_output_is_a_safety_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output: str,
) -> None:
    project = write_project(
        tmp_path,
        {"tests/test_plain.py": "def test_plain():\n    pass\n"},
    )
    if output == "converted":
        (project / output).mkdir()
    before = source_bytes(project)

    def unexpected_process(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("unsafe paths must be rejected before validation")

    monkeypatch.setattr("testenix.migration_service._run_process", unexpected_process)

    report = migrate(options(project, "pytest", output=output))

    assert report.status is MigrationStatus.SAFETY_ERROR
    assert report.exit_code == 2
    assert any(diagnostic.code == "MIG001" for diagnostic in report.diagnostics)
    assert not report.published
    if output == "tests/generated":
        assert not (project / output).exists()
    assert source_bytes(project) == before


def test_cli_writes_schema_versioned_json_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = write_project(
        tmp_path,
        {"tests/test_plain.py": "def test_plain():\n    assert True\n"},
    )
    report_path = project / "migration-report.json"
    monkeypatch.chdir(project)

    exit_code = main(
        [
            "migrate",
            "pytest",
            "tests",
            "--dry-run",
            "--report-json",
            str(report_path),
        ]
    )

    captured = capsys.readouterr()
    document = json.loads(report_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert "migration analysis passed" in captured.out
    assert not captured.err
    assert document["format"] == MIGRATION_FORMAT
    assert document["schema_version"] == MIGRATION_SCHEMA_VERSION
    assert document["status"] == "analyzed"
    assert document["mode"] == "dry-run"
    assert document["exit_code"] == 0
    assert document["originals_modified"] is False
    assert document["published"] is False
    assert document["converted_tests"] == 1
    assert document["baseline"] is None
    assert not (project / "converted").exists()


def test_cli_refuses_to_replace_an_existing_report_or_test_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = write_project(
        tmp_path,
        {"tests/test_plain.py": "def test_plain():\n    assert True\n"},
    )
    source = project / "tests" / "test_plain.py"
    original = source.read_bytes()
    monkeypatch.chdir(project)

    exit_code = main(
        [
            "migrate",
            "pytest",
            "tests",
            "--dry-run",
            "--report-json",
            str(source),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 3
    assert "already exists and will not be replaced" in captured.err
    assert source.read_bytes() == original
    assert not (project / "converted").exists()


@pytest.mark.parametrize(
    "report_path",
    ["tests/new-migration-report.py", "converted/new-migration-report.py"],
)
def test_cli_rejects_new_report_inside_source_or_output_suite(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    report_path: str,
) -> None:
    project = write_project(
        tmp_path,
        {"tests/test_plain.py": "def test_plain():\n    assert True\n"},
    )
    before = source_bytes(project)
    monkeypatch.chdir(project)

    exit_code = main(
        [
            "migrate",
            "pytest",
            "tests",
            "--output",
            "converted",
            "--workers",
            "2",
            "--report-json",
            report_path,
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "unsafe migration report path" in captured.err
    assert not (project / report_path).exists()
    assert not (project / "converted").exists()
    assert source_bytes(project) == before


def test_cli_report_to_stdout_is_clean_machine_readable_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = write_project(
        tmp_path,
        {"tests/test_plain.py": "def test_plain():\n    assert True\n"},
    )
    monkeypatch.chdir(project)

    exit_code = main(
        [
            "migrate",
            "pytest",
            "tests",
            "--dry-run",
            "--report-json",
            "-",
        ]
    )

    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert exit_code == 0
    assert document["format"] == MIGRATION_FORMAT
    assert document["status"] == "analyzed"
    assert "migration analysis passed" in captured.err


def test_explicit_nonstandard_pytest_filename_is_migrated_and_discoverable(
    tmp_path: Path,
) -> None:
    project = write_project(
        tmp_path,
        {"tests/specs.py": "def test_explicit_file():\n    assert 6 * 7 == 42\n"},
    )
    migration_options = MigrationOptions(
        framework="pytest",
        sources=(Path("tests/specs.py"),),
        output=Path("converted"),
        workers=2,
        project_root=project,
    )

    report = migrate(migration_options)

    assert report.status is MigrationStatus.PUBLISHED
    assert report.converted_tests == 1
    assert (project / "converted" / "test_specs.py").is_file()
    assert_equal_outcomes(report)


def test_migration_rejects_a_fake_single_worker_parallel_gate(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least 2"):
        MigrationOptions(
            framework="pytest",
            sources=(tmp_path,),
            workers=1,
        )


def test_validation_resets_pwd_so_test_side_effects_stay_in_the_shadow(tmp_path: Path) -> None:
    project = write_project(
        tmp_path,
        {
            "tests/test_pwd.py": """
                import os
                from pathlib import Path

                def test_pwd_points_at_shadow():
                    marker = Path(os.environ["PWD"]) / "tests" / "marker.txt"
                    marker.write_text("shadow", encoding="utf-8")
                    assert marker.read_text(encoding="utf-8") == "shadow"
            """,
            "tests/marker.txt": "original",
        },
    )

    report = migrate(options(project, "pytest"))

    assert report.status is MigrationStatus.PUBLISHED
    assert (project / "tests" / "marker.txt").read_text(encoding="utf-8") == "original"


def test_source_drift_during_a_failed_baseline_becomes_a_safety_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = write_project(
        tmp_path,
        {"tests/test_plain.py": "def test_plain():\n    assert True\n"},
    )
    source = project / "tests" / "test_plain.py"

    def failing_baseline(*args: object, **kwargs: object) -> ValidationSummary:
        del args, kwargs
        source.write_text("def test_plain():\n    assert False\n", encoding="utf-8")
        return ValidationSummary(
            runner="pytest",
            tests=1,
            passed=0,
            failed=1,
            errors=0,
            skipped=0,
            xfailed=0,
            xpassed=0,
            exit_code=1,
            duration=0.01,
            outcomes={"tests/test_plain.py::test_plain": "fail"},
        )

    monkeypatch.setattr("testenix.migration_service._run_source_baseline", failing_baseline)

    report = migrate(options(project, "pytest"))

    assert report.status is MigrationStatus.SAFETY_ERROR
    assert report.originals_modified
    assert any(diagnostic.code == "MIG003" for diagnostic in report.diagnostics)
    assert not (project / "converted").exists()


def test_cleanup_failure_after_commit_keeps_truthful_published_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from testenix.migration_fs import UnsafeMigrationPathError

    project = write_project(
        tmp_path,
        {"tests/test_plain.py": "def test_plain():\n    assert True\n"},
    )

    def broken_cleanup(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise UnsafeMigrationPathError("simulated cleanup failure")

    monkeypatch.setattr("testenix.migration_fs.cleanup_publish_staging", broken_cleanup)

    report = migrate(options(project, "pytest"))

    assert report.status is MigrationStatus.PUBLISHED
    assert report.published
    assert (project / "converted" / "test_plain.py").is_file()
    assert any(
        diagnostic.code == "MIG004" and diagnostic.severity.value == "warning"
        for diagnostic in report.diagnostics
    )


def test_directory_fsync_failure_after_rename_is_truthfully_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from testenix import migration_fs

    project = write_project(
        tmp_path,
        {"tests/test_plain.py": "def test_plain():\n    assert True\n"},
    )
    real_publish = migration_fs.atomic_publish

    def publish_then_warn(*args: object, **kwargs: object) -> Path:
        output = real_publish(*args, **kwargs)  # type: ignore[arg-type]
        raise migration_fs.PublishedOutputDurabilityError(
            f"migration output was published at {output}, but fsync failed"
        )

    monkeypatch.setattr("testenix.migration_fs.atomic_publish", publish_then_warn)

    report = migrate(options(project, "pytest"))

    assert report.status is MigrationStatus.PUBLISHED
    assert report.published
    assert (project / "converted" / "test_plain.py").is_file()
    assert "durability could not be confirmed" in report.message
    assert any(
        diagnostic.code == "MIG004" and diagnostic.severity.value == "warning"
        for diagnostic in report.diagnostics
    )


def test_report_write_failure_after_publish_does_not_report_migration_rollback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = write_project(
        tmp_path,
        {"tests/test_plain.py": "def test_plain():\n    assert True\n"},
    )
    monkeypatch.chdir(project)

    def broken_report_write(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("simulated report storage failure")

    monkeypatch.setattr(
        "testenix.migration_service.write_migration_report",
        broken_report_write,
    )

    exit_code = main(
        [
            "migrate",
            "pytest",
            "tests",
            "--output",
            "converted",
            "--workers",
            "2",
            "--report-json",
            "new-report.json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert (project / "converted").is_dir()
    assert "after successful publication" in captured.err
    assert "remains published" in captured.err


def test_per_test_outcome_swap_is_not_hidden_by_matching_totals() -> None:
    baseline = ValidationSummary(
        runner="source",
        tests=2,
        passed=1,
        failed=0,
        errors=0,
        skipped=1,
        xfailed=0,
        xpassed=0,
        exit_code=0,
        duration=0.1,
        outcomes={"a": "pass", "b": "skip"},
    )
    candidate = ValidationSummary(
        runner="native",
        tests=2,
        passed=1,
        failed=0,
        errors=0,
        skipped=1,
        xfailed=0,
        xpassed=0,
        exit_code=0,
        duration=0.1,
        outcomes={"a": "skip", "b": "pass"},
    )

    problem = _candidate_problem("parallel", baseline, candidate)

    assert problem is not None
    assert "per-test outcomes differ" in problem


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group regression")
def test_timeout_kills_an_orphan_descendant_that_ignores_sigterm(tmp_path: Path) -> None:
    child = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(10)"
    parent = f"import subprocess,sys; subprocess.Popen([sys.executable, '-c', {child!r}])"

    started = time.perf_counter()
    outcome = _run_process((sys.executable, "-c", parent), cwd=tmp_path, timeout=0.2)
    elapsed = time.perf_counter() - started

    assert outcome.timed_out
    assert elapsed < 2.5


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group regression")
def test_validation_timeout_kills_real_native_worker(tmp_path: Path) -> None:
    marker = tmp_path / "worker.pid"
    test_file = tmp_path / "test_hang.py"
    test_file.write_text(
        dedent(
            f"""
            import os
            import time
            from pathlib import Path

            def test_hang():
                Path({str(marker)!r}).write_text(str(os.getpid()), encoding="utf-8")
                time.sleep(30)
            """
        ).lstrip(),
        encoding="utf-8",
    )

    outcome = _run_process(
        (
            sys.executable,
            "-m",
            "testenix",
            "run",
            str(test_file),
            "--workers",
            "2",
            "--no-history",
        ),
        cwd=tmp_path,
        timeout=3.0,
    )

    assert outcome.timed_out
    assert marker.is_file()
    worker_pid = int(marker.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2.0
    alive = True
    while alive and time.monotonic() < deadline:
        try:
            os.kill(worker_pid, 0)
        except ProcessLookupError:
            alive = False
        else:
            time.sleep(0.02)
    if alive:
        os.kill(worker_pid, signal.SIGKILL)
    assert not alive
