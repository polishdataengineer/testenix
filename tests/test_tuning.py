from __future__ import annotations

import json
from pathlib import Path

import pytest

from testenix.cli import main
from testenix.config import TestenixConfig, load_config, write_worker_recommendation
from testenix.contracts import RunResult, Status, TestResult, TestSpec
from testenix.tuning import (
    TuningCandidate,
    TuningError,
    TuningReport,
    default_worker_candidates,
    execution_units,
    resolve_adaptive_workers,
    run_tuning,
)


def _spec(
    name: str,
    *,
    path: str | None = None,
    timeout: float | None = None,
) -> TestSpec:
    effective_path = path or f"tests/test_{name}.py"
    return TestSpec(
        id=f"{effective_path}::test_{name}",
        path=effective_path,
        module_name=f"test_{name}",
        function_name=f"test_{name}",
        display_name=f"test_{name}",
        timeout=timeout,
    )


def _run(*specs: TestSpec, status: Status = Status.PASS) -> RunResult:
    return RunResult(
        run_id="tuning-run",
        tests=tuple(
            TestResult(test=spec, status=status, attempts=(), duration=float(index + 1))
            for index, spec in enumerate(specs)
        ),
        collection_issues=(),
        started_at=1.0,
        finished_at=2.0,
    )


def _report(recommended_workers: int = 2) -> TuningReport:
    return TuningReport(
        paths=("tests_testenix",),
        warmups=1,
        repeats=3,
        discovered_tests=4,
        execution_units=2,
        model_recommendation=2,
        recommended_workers=recommended_workers,
        candidates=(
            TuningCandidate(1, (2.0, 2.1, 2.0)),
            TuningCandidate(2, (1.0, 1.1, 1.0)),
        ),
    )


def test_adaptive_workers_preserve_explicit_configuration() -> None:
    specs = (_spec("first"), _spec("second"))

    assert resolve_adaptive_workers(TestenixConfig(workers=7), specs, {}) == 7
    assert TestenixConfig(workers=7).resolve_workers(specs, {}) == 7


def test_adaptive_workers_use_module_units_history_and_spawn_cost() -> None:
    dominant = _spec("dominant")
    specs = (dominant, _spec("small_a"), _spec("small_b"), _spec("small_c"))
    durations = {
        dominant.id: 10.0,
        specs[1].id: 1.0,
        specs[2].id: 1.0,
        specs[3].id: 1.0,
    }

    assert (
        resolve_adaptive_workers(
            TestenixConfig(),
            specs,
            durations,
            spawn_cost=0.1,
            cpu_count=32,
        )
        == 2
    )
    assert (
        resolve_adaptive_workers(
            TestenixConfig(),
            specs,
            durations,
            spawn_cost=20.0,
            cpu_count=32,
        )
        == 1
    )


def test_adaptive_workers_do_not_split_a_normal_module_but_isolate_timeouts() -> None:
    shared_path = "tests/test_shared.py"
    specs = (
        _spec("first", path=shared_path),
        _spec("second", path=shared_path),
        _spec("timed_a", path=shared_path, timeout=1.0),
        _spec("timed_b", path=shared_path, timeout=2.0),
    )

    units = execution_units(specs, {})

    assert len(units) == 3
    assert [(unit.tests, unit.isolated) for unit in units] == [
        (2, False),
        (1, True),
        (1, True),
    ]
    assert resolve_adaptive_workers(TestenixConfig(), specs[:2], {}, cpu_count=64) == 1


def test_adaptive_workers_see_only_explicitly_shardable_module_tests_as_units() -> None:
    shared_path = "tests/test_shared.py"
    specs = tuple(_spec(f"case_{index}", path=shared_path) for index in range(20))

    assert len(execution_units(specs, {})) == 1
    assert len(execution_units(specs, {}, shardable_paths={shared_path})) == 20
    assert resolve_adaptive_workers(TestenixConfig(), specs, {}, cpu_count=64) == 1
    assert (
        resolve_adaptive_workers(
            TestenixConfig(),
            specs,
            {},
            cpu_count=64,
            shardable_paths={shared_path},
        )
        == 4
    )


def test_equal_independent_units_scale_when_spawn_is_free() -> None:
    specs = tuple(_spec(f"case_{index}") for index in range(4))

    assert (
        resolve_adaptive_workers(
            TestenixConfig(),
            specs,
            {spec.id: 1.0 for spec in specs},
            spawn_cost=0.0,
            cpu_count=4,
        )
        == 4
    )
    assert default_worker_candidates(13, model_recommendation=2, cpu_count=14) == (1, 2, 4)


def test_default_tuning_candidates_do_not_expand_to_a_large_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("testenix.tuning.os.cpu_count", lambda: 192)
    monkeypatch.setattr("testenix.tuning.os.process_cpu_count", lambda: 192, raising=False)
    monkeypatch.setattr(
        "testenix.tuning.os.sched_getaffinity",
        lambda _process: frozenset(range(192)),
        raising=False,
    )

    assert default_worker_candidates(3_000, model_recommendation=4) == (1, 2, 4)


def test_cold_start_caps_large_unknown_suites_at_four_workers() -> None:
    synthetic_100k = tuple(
        _spec(f"case_{index}", path=f"tests/test_generated_{index % 16}.py")
        for index in range(100_000)
    )
    skewed_118 = tuple(
        _spec(f"real_{index}", path=f"tests/test_real_{min(index // 9, 12)}.py")
        for index in range(118)
    )

    assert (
        resolve_adaptive_workers(
            TestenixConfig(),
            synthetic_100k,
            {},
            cpu_count=64,
        )
        == 4
    )
    assert (
        resolve_adaptive_workers(
            TestenixConfig(),
            skewed_118,
            {},
            cpu_count=64,
        )
        == 4
    )


def test_partial_history_does_not_guess_duration_for_a_new_module() -> None:
    specs = tuple(_spec(f"known_{index}") for index in range(6)) + (_spec("new_module"),)
    durations = {spec.id: 0.01 for spec in specs[:-1]}

    assert (
        resolve_adaptive_workers(
            TestenixConfig(),
            specs,
            durations,
            spawn_cost=20.0,
            cpu_count=64,
        )
        == 4
    )


def test_run_tuning_counterbalances_candidates_and_selects_measured_median() -> None:
    specs = (_spec("first"), _spec("second"), _spec("third"), _spec("fourth"))
    calls: list[int] = []
    elapsed = {1: 4.0, 2: 2.0, 4: 3.0}

    def measure(paths: tuple[str, ...], config: TestenixConfig) -> tuple[float, RunResult]:
        assert paths == ("tests_testenix",)
        assert isinstance(config.workers, int)
        assert config.history_path is None
        calls.append(config.workers)
        return elapsed[config.workers], _run(*specs)

    report = run_tuning(
        ("tests_testenix",),
        TestenixConfig(),
        candidates=(4, 1, 2, 2),
        warmups=1,
        repeats=3,
        native_measure=measure,
    )

    assert report.recommended_workers == 2
    assert [candidate.workers for candidate in report.candidates] == [1, 2, 4]
    assert [candidate.samples for candidate in report.candidates] == [
        (4.0, 4.0, 4.0),
        (2.0, 2.0, 2.0),
        (3.0, 3.0, 3.0),
    ]
    # Probe, forward warmups, then forward/reverse/forward measured rounds.
    assert calls == [1, 1, 2, 4, 1, 2, 4, 4, 2, 1, 1, 2, 4]


def test_run_tuning_prefers_fewer_workers_within_measurement_tolerance() -> None:
    specs = tuple(_spec(f"case_{index}") for index in range(4))
    elapsed = {1: 2.0, 2: 1.004, 4: 1.0}

    def measure(paths: tuple[str, ...], config: TestenixConfig) -> tuple[float, RunResult]:
        del paths
        assert isinstance(config.workers, int)
        return elapsed[config.workers], _run(*specs)

    report = run_tuning(
        ("tests",),
        TestenixConfig(),
        candidates=(1, 2, 4),
        warmups=0,
        repeats=3,
        native_measure=measure,
    )

    assert report.recommended_workers == 2


def test_run_tuning_rejects_changed_outcomes() -> None:
    spec = _spec("unstable")
    calls = 0

    def measure(paths: tuple[str, ...], config: TestenixConfig) -> tuple[float, RunResult]:
        nonlocal calls
        del paths, config
        calls += 1
        status = Status.PASS if calls == 1 else Status.FAIL
        return 1.0, _run(spec, status=status)

    with pytest.raises(TuningError, match="failed with exit code"):
        run_tuning(
            ("tests",),
            TestenixConfig(),
            candidates=(1,),
            warmups=1,
            repeats=1,
            native_measure=measure,
        )


def test_run_tuning_can_compare_pytest_without_cache() -> None:
    spec = _spec("ok")
    pytest_calls: list[tuple[str, ...]] = []

    def measure_native(paths: tuple[str, ...], config: TestenixConfig) -> tuple[float, RunResult]:
        del paths, config
        return 1.0, _run(spec)

    def measure_pytest(paths: tuple[str, ...]) -> tuple[float, int]:
        pytest_calls.append(paths)
        return 2.0, 0

    report = run_tuning(
        ("tests_testenix",),
        TestenixConfig(),
        candidates=(1,),
        warmups=1,
        repeats=3,
        pytest_paths=("tests",),
        native_measure=measure_native,
        pytest_measure=measure_pytest,
    )

    assert report.pytest_samples == (2.0, 2.0, 2.0)
    assert report.pytest_over_native == 2.0
    assert pytest_calls == [("tests",)] * 4


def test_run_tuning_measures_native_candidates_in_fresh_cli_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    suite = tmp_path / "tests_testenix"
    suite.mkdir()
    (suite / "test_sample.py").write_text(
        "def test_one(): assert True\ndef test_two(): assert 2 + 2 == 4\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        """
[tool.testenix]
shard_modules = true
json = "must-not-be-written.json"
junit = "must-not-be-written.xml"
""".lstrip(),
        encoding="utf-8",
    )

    report = run_tuning(
        ("tests_testenix",),
        TestenixConfig(history_path=None),
        candidates=(1,),
        warmups=0,
        repeats=1,
    )

    assert report.discovered_tests == 2
    assert report.execution_units == 1
    assert report.recommended_workers == 1
    assert report.candidates[0].median > 0.0
    assert not (tmp_path / "must-not-be-written.json").exists()
    assert not (tmp_path / "must-not-be-written.xml").exists()


def test_worker_recommendation_updates_only_the_testenix_table(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[project]
name = "sample"

[tool.testenix]
workers = 2 # measured locally
paths = ["tests_testenix"]

[tool.ruff]
line-length = 100
""".lstrip(),
        encoding="utf-8",
    )

    assert write_worker_recommendation(pyproject, 4)
    assert load_config(pyproject).workers == 4
    contents = pyproject.read_text(encoding="utf-8")
    assert contents.count("workers = 4") == 1
    assert "workers = 4 # measured locally" in contents
    assert "[tool.ruff]\nline-length = 100" in contents
    assert not write_worker_recommendation(pyproject, 4)


def test_worker_recommendation_fails_closed_on_multiline_toml(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    original = '[tool.testenix]\ntags = ["""\nworkers = 99\n"""]\n'
    pyproject.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="refusing an update"):
        write_worker_recommendation(pyproject, 4)

    assert pyproject.read_text(encoding="utf-8") == original


def test_worker_recommendation_preserves_crlf_and_rejects_symlinks(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_bytes(b"[tool.testenix]\r\nworkers = 2\r\n")

    assert write_worker_recommendation(pyproject, 3)
    assert pyproject.read_bytes() == b"[tool.testenix]\r\nworkers = 3\r\n"

    link = tmp_path / "linked.toml"
    try:
        link.symlink_to(pyproject)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    with pytest.raises(ValueError, match="symbolic link"):
        write_worker_recommendation(link, 4)
    assert load_config(pyproject).workers == 3


def test_tune_cli_never_writes_config_without_explicit_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    original = '[tool.testenix]\npaths = ["tests_testenix"]\n'
    pyproject.write_text(original, encoding="utf-8")
    monkeypatch.setattr("testenix.tuning.run_tuning", lambda *args, **kwargs: _report())

    assert main(["tune", "--config", str(pyproject), "--repeats", "3"]) == 0
    assert pyproject.read_text(encoding="utf-8") == original
    assert "Recommended workers: 2" in capsys.readouterr().out

    assert main(["tune", "--config", str(pyproject), "--write"]) == 0
    assert load_config(pyproject).workers == 2
    assert "Wrote workers = 2" in capsys.readouterr().out


def test_tune_cli_json_stdout_is_machine_readable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("testenix.tuning.run_tuning", lambda *args, **kwargs: _report())

    assert main(["tune", "--json", "-", "tests_testenix"]) == 0
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document["schema"] == "testenix.tuning-report"
    assert document["recommended_workers"] == 2
    assert "Recommended workers: 2" in captured.err


def test_benchmark_alias_runs_the_same_tuning_service(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("testenix.tuning.run_tuning", lambda *args, **kwargs: _report())

    assert main(["benchmark", "tests_testenix", "--candidates", "1,2"]) == 0
    assert "Recommended workers: 2" in capsys.readouterr().out


def test_tune_write_rejects_positional_suite_different_from_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[tool.testenix]\npaths = ["configured"]\n', encoding="utf-8")
    called = False

    def should_not_run(*args: object, **kwargs: object) -> TuningReport:
        nonlocal called
        called = True
        return _report()

    monkeypatch.setattr("testenix.tuning.run_tuning", should_not_run)

    assert main(["tune", "other", "--config", str(pyproject), "--write"]) == 2
    assert not called
    assert pyproject.read_text(encoding="utf-8") == ('[tool.testenix]\npaths = ["configured"]\n')


@pytest.mark.parametrize(
    ("configuration", "arguments"),
    [
        ('paths = ["tests_testenix"]\n', ("--shard-modules",)),
        (
            'paths = ["tests_testenix"]\nshard_modules = true\n',
            ("--no-shard-modules",),
        ),
        ('paths = ["tests_testenix"]\n', ("--manifest", "transient.json")),
        (
            'paths = ["tests_testenix"]\nmanifest = "configured.json"\n',
            ("--manifest", "transient.json"),
        ),
    ],
)
def test_tune_write_rejects_transient_execution_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    configuration: str,
    arguments: tuple[str, ...],
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    original = f"[tool.testenix]\n{configuration}"
    pyproject.write_text(original, encoding="utf-8")
    called = False

    def should_not_run(*args: object, **kwargs: object) -> TuningReport:
        nonlocal called
        called = True
        return _report()

    monkeypatch.setattr("testenix.tuning.run_tuning", should_not_run)

    assert main(["tune", "--config", str(pyproject), *arguments, "--write"]) == 2
    assert not called
    assert pyproject.read_text(encoding="utf-8") == original
    message = capsys.readouterr().err
    assert "--write refuses a workers-only recommendation" in message
    assert "transient execution-profile override" in message


def test_tune_write_allows_matching_explicit_execution_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.testenix]\npaths = ["tests_testenix"]\nshard_modules = true\n',
        encoding="utf-8",
    )
    measured_configs: list[TestenixConfig] = []

    def measure(*args: object, **kwargs: object) -> TuningReport:
        measured_configs.append(args[1])
        return _report()

    monkeypatch.setattr("testenix.tuning.run_tuning", measure)

    assert main(["tune", "--config", str(pyproject), "--shard-modules", "--write"]) == 0
    assert measured_configs[0].shard_modules is True
    assert load_config(pyproject).workers == 2


def test_tune_json_refuses_existing_and_configuration_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    original = '[tool.testenix]\npaths = ["tests_testenix"]\n'
    pyproject.write_text(original, encoding="utf-8")
    monkeypatch.setattr("testenix.tuning.run_tuning", lambda *args, **kwargs: _report())

    assert main(["tune", "--config", str(pyproject), "--json", str(pyproject)]) == 2
    assert pyproject.read_text(encoding="utf-8") == original

    existing = tmp_path / "report.json"
    existing.write_text("keep", encoding="utf-8")
    assert main(["tune", "--config", str(pyproject), "--json", str(existing)]) == 2
    assert existing.read_text(encoding="utf-8") == "keep"


def test_tune_cli_contains_unexpected_exceptions(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def explode(*args: object, **kwargs: object) -> TuningReport:
        raise RuntimeError("boom")

    monkeypatch.setattr("testenix.tuning.run_tuning", explode)

    assert main(["tune", "tests_testenix", "--candidates", "1"]) == 3
    assert "tuning error: boom" in capsys.readouterr().err


@pytest.mark.parametrize(
    "arguments",
    [
        ("--candidates", "1,auto"),
        ("--candidates", "1,,2"),
        ("--repeats", "0"),
        ("--warmups", "-1"),
    ],
)
def test_tune_cli_rejects_invalid_measurement_options(arguments: tuple[str, ...]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["tune", *arguments])

    assert exit_info.value.code == 2
