"""Reproducible local comparison against pytest and pytest-xdist."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import shlex
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

if __package__:
    from benchmarks.process_control import run_bounded_process
else:  # direct ``python benchmarks/run_benchmark.py`` execution
    from process_control import run_bounded_process

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class Measurement:
    command: str
    argv: tuple[str, ...]
    samples: tuple[float, ...]
    stdout_bytes: tuple[int, ...]
    stderr_bytes: tuple[int, ...]
    observed_workers: tuple[int | None, ...]

    @property
    def median(self) -> float:
        return statistics.median(self.samples)

    @property
    def minimum(self) -> float:
        return min(self.samples)

    @property
    def maximum(self) -> float:
        return max(self.samples)

    @property
    def mean(self) -> float:
        return statistics.fmean(self.samples)

    @property
    def stdev(self) -> float:
        return statistics.stdev(self.samples) if len(self.samples) > 1 else 0.0


def _display_argv(command: list[str]) -> tuple[str, ...]:
    parts: list[str] = []
    for value in command:
        if value == sys.executable:
            parts.append("python")
        elif "testenix-benchmark-" in value:
            parts.append("<suite>")
        else:
            parts.append(value)
    return tuple(parts)


def _display_command(command: list[str]) -> str:
    return shlex.join(_display_argv(command))


ModuleLayout = Literal["balanced", "dominant", "single"]
HistoryMode = Literal["disabled", "default"]
WorkerRequest = int | Literal["auto"]
XdistStrategy = Literal["load", "loadfile", "loadscope", "worksteal"]
ShardingMode = Literal["disabled", "safe"]


def _module_indexes(
    count: int,
    module_count: int,
    layout: ModuleLayout,
    dominant_fraction: float,
) -> tuple[int, ...]:
    if layout == "single":
        return (0,) * count
    if layout == "balanced":
        return tuple(index % module_count for index in range(count))
    if module_count == 1:
        return (0,) * count

    dominant_count = min(count, max(1, math.ceil(count * dominant_fraction)))
    return tuple(
        0 if index < dominant_count else 1 + ((index - dominant_count) % (module_count - 1))
        for index in range(count)
    )


def _generate_suite(
    directory: Path,
    count: int,
    uneven: bool,
    module_count: int,
    *,
    layout: ModuleLayout,
    dominant_fraction: float,
) -> tuple[int, ...]:
    modules = [["import time", ""] for _ in range(module_count)]
    assignments = _module_indexes(count, module_count, layout, dominant_fraction)
    for index, module_index in enumerate(assignments):
        lines = modules[module_index]
        lines.append(f"def test_{index:05d}():")
        if uneven and index % 100 == 0:
            # Scale the amount of work with suite size and deliberately skew a
            # subset of modules. This exposes scheduler tail latency instead of
            # turning into another no-op benchmark above a few hundred tests.
            duration = 0.0005 * ((index // 100) % 16 + 1)
            lines.append(f"    time.sleep({duration!r})")
        else:
            lines.append("    pass")
        lines.append("")
    for module_index, lines in enumerate(modules):
        (directory / f"test_generated_{module_index:03d}.py").write_text(
            "\n".join(lines),
            encoding="utf-8",
        )
    return tuple(assignments.count(index) for index in range(module_count))


def _benchmark_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONHASHSEED"] = "0"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTEST_ADDOPTS"] = ""
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment["NO_COLOR"] = "1"
    environment["TERM"] = "dumb"
    source_root = str(ROOT / "src")
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        f"{source_root}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else source_root
    )

    return environment


def _validate_completed_run(
    name: str,
    command: list[str],
    completed: subprocess.CompletedProcess[str],
    test_count: int,
) -> None:
    if completed.returncode != 0:
        raise RuntimeError(
            f"benchmark command failed ({completed.returncode}): {' '.join(command)}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    if name == "testenix":
        pattern = rf"(?m)^\s*{test_count} tests(?:,|\s)"
    else:
        pattern = rf"\b{test_count} passed\b"
    if re.search(pattern, completed.stdout) is None:
        raise RuntimeError(
            f"benchmark command did not report {test_count} completed tests: "
            f"{' '.join(command)}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )


def _run_once(
    name: str,
    command: list[str],
    *,
    environment: dict[str, str],
    test_count: int,
    working_directory: Path,
    timeout: float,
) -> tuple[float, int, int, int | None]:
    started = time.perf_counter()
    completed = run_bounded_process(
        command,
        env=environment,
        cwd=working_directory,
        timeout=timeout,
    )
    elapsed = time.perf_counter() - started
    _validate_completed_run(name, command, completed, test_count)
    worker_match = (
        re.search(r"(?m)^Testenix\s+\|.*\|\s+(\d+)\s+workers?\s*$", completed.stdout)
        if name == "testenix"
        else None
    )
    if name == "testenix" and worker_match is None:
        raise RuntimeError(
            "Testenix benchmark output did not expose the actual worker count; "
            "refusing to record ambiguous performance evidence"
        )
    return (
        elapsed,
        len(completed.stdout.encode("utf-8")),
        len(completed.stderr.encode("utf-8")),
        int(worker_match.group(1)) if worker_match is not None else None,
    )


def _measure_commands(
    commands: dict[str, list[str]],
    *,
    repeats: int,
    warmups: int,
    test_count: int,
    working_directory: Path,
    timeout: float,
) -> tuple[dict[str, Measurement], tuple[tuple[str, ...], ...]]:
    """Measure in deterministic rotated rounds instead of framework-sized blocks."""

    environment = _benchmark_environment()
    names = tuple(commands)
    for _ in range(warmups):
        for name in names:
            _run_once(
                name,
                commands[name],
                environment=environment,
                test_count=test_count,
                working_directory=working_directory,
                timeout=timeout,
            )

    samples: dict[str, list[float]] = {name: [] for name in names}
    stdout_bytes: dict[str, list[int]] = {name: [] for name in names}
    stderr_bytes: dict[str, list[int]] = {name: [] for name in names}
    observed_workers: dict[str, list[int | None]] = {name: [] for name in names}
    orders: list[tuple[str, ...]] = []
    for round_index in range(repeats):
        shift = round_index % len(names)
        order = (*names[shift:], *names[:shift])
        orders.append(order)
        for name in order:
            elapsed, stdout_size, stderr_size, observed_worker_count = _run_once(
                name,
                commands[name],
                environment=environment,
                test_count=test_count,
                working_directory=working_directory,
                timeout=timeout,
            )
            samples[name].append(elapsed)
            stdout_bytes[name].append(stdout_size)
            stderr_bytes[name].append(stderr_size)
            observed_workers[name].append(observed_worker_count)

    return (
        {
            name: Measurement(
                _display_command(commands[name]),
                _display_argv(commands[name]),
                tuple(samples[name]),
                tuple(stdout_bytes[name]),
                tuple(stderr_bytes[name]),
                tuple(observed_workers[name]),
            )
            for name in names
        },
        tuple(orders),
    )


def _measurement_dict(measurement: Measurement, *, test_count: int) -> dict[str, Any]:
    data = asdict(measurement)
    data.update(
        median=measurement.median,
        minimum=measurement.minimum,
        maximum=measurement.maximum,
        mean=measurement.mean,
        stdev=measurement.stdev,
        median_tests_per_second=test_count / measurement.median,
        median_stdout_bytes=statistics.median(measurement.stdout_bytes),
        median_stderr_bytes=statistics.median(measurement.stderr_bytes),
        observed_workers=list(measurement.observed_workers),
    )
    return data


def _distribution_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "not-installed"


def _git_value(*arguments: str) -> str | None:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _cpu_model() -> str:
    if sys.platform == "darwin":
        completed = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True,
            text=True,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            return completed.stdout.strip()

    if sys.platform.startswith("linux"):
        try:
            cpuinfo = Path("/proc/cpuinfo").read_text(encoding="utf-8")
        except OSError:
            cpuinfo = ""
        for line in cpuinfo.splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip().lower() in {"hardware", "model name"}:
                return value.strip()

    return (
        platform.processor().strip()
        or os.environ.get("PROCESSOR_IDENTIFIER", "").strip()
        or "unknown"
    )


def _provenance() -> dict[str, Any]:
    status = _git_value("status", "--porcelain")
    lock_path = ROOT / "uv.lock"
    return {
        "commit": _git_value("rev-parse", "HEAD"),
        "dirty": bool(status) if status is not None else None,
        "lock_sha256": (
            hashlib.sha256(lock_path.read_bytes()).hexdigest() if lock_path.exists() else None
        ),
        "versions": {
            "pytest": _distribution_version("pytest"),
            "pytest_xdist": _distribution_version("pytest-xdist"),
            "testenix": _distribution_version("testenix"),
        },
    }


def run_benchmark(
    *,
    test_count: int,
    repeats: int,
    warmups: int,
    workers: WorkerRequest,
    uneven: bool,
    module_count: int | None = None,
    module_layout: ModuleLayout = "balanced",
    dominant_fraction: float = 0.5,
    history_mode: HistoryMode = "disabled",
    xdist_strategy: XdistStrategy = "load",
    sharding_mode: ShardingMode = "disabled",
    timeout: float = 900.0,
) -> dict[str, Any]:
    if test_count < 1 or repeats < 1 or warmups < 0:
        raise ValueError("test_count/repeats must be positive and warmups cannot be negative")
    if workers != "auto" and (
        isinstance(workers, bool) or not isinstance(workers, int) or workers < 1
    ):
        raise ValueError("workers must be a positive integer or 'auto'")
    if module_count is not None and module_count < 1:
        raise ValueError("module_count must be positive")
    if not isinstance(uneven, bool):
        raise TypeError("uneven must be a boolean")
    if module_layout not in {"balanced", "dominant", "single"}:
        raise ValueError("module_layout must be balanced, dominant, or single")
    if history_mode not in {"disabled", "default"}:
        raise ValueError("history_mode must be disabled or default")
    if xdist_strategy not in {"load", "loadfile", "loadscope", "worksteal"}:
        raise ValueError("unsupported pytest-xdist strategy")
    if sharding_mode not in {"disabled", "safe"}:
        raise ValueError("sharding_mode must be disabled or safe")
    if not 0.0 < dominant_fraction < 1.0:
        raise ValueError("dominant_fraction must be greater than zero and less than one")
    if not 0 < timeout <= 3600:
        raise ValueError("timeout must be greater than zero and at most 3600 seconds")
    with tempfile.TemporaryDirectory(prefix="testenix-benchmark-") as temporary:
        suite = Path(temporary)
        xdist_workers = (os.cpu_count() or 1) if workers == "auto" else workers
        resolved_module_count = min(
            test_count,
            1 if module_layout == "single" else (module_count if module_count is not None else 16),
        )
        module_sizes = _generate_suite(
            suite,
            test_count,
            uneven,
            resolved_module_count,
            layout=module_layout,
            dominant_fraction=dominant_fraction,
        )
        commands = {
            "pytest": [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                str(suite),
            ],
            "pytest_xdist": [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "xdist.plugin",
                "-p",
                "no:cacheprovider",
                "-n",
                str(xdist_workers),
                "--dist",
                xdist_strategy,
                str(suite),
            ],
            "testenix": [
                sys.executable,
                "-m",
                "testenix",
                "run",
                str(suite),
                "--workers",
                str(workers),
                *(["--no-history"] if history_mode == "disabled" else []),
                *(["--shard-modules"] if sharding_mode == "safe" else []),
            ],
        }
        measurements, execution_orders = _measure_commands(
            commands,
            repeats=repeats,
            warmups=warmups,
            test_count=test_count,
            working_directory=suite,
            timeout=timeout,
        )

    return {
        "schema_version": 2,
        "recorded_at": datetime.now(UTC).isoformat(),
        "provenance": _provenance(),
        "environment": {
            "cpu_count": os.cpu_count(),
            "cpu_model": _cpu_model(),
            "machine": platform.machine(),
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "scenario": {
            "repeats": repeats,
            "test_count": test_count,
            "test_modules": resolved_module_count,
            "module_layout": module_layout,
            "module_sizes": module_sizes,
            "dominant_fraction": dominant_fraction if module_layout == "dominant" else None,
            "uneven": uneven,
            "warmups": warmups,
            "workers": workers,
            "workers_requested": workers,
            "xdist_workers": xdist_workers,
            "history_mode": history_mode,
            "history_cli": "--no-history" if history_mode == "disabled" else "default",
            "sharding_mode": sharding_mode,
            "sharding_cli": "--shard-modules" if sharding_mode == "safe" else None,
            "xdist_strategy": xdist_strategy,
            "timeout_seconds": timeout,
            "measured_execution_orders": execution_orders,
        },
        "measurements": {
            name: _measurement_dict(measurement, test_count=test_count)
            for name, measurement in measurements.items()
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tests", type=int, default=1_000)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--workers", type=_worker_request, default=min(4, os.cpu_count() or 1))
    parser.add_argument(
        "--modules",
        type=int,
        default=None,
        help="generated module count (default: 16; single layout always uses one)",
    )
    parser.add_argument(
        "--sharding-mode",
        choices=("disabled", "safe"),
        default="disabled",
        help="enable Testenix's explicit safe-module sharding with --shard-modules",
    )
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--uneven", action="store_true")
    parser.add_argument(
        "--module-layout",
        choices=("balanced", "dominant", "single"),
        default="balanced",
        help="test-count distribution across generated modules",
    )
    parser.add_argument(
        "--dominant-fraction",
        type=float,
        default=0.5,
        help="fraction of tests placed in module zero for --module-layout dominant",
    )
    parser.add_argument(
        "--history-mode",
        choices=("disabled", "default"),
        default="disabled",
        help="disable history (historical baseline) or exercise the default history database",
    )
    parser.add_argument(
        "--xdist-strategy",
        choices=("load", "loadfile", "loadscope", "worksteal"),
        default="load",
    )
    parser.add_argument("--output", type=Path)
    return parser


def _worker_request(value: str) -> WorkerRequest:
    if value == "auto":
        return "auto"
    try:
        workers = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("workers must be a positive integer or 'auto'") from error
    if workers < 1:
        raise argparse.ArgumentTypeError("workers must be positive")
    return workers


def main() -> int:
    arguments = build_parser().parse_args()
    if arguments.tests < 1 or arguments.repeats < 1 or arguments.warmups < 0:
        raise SystemExit("tests/repeats must be positive and warmups cannot be negative")
    if arguments.modules is not None and arguments.modules < 1:
        raise SystemExit("modules must be positive")
    if not 0.0 < arguments.dominant_fraction < 1.0:
        raise SystemExit("dominant-fraction must be greater than zero and less than one")
    if not 0 < arguments.timeout <= 3600:
        raise SystemExit("timeout must be greater than zero and at most 3600 seconds")
    result = run_benchmark(
        test_count=arguments.tests,
        repeats=arguments.repeats,
        warmups=arguments.warmups,
        workers=arguments.workers,
        uneven=arguments.uneven,
        module_count=arguments.modules,
        module_layout=arguments.module_layout,
        dominant_fraction=arguments.dominant_fraction,
        history_mode=arguments.history_mode,
        xdist_strategy=arguments.xdist_strategy,
        sharding_mode=arguments.sharding_mode,
        timeout=arguments.timeout,
    )
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
