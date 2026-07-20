"""Reproducible local comparison against pytest and pytest-xdist."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class Measurement:
    command: str
    samples: tuple[float, ...]

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


def _display_command(command: list[str]) -> str:
    parts = []
    for value in command:
        if value == sys.executable:
            parts.append("python")
        elif "testenix-benchmark-" in value:
            parts.append("<suite>")
        else:
            parts.append(value)
    return " ".join(parts)


def _generate_suite(directory: Path, count: int, uneven: bool, module_count: int) -> None:
    modules = [["import time", ""] for _ in range(module_count)]
    for index in range(count):
        lines = modules[index % module_count]
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


def _benchmark_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONHASHSEED"] = "0"
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
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
) -> float:
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env=environment,
        cwd=working_directory,
    )
    elapsed = time.perf_counter() - started
    _validate_completed_run(name, command, completed, test_count)
    return elapsed


def _measure_commands(
    commands: dict[str, list[str]],
    *,
    repeats: int,
    warmups: int,
    test_count: int,
    working_directory: Path,
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
            )

    samples: dict[str, list[float]] = {name: [] for name in names}
    orders: list[tuple[str, ...]] = []
    for round_index in range(repeats):
        shift = round_index % len(names)
        order = (*names[shift:], *names[:shift])
        orders.append(order)
        for name in order:
            samples[name].append(
                _run_once(
                    name,
                    commands[name],
                    environment=environment,
                    test_count=test_count,
                    working_directory=working_directory,
                )
            )

    return (
        {
            name: Measurement(_display_command(commands[name]), tuple(samples[name]))
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
    workers: int,
    uneven: bool,
    module_count: int | None = None,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="testenix-benchmark-") as temporary:
        suite = Path(temporary)
        resolved_module_count = min(
            test_count,
            module_count if module_count is not None else max(1, workers * 4),
        )
        _generate_suite(suite, test_count, uneven, resolved_module_count)
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
                str(workers),
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
                "--no-history",
            ],
        }
        measurements, execution_orders = _measure_commands(
            commands,
            repeats=repeats,
            warmups=warmups,
            test_count=test_count,
            working_directory=suite,
        )

    return {
        "schema_version": 1,
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
            "uneven": uneven,
            "warmups": warmups,
            "workers": workers,
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
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument(
        "--modules",
        type=int,
        default=None,
        help="generated module count (default: workers * 4)",
    )
    parser.add_argument("--uneven", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    if arguments.tests < 1 or arguments.repeats < 1 or arguments.warmups < 0:
        raise SystemExit("tests/repeats must be positive and warmups cannot be negative")
    if arguments.workers < 1:
        raise SystemExit("workers must be positive")
    if arguments.modules is not None and arguments.modules < 1:
        raise SystemExit("modules must be positive")
    result = run_benchmark(
        test_count=arguments.tests,
        repeats=arguments.repeats,
        warmups=arguments.warmups,
        workers=arguments.workers,
        uneven=arguments.uneven,
        module_count=arguments.modules,
    )
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
