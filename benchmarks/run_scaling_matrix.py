"""Run a provenance-gated Testenix scaling matrix without publishing unmeasured claims.

The default design uses dimension sweeps rather than an expensive Cartesian product:

* 100, 500, 1,000, and 3,000 tests at the reference configuration;
* 1, 2, 4, and ``auto`` workers at the largest test count;
* balanced, one-dominant-module, and single-module layouts at the largest count;
* default history and ``--no-history`` at the largest count.
* explicit safe-module sharding for every layout at the largest count.

Pass ``--full-cross-product`` when every combination is required.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeAlias

if __package__:
    from benchmarks.run_benchmark import run_benchmark
else:  # direct ``python benchmarks/run_scaling_matrix.py`` execution
    from run_benchmark import run_benchmark

ModuleLayout: TypeAlias = Literal["balanced", "dominant", "single"]
HistoryMode: TypeAlias = Literal["disabled", "default"]
WorkerRequest: TypeAlias = int | Literal["auto"]
ShardingMode: TypeAlias = Literal["disabled", "safe"]

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COUNTS = (100, 500, 1_000, 3_000)
DEFAULT_WORKERS: tuple[WorkerRequest, ...] = (1, 2, 4, "auto")
DEFAULT_LAYOUTS: tuple[ModuleLayout, ...] = ("balanced", "dominant", "single")
DEFAULT_HISTORIES: tuple[HistoryMode, ...] = ("disabled", "default")
DEFAULT_SHARDING_MODES: tuple[ShardingMode, ...] = ("disabled", "safe")


@dataclass(frozen=True, slots=True)
class MatrixScenario:
    id: str
    test_count: int
    module_count: int
    workers: WorkerRequest
    module_layout: ModuleLayout
    history_mode: HistoryMode
    sharding_mode: ShardingMode = "disabled"
    uneven: bool = False
    dominant_fraction: float = 0.5


def _project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as source:
        return str(tomllib.load(source)["project"]["version"])


def _git_value(*arguments: str) -> str | None:
    import subprocess

    completed = subprocess.run(
        ("git", *arguments),
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _provenance(*, allow_dirty: bool) -> dict[str, Any]:
    status = _git_value("status", "--porcelain")
    if status is None:
        raise RuntimeError("cannot read the Testenix Git worktree state")
    dirty = bool(status)
    if dirty and not allow_dirty:
        raise RuntimeError(
            "refusing to record a publishable matrix from a dirty worktree; "
            "commit/stash changes or use --allow-dirty for an unpublished smoke run"
        )

    expected_version = _project_version()
    try:
        installed_version = importlib.metadata.version("testenix")
    except importlib.metadata.PackageNotFoundError as error:
        raise RuntimeError("Testenix must be installed in the benchmark environment") from error
    if installed_version != expected_version:
        raise RuntimeError(
            f"installed Testenix {installed_version} does not match pyproject {expected_version}"
        )
    return {
        "commit": _git_value("rev-parse", "HEAD"),
        "dirty": dirty,
        "testenix_version": installed_version,
        "pyproject_version": expected_version,
    }


def _deduplicate(scenarios: list[MatrixScenario]) -> tuple[MatrixScenario, ...]:
    seen: set[tuple[object, ...]] = set()
    unique: list[MatrixScenario] = []
    for scenario in scenarios:
        key = (
            scenario.test_count,
            scenario.module_count,
            scenario.workers,
            scenario.module_layout,
            scenario.history_mode,
            scenario.sharding_mode,
            scenario.uneven,
            scenario.dominant_fraction,
        )
        if key not in seen:
            seen.add(key)
            unique.append(scenario)
    return tuple(unique)


def _reference_worker(workers: tuple[WorkerRequest, ...]) -> WorkerRequest:
    return "auto" if "auto" in workers else (4 if 4 in workers else workers[0])


def build_scenarios(
    *,
    counts: tuple[int, ...],
    module_count: int,
    workers: tuple[WorkerRequest, ...],
    layouts: tuple[ModuleLayout, ...],
    histories: tuple[HistoryMode, ...],
    sharding_modes: tuple[ShardingMode, ...],
    dominant_fraction: float,
    full_cross_product: bool,
    include_duration_skew: bool,
) -> tuple[MatrixScenario, ...]:
    largest = max(counts)
    if full_cross_product:
        scenarios = [
            MatrixScenario(
                id=(
                    f"tests-{count}-layout-{layout}-workers-{worker}-history-{history}"
                    f"-sharding-{sharding}"
                ),
                test_count=count,
                module_count=1 if layout == "single" else min(module_count, count),
                workers=worker,
                module_layout=layout,
                history_mode=history,
                sharding_mode=sharding,
                dominant_fraction=dominant_fraction,
            )
            for count in counts
            for layout in layouts
            for worker in workers
            for history in histories
            for sharding in sharding_modes
        ]
        return _deduplicate(scenarios)

    reference_workers = _reference_worker(workers)
    scenarios = [
        MatrixScenario(
            id=f"scale-{count}",
            test_count=count,
            module_count=min(module_count, count),
            workers=reference_workers,
            module_layout="balanced",
            history_mode="disabled",
            sharding_mode="disabled",
        )
        for count in counts
    ]
    scenarios.extend(
        MatrixScenario(
            id=f"workers-{worker}",
            test_count=largest,
            module_count=min(module_count, largest),
            workers=worker,
            module_layout="balanced",
            history_mode="disabled",
            sharding_mode="disabled",
        )
        for worker in workers
    )
    scenarios.extend(
        MatrixScenario(
            id=f"layout-{layout}",
            test_count=largest,
            module_count=1 if layout == "single" else min(module_count, largest),
            workers=reference_workers,
            module_layout=layout,
            history_mode="disabled",
            sharding_mode="disabled",
            dominant_fraction=dominant_fraction,
        )
        for layout in layouts
    )
    scenarios.extend(
        MatrixScenario(
            id=f"history-{history}",
            test_count=largest,
            module_count=min(module_count, largest),
            workers=reference_workers,
            module_layout="balanced",
            history_mode=history,
            sharding_mode="disabled",
        )
        for history in histories
    )
    if "safe" in sharding_modes:
        scenarios.extend(
            MatrixScenario(
                id=f"sharding-safe-layout-{layout}",
                test_count=largest,
                module_count=1 if layout == "single" else min(module_count, largest),
                workers=reference_workers,
                module_layout=layout,
                history_mode="disabled",
                sharding_mode="safe",
                dominant_fraction=dominant_fraction,
            )
            for layout in layouts
        )
    if include_duration_skew:
        scenarios.append(
            MatrixScenario(
                id="duration-skew",
                test_count=largest,
                module_count=min(module_count, largest),
                workers=reference_workers,
                module_layout="balanced",
                history_mode="disabled",
                uneven=True,
            )
        )
    return _deduplicate(scenarios)


def _reference_curve(
    results: list[dict[str, Any]],
    *,
    reference_workers: WorkerRequest,
) -> list[dict[str, Any]]:
    """Project one canonical balanced/no-history point for every test count."""

    points = (
        {
            "test_count": entry["scenario"].test_count,
            "workers_requested": entry["scenario"].workers,
            "history_mode": entry["scenario"].history_mode,
            "sharding_mode": entry["scenario"].sharding_mode,
            "measurements": {
                name: {
                    "median_seconds": measurement["median"],
                    "median_tests_per_second": measurement["median_tests_per_second"],
                    "observed_workers": measurement["observed_workers"],
                }
                for name, measurement in entry["result"]["measurements"].items()
            },
        }
        for entry in results
        if entry["scenario"].workers == reference_workers
        and entry["scenario"].module_layout == "balanced"
        and entry["scenario"].history_mode == "disabled"
        and entry["scenario"].sharding_mode == "disabled"
        and not entry["scenario"].uneven
    )
    curve = sorted(points, key=lambda point: point["test_count"])
    counts = [point["test_count"] for point in curve]
    if len(counts) != len(set(counts)):
        raise RuntimeError("canonical reference curve contains duplicate test counts")
    return curve


def _parse_positive_csv(value: str) -> tuple[int, ...]:
    try:
        parsed = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected comma-separated positive integers") from error
    if not parsed or any(item < 1 for item in parsed):
        raise argparse.ArgumentTypeError("expected comma-separated positive integers")
    return parsed


def _parse_workers(value: str) -> tuple[WorkerRequest, ...]:
    parsed: list[WorkerRequest] = []
    for raw in value.split(","):
        item = raw.strip()
        if not item:
            continue
        if item == "auto":
            parsed.append("auto")
            continue
        try:
            number = int(item)
        except ValueError as error:
            raise argparse.ArgumentTypeError("workers must be integers or 'auto'") from error
        if number < 1:
            raise argparse.ArgumentTypeError("workers must be positive")
        parsed.append(number)
    if not parsed:
        raise argparse.ArgumentTypeError("at least one worker value is required")
    return tuple(parsed)


def _validate_coverage(
    scenarios: tuple[MatrixScenario, ...],
    *,
    counts: tuple[int, ...],
    workers: tuple[WorkerRequest, ...],
    layouts: tuple[ModuleLayout, ...],
    histories: tuple[HistoryMode, ...],
    sharding_modes: tuple[ShardingMode, ...],
) -> None:
    if not set(counts).issubset({scenario.test_count for scenario in scenarios}):
        raise RuntimeError("matrix does not cover every requested test count")
    if not set(workers).issubset({scenario.workers for scenario in scenarios}):
        raise RuntimeError("matrix does not cover every requested worker value")
    if not set(layouts).issubset({scenario.module_layout for scenario in scenarios}):
        raise RuntimeError("matrix does not cover every requested module layout")
    if not set(histories).issubset({scenario.history_mode for scenario in scenarios}):
        raise RuntimeError("matrix does not cover both history modes")
    if not set(sharding_modes).issubset({scenario.sharding_mode for scenario in scenarios}):
        raise RuntimeError("matrix does not cover every requested sharding mode")
    if "safe" in sharding_modes:
        missing_safe_layouts = {
            layout
            for layout in layouts
            if not any(
                scenario.module_layout == layout and scenario.sharding_mode == "safe"
                for scenario in scenarios
            )
        }
        if missing_safe_layouts:
            raise RuntimeError(
                "safe sharding is missing layouts: " + ", ".join(sorted(missing_safe_layouts))
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tests", type=_parse_positive_csv, default=DEFAULT_COUNTS)
    parser.add_argument("--modules", type=int, default=16)
    parser.add_argument("--workers", type=_parse_workers, default=DEFAULT_WORKERS)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--dominant-fraction", type=float, default=0.5)
    parser.add_argument("--full-cross-product", action="store_true")
    parser.add_argument("--include-duration-skew", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="unpublished smoke mode: one measured round and no warm-up",
    )
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    if arguments.modules < 1:
        raise SystemExit("modules must be positive")
    if arguments.repeats < 1 or arguments.warmups < 0:
        raise SystemExit("repeats must be positive and warmups cannot be negative")
    if not 0.0 < arguments.dominant_fraction < 1.0:
        raise SystemExit("dominant-fraction must be greater than zero and less than one")

    provenance = _provenance(allow_dirty=arguments.allow_dirty)
    repeats = 1 if arguments.quick else arguments.repeats
    warmups = 0 if arguments.quick else arguments.warmups
    scenarios = build_scenarios(
        counts=arguments.tests,
        module_count=arguments.modules,
        workers=arguments.workers,
        layouts=DEFAULT_LAYOUTS,
        histories=DEFAULT_HISTORIES,
        sharding_modes=DEFAULT_SHARDING_MODES,
        dominant_fraction=arguments.dominant_fraction,
        full_cross_product=arguments.full_cross_product,
        include_duration_skew=arguments.include_duration_skew,
    )
    _validate_coverage(
        scenarios,
        counts=arguments.tests,
        workers=arguments.workers,
        layouts=DEFAULT_LAYOUTS,
        histories=DEFAULT_HISTORIES,
        sharding_modes=DEFAULT_SHARDING_MODES,
    )

    results: list[dict[str, Any]] = []
    for index, scenario in enumerate(scenarios, start=1):
        print(f"[{index}/{len(scenarios)}] {scenario.id}", flush=True)
        result = run_benchmark(
            test_count=scenario.test_count,
            repeats=repeats,
            warmups=warmups,
            workers=scenario.workers,
            uneven=scenario.uneven,
            module_count=scenario.module_count,
            module_layout=scenario.module_layout,
            dominant_fraction=scenario.dominant_fraction,
            history_mode=scenario.history_mode,
            xdist_strategy="load",
            sharding_mode=scenario.sharding_mode,
        )
        results.append({"id": scenario.id, "scenario": scenario, "result": result})

    reference_curve = _reference_curve(
        results,
        reference_workers=_reference_worker(arguments.workers),
    )
    serialized_results = [{"id": entry["id"], "result": entry["result"]} for entry in results]
    publication_eligible = (
        not provenance["dirty"] and not arguments.quick and repeats >= 5 and warmups >= 1
    )

    output = {
        "schema_version": 1,
        "kind": "testenix.scaling-matrix",
        "recorded_at": datetime.now(UTC).isoformat(),
        "provenance": provenance,
        "design": {
            "mode": "full-cross-product" if arguments.full_cross_product else "dimension-sweeps",
            "tests": arguments.tests,
            "workers": arguments.workers,
            "module_layouts": DEFAULT_LAYOUTS,
            "history_modes": DEFAULT_HISTORIES,
            "sharding_modes": DEFAULT_SHARDING_MODES,
            "modules": arguments.modules,
            "dominant_fraction": arguments.dominant_fraction,
            "repeats": repeats,
            "warmups": warmups,
            "xdist_strategy": "load",
            "testenix_auto_semantics": "adaptive",
            "xdist_auto_workers": os.cpu_count() or 1,
            "publication_eligible": publication_eligible,
            "publication_contract": "clean commit, >=5 measured rounds, >=1 warm-up",
        },
        "reference_curve": reference_curve,
        "scenarios": serialized_results,
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
