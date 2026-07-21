from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    run_benchmark = importlib.import_module("benchmarks.run_benchmark")
    run_migration_benchmark = importlib.import_module("benchmarks.run_migration_benchmark")
    process_control = importlib.import_module("benchmarks.process_control")
    run_project_benchmark = importlib.import_module("benchmarks.run_project_benchmark")
    run_scaling_matrix = importlib.import_module("benchmarks.run_scaling_matrix")
finally:
    sys.path.pop(0)

_module_indexes = run_benchmark._module_indexes
run_bounded_process = process_control.run_bounded_process
_run_migration_process = run_migration_benchmark._run_process
_display_command = run_project_benchmark._display_command
_environment = run_project_benchmark._environment
_explicit_suite_targets = run_project_benchmark._explicit_suite_targets
_migration_gate = run_project_benchmark._migration_gate
_observed_testenix_workers = run_project_benchmark._observed_testenix_workers
_runner_contract = run_project_benchmark._runner_contract
_runners = run_project_benchmark._runners
_validate_output = run_project_benchmark._validate_output
_tree_fingerprint = run_project_benchmark._tree_fingerprint
_testenix_runtime_identity = run_project_benchmark._testenix_runtime_identity
DEFAULT_HISTORIES = run_scaling_matrix.DEFAULT_HISTORIES
DEFAULT_LAYOUTS = run_scaling_matrix.DEFAULT_LAYOUTS
DEFAULT_SHARDING_MODES = run_scaling_matrix.DEFAULT_SHARDING_MODES
DEFAULT_WORKERS = run_scaling_matrix.DEFAULT_WORKERS
_reference_curve = run_scaling_matrix._reference_curve
_validate_coverage = run_scaling_matrix._validate_coverage
build_scenarios = run_scaling_matrix.build_scenarios


def test_generated_module_layouts_have_explicit_distribution() -> None:
    assert _module_indexes(8, 4, "balanced", 0.5) == (0, 1, 2, 3, 0, 1, 2, 3)
    assert _module_indexes(5, 3, "dominant", 0.6) == (0, 0, 0, 1, 2)
    assert _module_indexes(4, 9, "single", 0.5) == (0, 0, 0, 0)


def test_default_scaling_sweeps_cover_every_required_axis() -> None:
    counts = (100, 500, 1_000, 3_000)
    scenarios = build_scenarios(
        counts=counts,
        module_count=16,
        workers=DEFAULT_WORKERS,
        layouts=DEFAULT_LAYOUTS,
        histories=DEFAULT_HISTORIES,
        sharding_modes=DEFAULT_SHARDING_MODES,
        dominant_fraction=0.5,
        full_cross_product=False,
        include_duration_skew=False,
    )

    _validate_coverage(
        scenarios,
        counts=counts,
        workers=DEFAULT_WORKERS,
        layouts=DEFAULT_LAYOUTS,
        histories=DEFAULT_HISTORIES,
        sharding_modes=DEFAULT_SHARDING_MODES,
    )
    assert len(scenarios) == 13
    assert any(scenario.workers == "auto" for scenario in scenarios)
    assert any(scenario.module_layout == "single" for scenario in scenarios)
    assert any(scenario.history_mode == "default" for scenario in scenarios)
    assert all(
        scenario.workers == "auto" for scenario in scenarios if scenario.id.startswith("scale-")
    )
    assert {
        scenario.module_layout for scenario in scenarios if scenario.sharding_mode == "safe"
    } == set(DEFAULT_LAYOUTS)


def test_full_cross_product_exposes_a_canonical_reference_curve() -> None:
    counts = (100, 500, 1_000, 3_000)
    scenarios = build_scenarios(
        counts=counts,
        module_count=16,
        workers=DEFAULT_WORKERS,
        layouts=DEFAULT_LAYOUTS,
        histories=DEFAULT_HISTORIES,
        sharding_modes=DEFAULT_SHARDING_MODES,
        dominant_fraction=0.5,
        full_cross_product=True,
        include_duration_skew=False,
    )
    measurement = {
        "median": 1.0,
        "median_tests_per_second": 100.0,
        "observed_workers": [4],
    }
    results = [
        {
            "id": scenario.id,
            "scenario": scenario,
            "result": {"measurements": {"testenix": measurement}},
        }
        for scenario in scenarios
    ]

    curve = _reference_curve(results, reference_workers="auto")

    assert [point["test_count"] for point in curve] == list(counts)
    assert all(point["workers_requested"] == "auto" for point in curve)
    assert all(point["history_mode"] == "disabled" for point in curve)
    assert all(point["sharding_mode"] == "disabled" for point in curve)


def test_full_cross_product_deduplicates_repeated_axes() -> None:
    scenarios = build_scenarios(
        counts=(100, 100),
        module_count=4,
        workers=("auto", "auto"),
        layouts=("balanced",),
        histories=("disabled",),
        sharding_modes=("disabled",),
        dominant_fraction=0.5,
        full_cross_product=True,
        include_duration_skew=False,
    )

    assert len(scenarios) == 1
    assert scenarios[0].id == (
        "tests-100-layout-balanced-workers-auto-history-disabled-sharding-disabled"
    )


def test_bounded_process_removes_detached_descendants_after_timeout(tmp_path: Path) -> None:
    marker = tmp_path / "orphan-finished"
    child_code = (
        "import os, pathlib, sys, time\n"
        "if os.name == 'posix':\n"
        "    os.setsid()\n"
        "time.sleep(0.8)\n"
        "pathlib.Path(sys.argv[1]).write_text('orphan', encoding='utf-8')\n"
    )
    parent_code = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}, {str(marker)!r}])\n"
        "time.sleep(30)\n"
    )
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded_process(
            (sys.executable, "-c", parent_code),
            cwd=tmp_path,
            env=os.environ,
            timeout=0.3,
        )

    assert time.monotonic() - started < 6.0
    time.sleep(1.0)
    assert not marker.exists()


def test_bounded_process_tracks_detached_child_before_leader_exits(tmp_path: Path) -> None:
    ready = tmp_path / "child-ready"
    marker = tmp_path / "orphan-finished"
    child_code = (
        "import os, pathlib, sys, time\n"
        "if os.name == 'posix':\n"
        "    os.setsid()\n"
        "pathlib.Path(sys.argv[1]).write_text('ready', encoding='utf-8')\n"
        "time.sleep(0.8)\n"
        "pathlib.Path(sys.argv[2]).write_text('orphan', encoding='utf-8')\n"
    )
    parent_code = (
        "import pathlib, subprocess, sys, time\n"
        f"ready = pathlib.Path({str(ready)!r})\n"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}, str(ready), {str(marker)!r}])\n"
        "deadline = time.monotonic() + 2\n"
        "while not ready.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.005)\n"
        "time.sleep(0.05)\n"
    )

    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded_process(
            (sys.executable, "-c", parent_code),
            cwd=tmp_path,
            env=os.environ,
            timeout=0.3,
        )

    assert ready.exists()
    time.sleep(1.0)
    assert not marker.exists()


@pytest.mark.skipif(os.name != "posix", reason="exercises POSIX process-group cleanup")
def test_bounded_process_cleans_background_child_after_success(tmp_path: Path) -> None:
    ready = tmp_path / "background-ready"
    marker = tmp_path / "background-finished"
    child_code = (
        "import pathlib, sys, time\n"
        "pathlib.Path(sys.argv[1]).write_text('ready', encoding='utf-8')\n"
        "time.sleep(0.8)\n"
        "pathlib.Path(sys.argv[2]).write_text('orphan', encoding='utf-8')\n"
    )
    parent_code = (
        "import pathlib, subprocess, sys, time\n"
        f"ready = pathlib.Path({str(ready)!r})\n"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}, str(ready), "
        f"{str(marker)!r}], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "deadline = time.monotonic() + 2\n"
        "while not ready.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.005)\n"
        "time.sleep(0.05)\n"
    )

    completed = run_bounded_process(
        (sys.executable, "-c", parent_code),
        cwd=tmp_path,
        env=os.environ,
        timeout=2.0,
    )

    assert completed.returncode == 0
    assert ready.exists()
    time.sleep(1.0)
    assert not marker.exists()


def test_posix_tracker_discards_a_recycled_descendant_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tokens = {90_001: "root-v1", 90_002: "child-v1"}
    children = {90_001: (90_002,), 90_002: ()}
    monkeypatch.setattr(process_control, "_TRACKER_INTERVAL_SECONDS", 60.0)
    monkeypatch.setattr(process_control, "_process_identity", tokens.get)
    monkeypatch.setattr(
        process_control,
        "_posix_direct_children",
        lambda pid: children.get(pid, ()),
    )
    tracker = process_control._PosixTreeTracker(90_001)
    tokens[90_002] = "child-v2"
    children[90_001] = ()

    assert tracker.stop() == {}


def test_posix_cleanup_does_not_signal_an_unowned_recycled_root_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signalled: list[int] = []
    monkeypatch.setattr(process_control.os, "getpgrp", lambda: 100)
    monkeypatch.setattr(
        process_control.os, "killpg", lambda group, _signal: signalled.append(group)
    )

    process_control._posix_signal_tree(
        90_001,
        {},
        process_control.signal.SIGKILL,
        root_group_owned=False,
    )

    assert signalled == []


def test_windows_cleanup_falls_back_when_job_calls_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailedJob:
        def terminate(self) -> bool:
            return False

        def close(self) -> bool:
            return False

    class FakeProcess:
        pid = 1234

        def __init__(self) -> None:
            self.killed = False

        def kill(self) -> None:
            self.killed = True

    process = FakeProcess()
    observed: list[int] = []

    def fake_taskkill(pid: int) -> bool:
        observed.append(pid)
        return True

    monkeypatch.setattr(process_control, "_bounded_taskkill", fake_taskkill)

    assert process_control._cleanup_windows_tree(process, FailedJob()) is True
    assert observed == [1234]
    assert process.killed is True


def test_migration_benchmark_uses_bounded_process_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(command: object, **options: object) -> subprocess.CompletedProcess[str]:
        observed["command"] = command
        observed.update(options)
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(run_migration_benchmark, "run_bounded_process", fake_run)

    outcome = _run_migration_process(
        (sys.executable, "-c", "pass"),
        project=tmp_path,
        environment={"NO_COLOR": "1"},
    )

    assert outcome.returncode == 0
    assert outcome.stdout == "ok"
    assert observed["cwd"] == tmp_path
    assert observed["timeout"] == run_migration_benchmark.COMMAND_TIMEOUT_SECONDS


def test_real_project_manifest_commands_are_arrays_and_redactable(tmp_path: Path) -> None:
    manifest = {
        "runners": [
            {
                "name": "pytest",
                "kind": "pytest",
                "command": ["{python}", "-m", "pytest", "tests"],
            },
            {
                "name": "testenix",
                "kind": "testenix",
                "command": ["{python}", "-m", "testenix", "run", "secret-suite"],
                "redact_arguments": [4],
            },
        ]
    }

    pytest_runner, testenix_runner = _runners(manifest)

    assert pytest_runner.command[0] == sys.executable
    rendered = _display_command(testenix_runner, tmp_path)
    assert "secret-suite" not in rendered
    assert "<redacted>" in rendered


def test_real_project_runner_contract_records_performance_switches() -> None:
    manifest = {
        "runners": [
            {
                "name": "pytest",
                "kind": "pytest",
                "command": ["{python}", "-m", "pytest", "-n", "4", "tests"],
            },
            {
                "name": "testenix",
                "kind": "testenix",
                "command": [
                    "{python}",
                    "-m",
                    "testenix",
                    "run",
                    "tests_testenix",
                    "--workers",
                    "auto",
                    "--no-history",
                    "--shard-modules",
                ],
            },
        ]
    }

    pytest_runner, testenix_runner = _runners(manifest)

    assert _runner_contract(pytest_runner)["workers_requested"] == "4"
    assert _runner_contract(testenix_runner) == {
        "workers_requested": "auto",
        "history_mode": "disabled",
        "safe_module_sharding": True,
    }
    assert _observed_testenix_workers("Testenix  |  3,000 tests  |  16 files  |  4 workers\n") == 4


@pytest.mark.parametrize(
    ("kind", "command"),
    [
        (
            "pytest",
            ["{python}", "-m", "pytest", "-n", "2", "--numprocesses=4", "tests"],
        ),
        (
            "testenix",
            [
                "{python}",
                "-m",
                "testenix",
                "run",
                "--workers",
                "2",
                "-w=4",
                "tests_testenix",
            ],
        ),
    ],
)
def test_real_project_runner_contract_rejects_duplicate_worker_flags(
    kind: str,
    command: list[str],
) -> None:
    other = (
        {"name": "testenix", "kind": "testenix", "command": ["{python}", "-m", "testenix", "run"]}
        if kind == "pytest"
        else {"name": "pytest", "kind": "pytest", "command": ["{python}", "-m", "pytest"]}
    )
    runner = next(
        candidate
        for candidate in _runners(
            {
                "runners": [
                    {"name": kind, "kind": kind, "command": command},
                    other,
                ]
            }
        )
        if candidate.kind == kind
    )

    with pytest.raises(RuntimeError, match="workers is configured more than once"):
        _runner_contract(runner)


def test_real_project_runner_contract_rejects_conflicting_history_flags() -> None:
    runner = _runners(
        {
            "runners": [
                {"name": "pytest", "kind": "pytest", "command": ["{python}", "-m", "pytest"]},
                {
                    "name": "testenix",
                    "kind": "testenix",
                    "command": [
                        "{python}",
                        "-m",
                        "testenix",
                        "run",
                        "--history",
                        "history.sqlite3",
                        "--no-history",
                    ],
                },
            ]
        }
    )[1]

    with pytest.raises(RuntimeError, match="history is configured more than once"):
        _runner_contract(runner)


def test_real_project_manifest_requires_both_runners_and_same_python() -> None:
    only_pytest = {
        "runners": [
            {"name": "one", "kind": "pytest", "command": ["{python}", "-m", "pytest"]},
            {"name": "two", "kind": "pytest", "command": ["{python}", "-m", "pytest"]},
        ]
    }
    mixed_interpreters = {
        "runners": [
            {"name": "pytest", "kind": "pytest", "command": ["python3", "-m", "pytest"]},
            {
                "name": "testenix",
                "kind": "testenix",
                "command": ["{python}", "-m", "testenix", "run", "tests"],
            },
        ]
    }

    with pytest.raises(RuntimeError, match="at least one pytest and one testenix"):
        _runners(only_pytest)
    with pytest.raises(RuntimeError, match=r"must start with \{python\}"):
        _runners(mixed_interpreters)


def test_real_project_runner_kind_cannot_label_an_arbitrary_script() -> None:
    manifest = {
        "runners": [
            {
                "name": "fake-pytest",
                "kind": "pytest",
                "command": ["{python}", "fake_pytest.py"],
            },
            {
                "name": "testenix",
                "kind": "testenix",
                "command": ["{python}", "-m", "testenix", "run", "tests_testenix"],
            },
        ]
    }

    with pytest.raises(RuntimeError, match="canonical.*pytest"):
        _runners(manifest)


def test_real_project_pytest_validation_checks_total_outcomes() -> None:
    runner = _runners(
        {
            "runners": [
                {
                    "name": "pytest",
                    "kind": "pytest",
                    "command": ["{python}", "-m", "pytest"],
                },
                {
                    "name": "testenix",
                    "kind": "testenix",
                    "command": ["{python}", "-m", "testenix", "run", "tests"],
                },
            ]
        }
    )[0]
    completed = subprocess.CompletedProcess(
        runner.command,
        0,
        stdout="117 passed, 1 skipped in 2.70s\n",
        stderr="",
    )

    _validate_output(runner, completed, expected_tests=118, expected_passed=117)
    with pytest.raises(RuntimeError, match="did not report 119 tests"):
        _validate_output(runner, completed, expected_tests=119, expected_passed=117)


def test_real_project_runtime_identity_hashes_executed_package() -> None:
    environment, _, _ = _environment({"environment": {"NO_COLOR": "1"}})

    identity = _testenix_runtime_identity(ROOT, environment)

    assert identity["version"]
    assert identity["package_files"] > 0
    assert len(identity["package_sha256"]) == 64
    assert isinstance(identity["source_matches_distribution"], bool)


def test_real_project_runtime_identity_rejects_unowned_source_override() -> None:
    environment, _, _ = _environment({"environment": {"NO_COLOR": "1"}})
    environment["PYTHONPATH"] = str(ROOT / "src")

    identity = _testenix_runtime_identity(ROOT, environment)

    assert identity["source_matches_distribution"] is False


def test_tree_fingerprint_records_aggregate_metadata_only(tmp_path: Path) -> None:
    suite = tmp_path / "private-tests"
    suite.mkdir()
    (suite / "test_example.py").write_text("def test_example():\n    pass\n", encoding="utf-8")
    (suite / "notes.txt").write_text("not benchmark source", encoding="utf-8")

    fingerprint = _tree_fingerprint(tmp_path, "private-tests")

    assert set(fingerprint) == {"sha256", "files", "bytes"}
    assert fingerprint["files"] == 1
    assert fingerprint["bytes"] > 0


def test_tree_fingerprint_rejects_empty_python_tree(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.raises(RuntimeError, match="contains no Python files"):
        _tree_fingerprint(tmp_path, "empty")


def _write_verified_migration_report(project: Path) -> tuple[dict[str, object], tuple[object, ...]]:
    source = project / "tests" / "test_example.py"
    generated = project / "tests_testenix" / "test_example.py"
    source.parent.mkdir()
    generated.parent.mkdir()
    source.write_text("def test_example():\n    assert True\n", encoding="utf-8")
    generated.write_text("def test_example():\n    assert True\n", encoding="utf-8")
    test_id = "tests/test_example.py::test_example"

    def summary(runner: str) -> dict[str, object]:
        return {
            "runner": runner,
            "tests": 1,
            "passed": 1,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
            "xfailed": 0,
            "xpassed": 0,
            "outcomes": {test_id: "pass"},
        }

    report = {
        "format": "testenix.migration-report",
        "schema_version": 1,
        "framework": "pytest",
        "status": "published",
        "published": True,
        "converted_tests": 1,
        "originals_modified": False,
        "sources": ["tests"],
        "output": "tests_testenix",
        "source_hashes": {"tests/test_example.py": hashlib.sha256(source.read_bytes()).hexdigest()},
        "generated_files": ["test_example.py"],
        "mappings": [
            {
                "source_id": test_id,
                "target_file": "test_example.py",
                "target_function": "test_example",
                "case_id": None,
            }
        ],
        "baseline": summary("pytest"),
        "native_serial": summary("testenix-serial"),
        "native_parallel": summary("testenix-parallel"),
    }
    reports = project / "reports"
    reports.mkdir()
    (reports / "migration.json").write_text(json.dumps(report), encoding="utf-8")
    manifest: dict[str, object] = {"migration_report": "reports/migration.json"}
    runners = _runners(
        {
            "runners": [
                {
                    "name": "pytest",
                    "kind": "pytest",
                    "command": ["{python}", "-m", "pytest", "--", "tests"],
                },
                {
                    "name": "testenix",
                    "kind": "testenix",
                    "command": [
                        "{python}",
                        "-m",
                        "testenix",
                        "run",
                        "--",
                        "tests_testenix",
                    ],
                },
            ]
        }
    )
    return manifest, runners


def test_real_project_publication_gate_verifies_exact_migration_outcomes(
    tmp_path: Path,
) -> None:
    manifest, runners = _write_verified_migration_report(tmp_path)

    gate = _migration_gate(manifest, tmp_path, 1, 1, runners)

    assert gate is not None
    assert gate["runner_paths_verified"] is True
    assert gate["source_files_verified"] == 1
    assert gate["generated_files_verified"] == 1


def test_real_project_publication_gate_rejects_per_test_outcome_mismatch(
    tmp_path: Path,
) -> None:
    manifest, runners = _write_verified_migration_report(tmp_path)
    report_path = tmp_path / "reports" / "migration.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["native_parallel"]["outcomes"] = {"tests/test_example.py::test_example": "fail"}
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(RuntimeError, match="per-test outcomes are not equivalent"):
        _migration_gate(manifest, tmp_path, 1, 1, runners)


def test_real_project_publication_gate_rejects_added_source_support_file(
    tmp_path: Path,
) -> None:
    manifest, runners = _write_verified_migration_report(tmp_path)
    (tmp_path / "tests" / "conftest.py").write_text(
        "def pytest_configure(config):\n    pass\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="source Python inventory is stale"):
        _migration_gate(manifest, tmp_path, 1, 1, runners)


def test_real_project_publication_gate_rejects_added_native_file(tmp_path: Path) -> None:
    manifest, runners = _write_verified_migration_report(tmp_path)
    (tmp_path / "tests_testenix" / "test_extra.py").write_text(
        "def test_extra():\n    pass\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="generated Python inventory is stale"):
        _migration_gate(manifest, tmp_path, 1, 1, runners)


def test_real_project_publication_gate_binds_runner_paths_to_migration(
    tmp_path: Path,
) -> None:
    manifest, _ = _write_verified_migration_report(tmp_path)
    unrelated = tmp_path / "other"
    unrelated.mkdir()
    runners = _runners(
        {
            "runners": [
                {
                    "name": "pytest",
                    "kind": "pytest",
                    "command": [
                        "{python}",
                        "-m",
                        "pytest",
                        "-k",
                        "tests",
                        "--",
                        "other",
                    ],
                },
                {
                    "name": "testenix",
                    "kind": "testenix",
                    "command": [
                        "{python}",
                        "-m",
                        "testenix",
                        "run",
                        "--tag",
                        "tests_testenix",
                        "--",
                        "other",
                    ],
                },
            ]
        }
    )

    with pytest.raises(RuntimeError, match="paths do not match"):
        _migration_gate(manifest, tmp_path, 1, 1, runners)

    pytest_runner, testenix_runner = runners
    assert _explicit_suite_targets(pytest_runner, tmp_path) == (unrelated.resolve(),)
    assert _explicit_suite_targets(testenix_runner, tmp_path) == (unrelated.resolve(),)


def test_real_project_publication_targets_require_explicit_delimiter(tmp_path: Path) -> None:
    runner = _runners(
        {
            "runners": [
                {
                    "name": "pytest",
                    "kind": "pytest",
                    "command": ["{python}", "-m", "pytest", "tests"],
                },
                {
                    "name": "testenix",
                    "kind": "testenix",
                    "command": ["{python}", "-m", "testenix", "run", "tests_testenix"],
                },
            ]
        }
    )[0]

    with pytest.raises(RuntimeError, match="exactly one '--' delimiter"):
        _explicit_suite_targets(runner, tmp_path)
