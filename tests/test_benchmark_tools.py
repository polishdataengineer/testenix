from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    run_benchmark = importlib.import_module("benchmarks.run_benchmark")
    run_project_benchmark = importlib.import_module("benchmarks.run_project_benchmark")
    run_scaling_matrix = importlib.import_module("benchmarks.run_scaling_matrix")
finally:
    sys.path.pop(0)

_module_indexes = run_benchmark._module_indexes
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
