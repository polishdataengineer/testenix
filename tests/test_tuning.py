from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import testenix.tuning as tuning_module
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


def test_run_tuning_discards_result_when_project_sources_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    suite = tmp_path / "tests_testenix"
    suite.mkdir()
    test_source = suite / "test_sample.py"
    test_source.write_text("def test_sample(): assert True\n", encoding="utf-8")
    helper = tmp_path / "project_helper.py"
    helper.write_text("VALUE = 1\n", encoding="utf-8")
    spec = _spec("sample", path=str(test_source))
    calls = 0

    def measure(paths: tuple[str, ...], config: TestenixConfig) -> tuple[float, RunResult]:
        nonlocal calls
        del paths, config
        calls += 1
        if calls == 2:
            helper.write_text("VALUE = 2\n", encoding="utf-8")
        return 1.0, _run(spec)

    with pytest.raises(TuningError, match="project sources changed"):
        run_tuning(
            ("tests_testenix",),
            TestenixConfig(),
            candidates=(1,),
            warmups=0,
            repeats=1,
            native_measure=measure,
        )


def test_run_tuning_detects_source_changed_and_restored_inside_sample(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    suite = tmp_path / "tests_testenix"
    suite.mkdir()
    test_source = suite / "test_sample.py"
    test_source.write_text("def test_sample(): assert True\n", encoding="utf-8")
    helper = tmp_path / "project_helper.py"
    original = "VALUE = 1\n"
    helper.write_text(original, encoding="utf-8")
    spec = _spec("sample", path=str(test_source))
    calls = 0

    def measure(paths: tuple[str, ...], config: TestenixConfig) -> tuple[float, RunResult]:
        nonlocal calls
        del paths, config
        calls += 1
        if calls == 2:
            helper.write_text("VALUE = 2\n", encoding="utf-8")
            assert helper.read_text(encoding="utf-8") == "VALUE = 2\n"
            helper.write_text(original, encoding="utf-8")
        return 1.0, _run(spec)

    with pytest.raises(TuningError, match="project sources changed"):
        run_tuning(
            ("tests_testenix",),
            TestenixConfig(),
            candidates=(1,),
            warmups=0,
            repeats=1,
            native_measure=measure,
        )


def test_tuning_source_snapshot_follows_source_directory_symlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    external = tmp_path / "external"
    project.mkdir()
    external.mkdir()
    helper = external / "helper.py"
    helper.write_text("VALUE = 1\n", encoding="utf-8")
    linked = project / "linked_src"
    try:
        linked.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("directory symbolic links are unavailable")
    monkeypatch.chdir(project)

    before = tuning_module._tuning_source_snapshot(("linked_src",))
    helper.write_text("VALUE = 2\n", encoding="utf-8")

    assert tuning_module._tuning_source_snapshot(("linked_src",)) != before


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


def test_tuning_subprocess_timeout_terminates_its_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posix_signals = SimpleNamespace(SIGTERM=15, SIGKILL=9)

    class FakeProcess:
        pid = 4242

        def __init__(self) -> None:
            self.waits = 0
            self.reaped = False

        def wait(self, *, timeout: float) -> int:
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired(("python",), timeout)
            self.reaped = True
            return -15

        def poll(self) -> int | None:
            return -15 if self.reaped else None

        def kill(self) -> None:
            raise AssertionError("process-group cleanup should reap the process")

    process = FakeProcess()
    popen_options: dict[str, object] = {}
    killed_groups: list[tuple[int, int]] = []
    killed_processes: list[tuple[int, int]] = []

    def fake_popen(command: object, **options: object) -> FakeProcess:
        assert command == ("python", "suite.py")
        popen_options.update(options)
        return process

    class FakeTracker:
        def __init__(self, pid: int) -> None:
            assert pid == 4242

        def stop(self) -> dict[int, str]:
            return {5001: "worker-a", 5000: "worker-b"}

    monkeypatch.setattr(tuning_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tuning_module, "signal", posix_signals)
    monkeypatch.setattr(tuning_module, "_PosixTreeTracker", FakeTracker)
    monkeypatch.setattr(tuning_module, "_posix_descendant_pids", lambda pid: (5001, 5000))
    monkeypatch.setattr(
        tuning_module,
        "_process_identity",
        lambda pid: {5001: "worker-a", 5000: "worker-b"}.get(pid),
    )
    monkeypatch.setattr(
        tuning_module,
        "os",
        SimpleNamespace(
            name="posix",
            getpgid=lambda pid: 4242,
            getpgrp=lambda: 9999,
            kill=lambda pid, sig: killed_processes.append((pid, sig)),
            killpg=lambda pid, sig: killed_groups.append((pid, sig)),
        ),
    )

    with pytest.raises(TuningError, match="2s per-run deadline"):
        tuning_module._run_bounded_process(
            ("python", "suite.py"),
            env={},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
            label="native sample",
        )

    assert popen_options["start_new_session"] is True
    assert killed_groups == [
        (4242, posix_signals.SIGTERM),
        (4242, posix_signals.SIGKILL),
    ]
    assert killed_processes == []


def test_posix_descendant_snapshot_is_deepest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = SimpleNamespace(
        returncode=0,
        stdout="1 0\n10 1\n11 10\n12 1\n99 77\ninvalid\n",
    )
    monkeypatch.setattr(tuning_module.subprocess, "run", lambda *args, **kwargs: completed)

    assert tuning_module._posix_descendant_pids(1) == (11, 10, 12)


def test_posix_cleanup_never_signals_a_recycled_descendant_or_root_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed_groups: list[tuple[int, int]] = []
    monkeypatch.setattr(tuning_module, "_process_identity", lambda pid: "new-process")
    monkeypatch.setattr(
        tuning_module,
        "os",
        SimpleNamespace(
            getpgrp=lambda: 9999,
            getpgid=lambda pid: pytest.fail(f"recycled PID {pid} must not be resolved"),
            killpg=lambda group, sig: killed_groups.append((group, sig)),
        ),
    )

    tuning_module._posix_signal_tree(
        4242,
        {5001: "old-process"},
        tuning_module.signal.SIGTERM,
        root_group_owned=False,
    )

    assert killed_groups == []


def test_windows_tuning_timeout_closes_kill_job_before_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        pid = 4343

        def __init__(self) -> None:
            self.waits = 0
            self.reaped = False

        def wait(self, *, timeout: float) -> int:
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired(("python",), timeout)
            self.reaped = True
            return -9

        def poll(self) -> int | None:
            return -9 if self.reaped else None

        def kill(self) -> None:
            self.reaped = True

    class FakeJob:
        def __init__(self) -> None:
            self.closed = False

        def terminate(self) -> bool:
            self.closed = True
            return True

        def close(self) -> bool:
            self.closed = True
            return True

    process = FakeProcess()
    job = FakeJob()
    popen_options: dict[str, object] = {}

    def fake_popen(command: object, **options: object) -> FakeProcess:
        del command
        popen_options.update(options)
        return process

    monkeypatch.setattr(tuning_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        tuning_module.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("taskkill fallback must not run with a Job Object"),
    )
    monkeypatch.setattr(tuning_module, "_windows_kill_job", lambda candidate: job)
    monkeypatch.setattr(tuning_module, "_resume_windows_process", lambda candidate: None)
    monkeypatch.setattr(tuning_module, "os", SimpleNamespace(name="nt"))

    with pytest.raises(TuningError, match="per-run deadline"):
        tuning_module._run_bounded_process(
            ("python", "suite.py"),
            env={},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
            label="windows sample",
        )

    assert job.closed
    assert popen_options["creationflags"] == (
        getattr(tuning_module.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        | getattr(tuning_module.subprocess, "CREATE_SUSPENDED", 0x00000004)
    )


def test_windows_tuning_fails_closed_before_resuming_without_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        pid = 4545

        def __init__(self) -> None:
            self.killed = False

        def kill(self) -> None:
            self.killed = True

        def wait(self, *, timeout: float) -> int:
            assert timeout == tuning_module._PROCESS_TERMINATION_GRACE
            return -9

    process = FakeProcess()
    popen_options: dict[str, object] = {}

    def fake_popen(command: object, **options: object) -> FakeProcess:
        del command
        popen_options.update(options)
        return process

    monkeypatch.setattr(tuning_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tuning_module, "_windows_kill_job", lambda candidate: None)
    monkeypatch.setattr(tuning_module, "os", SimpleNamespace(name="nt"))

    with pytest.raises(TuningError, match="kill-on-close Job Object"):
        tuning_module._run_bounded_process(
            ("python", "suite.py"),
            env={},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
            label="windows sample",
        )

    assert process.killed
    assert int(popen_options["creationflags"]) & 0x00000004


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-tree regression")
def test_tuning_timeout_kills_a_descendant_that_created_its_own_session(
    tmp_path: Path,
) -> None:
    child_pid_path = tmp_path / "child.pid"
    child_source = "import os, time; os.setsid(); time.sleep(30)"
    parent_source = (
        "import pathlib, subprocess, sys, time; "
        f"child = subprocess.Popen([sys.executable, '-c', {child_source!r}]); "
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid)); "
        "time.sleep(30)"
    )

    with pytest.raises(TuningError, match="per-run deadline"):
        tuning_module._run_bounded_process(
            (sys.executable, "-c", parent_source),
            env=os.environ,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1.0,
            label="detached-child sample",
        )

    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"detached timing descendant {child_pid} survived timeout cleanup")


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-tree regression")
def test_tuning_cleans_detached_child_after_fast_successful_leader(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "child-ready"
    marker = tmp_path / "orphan-finished"
    child_source = (
        "import os, pathlib, sys, time\n"
        "os.setsid()\n"
        "pathlib.Path(sys.argv[1]).write_text('ready', encoding='utf-8')\n"
        "time.sleep(0.8)\n"
        "pathlib.Path(sys.argv[2]).write_text('orphan', encoding='utf-8')\n"
    )
    parent_source = (
        "import pathlib, subprocess, sys, time\n"
        f"ready = pathlib.Path({str(ready)!r})\n"
        f"subprocess.Popen([sys.executable, '-c', {child_source!r}, str(ready), "
        f"{str(marker)!r}])\n"
        "deadline = time.monotonic() + 2\n"
        "while not ready.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.005)\n"
        "time.sleep(0.05)\n"
    )

    assert (
        tuning_module._run_bounded_process(
            (sys.executable, "-c", parent_source),
            env=os.environ,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
            label="fast-leader sample",
        )
        == 0
    )

    assert ready.exists()
    time.sleep(1.0)
    assert not marker.exists()


def test_run_tuning_rejects_invalid_per_run_deadline() -> None:
    with pytest.raises(ValueError, match="run_timeout"):
        run_tuning(("tests",), TestenixConfig(), run_timeout=0.0)


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


def test_worker_recommendation_compare_and_swap_preserves_concurrent_edit(
    tmp_path: Path,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    original = b'[tool.testenix]\npaths = ["tests_testenix"]\n'
    concurrent = b'[tool.testenix]\npaths = ["changed_elsewhere"]\n'
    pyproject.write_bytes(original)
    pyproject.write_bytes(concurrent)

    with pytest.raises(ValueError, match="configuration changed while tuning"):
        write_worker_recommendation(pyproject, 4, expected_source=original)

    assert pyproject.read_bytes() == concurrent


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


def test_tune_write_refuses_configuration_changed_during_measurement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    original = '[tool.testenix]\npaths = ["tests_testenix"]\n'
    changed = '[tool.testenix]\npaths = ["other_tests"]\n'
    pyproject.write_text(original, encoding="utf-8")

    def measure(*args: object, **kwargs: object) -> TuningReport:
        del args, kwargs
        pyproject.write_text(changed, encoding="utf-8")
        return _report()

    monkeypatch.setattr("testenix.tuning.run_tuning", measure)

    assert main(["tune", "--config", str(pyproject), "--write"]) == 3
    assert pyproject.read_text(encoding="utf-8") == changed
    assert "configuration changed while tuning" in capsys.readouterr().err


def test_tune_write_refuses_edit_in_the_final_write_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    original = b'[tool.testenix]\npaths = ["tests_testenix"]\n'
    concurrent = b'[tool.testenix]\npaths = ["changed_elsewhere"]\n'
    pyproject.write_bytes(original)
    monkeypatch.setattr("testenix.tuning.run_tuning", lambda *args, **kwargs: _report())
    real_write = write_worker_recommendation

    def edit_then_write(
        path: str | Path,
        workers: int,
        *,
        expected_source: bytes | None,
    ) -> bool:
        pyproject.write_bytes(concurrent)
        return real_write(path, workers, expected_source=expected_source)

    monkeypatch.setattr("testenix.config.write_worker_recommendation", edit_then_write)

    assert main(["tune", "--config", str(pyproject), "--write"]) == 3
    assert pyproject.read_bytes() == concurrent
    assert "configuration changed while tuning" in capsys.readouterr().err


def test_tune_cli_passes_per_run_deadline_to_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[float] = []

    def measure(*args: object, **kwargs: object) -> TuningReport:
        del args
        observed.append(float(kwargs["run_timeout"]))
        return _report()

    monkeypatch.setattr("testenix.tuning.run_tuning", measure)

    assert main(["tune", "tests_testenix", "--run-timeout", "12.5"]) == 0
    assert observed == [12.5]


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
        ("--run-timeout", "0"),
    ],
)
def test_tune_cli_rejects_invalid_measurement_options(arguments: tuple[str, ...]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["tune", *arguments])

    assert exit_info.value.code == 2
