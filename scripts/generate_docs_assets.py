#!/usr/bin/env python3
"""Generate benchmark and LLM documentation assets from canonical repository data."""

from __future__ import annotations

import argparse
import inspect
import json
import math
import posixpath
import re
import sys
import tomllib
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from html import escape
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SITE_URL = "https://polishdataengineer.github.io/testenix/"
REPOSITORY_URL = "https://github.com/polishdataengineer/testenix"

BASELINES = (
    ROOT / "benchmarks" / "baseline.json",
    ROOT / "benchmarks" / "baseline_uneven.json",
    ROOT / "benchmarks" / "baseline_100k.json",
)

MIGRATION_BASELINES = (
    ROOT / "benchmarks" / "migration_baseline_pytest_3000.json",
    ROOT / "benchmarks" / "migration_baseline_unittest_3000.json",
    ROOT / "benchmarks" / "migration_baseline_unittest_3000_delay_1ms.json",
)

SCALING_MATRIX = ROOT / "benchmarks" / "scaling_matrix_0_3_0.json"

LLM_DOCUMENTS = (
    ("Overview", Path("docs/index.md"), ""),
    ("Getting started", Path("docs/getting-started.md"), "getting-started/"),
    (
        "Pytest compatibility",
        Path("docs/guides/pytest-compatibility.md"),
        "guides/pytest-compatibility/",
    ),
    ("Safe migration", Path("docs/guides/migration.md"), "guides/migration/"),
    ("Writing tests", Path("docs/guides/writing-tests.md"), "guides/writing-tests/"),
    ("Fixtures", Path("docs/guides/fixtures.md"), "guides/fixtures/"),
    ("Parallel execution", Path("docs/guides/parallelism.md"), "guides/parallelism/"),
    ("Reports and history", Path("docs/guides/reports.md"), "guides/reports/"),
    ("CLI reference", Path("docs/reference/cli.md"), "reference/cli/"),
    (
        "Configuration reference",
        Path("docs/reference/configuration.md"),
        "reference/configuration/",
    ),
    ("Python API reference", Path("docs/reference/api.md"), "reference/api/"),
    ("Benchmark results", Path("docs/benchmarks/results.md"), "benchmarks/results/"),
    ("Benchmarking contract", Path("docs/benchmarking.md"), "benchmarking/"),
    ("Performance analysis", Path("docs/performance-analysis.md"), "performance-analysis/"),
    ("Architecture", Path("docs/architecture.md"), "architecture/"),
    ("Roadmap", Path("docs/roadmap.md"), "roadmap/"),
    ("Changelog", Path("CHANGELOG.md"), f"{REPOSITORY_URL}/blob/main/CHANGELOG.md"),
    ("Security policy", Path("SECURITY.md"), f"{REPOSITORY_URL}/blob/main/SECURITY.md"),
)


@dataclass(frozen=True, slots=True)
class Benchmark:
    source: Path
    test_count: int
    module_count: int
    workers: int
    uneven: bool
    repeats: int
    warmups: int
    environment: dict[str, Any]
    measurements: dict[str, dict[str, Any]]
    legacy_runner_id: bool
    provenance: dict[str, Any]
    recorded_at: str | None

    @property
    def label(self) -> str:
        workload = "uneven-duration" if self.uneven else "no-op"
        return f"{self.test_count:,} {workload} tests / {self.module_count:,} modules"

    @property
    def short_label(self) -> str:
        count = f"{self.test_count // 1000}k" if self.test_count >= 1000 else str(self.test_count)
        workload = "uneven" if self.uneven else "no-op"
        return f"{count} {workload}"

    @property
    def testenix(self) -> dict[str, Any]:
        return self.measurements["testenix"]

    @property
    def speedup_vs_pytest(self) -> float:
        return float(self.measurements["pytest"]["median"]) / float(self.testenix["median"])

    @property
    def speedup_vs_xdist(self) -> float:
        return float(self.measurements["pytest_xdist"]["median"]) / float(self.testenix["median"])


@dataclass(frozen=True, slots=True)
class MigrationBenchmark:
    source: Path
    framework: str
    test_count: int
    module_count: int
    workers: int
    delay_ms: float
    repeats: int
    warmups: int
    environment: dict[str, Any]
    source_measurement: dict[str, Any]
    native_measurement: dict[str, Any]
    migration_seconds: float
    originals_modified: bool
    provenance: dict[str, Any]
    recorded_at: str

    @property
    def ratio(self) -> float:
        return float(self.source_measurement["median_seconds"]) / float(
            self.native_measurement["median_seconds"]
        )

    @property
    def workload(self) -> str:
        return "no-op" if self.delay_ms == 0 else f"{self.delay_ms:g} ms body"


def _project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as source:
        return str(tomllib.load(source)["project"]["version"])


def _historical_version(benchmarks: tuple[Benchmark, ...]) -> str:
    versions = {
        str(benchmark.provenance.get("versions", {}).get("testenix", "unknown"))
        for benchmark in benchmarks
    }
    if len(versions) != 1:
        raise ValueError("published historical baselines do not share one Testenix version")
    return versions.pop()


def _load_scaling_matrix(path: Path, *, expected_version: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    provenance = data.get("provenance", {})
    design = data.get("design", {})
    scenarios = data.get("scenarios")
    if (
        data.get("schema_version") != 1
        or data.get("kind") != "testenix.scaling-matrix"
        or provenance.get("dirty") is not False
        or provenance.get("testenix_version") != expected_version
        or provenance.get("pyproject_version") != expected_version
        or not provenance.get("commit")
        or not isinstance(scenarios, list)
        or not scenarios
        or int(design.get("repeats", 0)) < 5
        or int(design.get("warmups", 0)) < 1
    ):
        raise ValueError(f"{path}: current-version scaling publication gates did not pass")

    counts: set[int] = set()
    workers: set[str] = set()
    layouts: set[str] = set()
    histories: set[str] = set()
    sharding_modes: set[str] = set()
    for entry in scenarios:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise ValueError(f"{path}: invalid scaling scenario entry")
        result = entry.get("result")
        if not isinstance(result, dict) or result.get("schema_version") != 2:
            raise ValueError(f"{path}: invalid scaling scenario result")
        scenario = result.get("scenario", {})
        measurements = result.get("measurements", {})
        result_provenance = result.get("provenance", {})
        if (
            result_provenance.get("dirty") is not False
            or result_provenance.get("commit") != provenance["commit"]
            or result_provenance.get("versions", {}).get("testenix") != expected_version
            or int(scenario.get("repeats", 0)) != int(design["repeats"])
            or int(scenario.get("warmups", -1)) != int(design["warmups"])
            or scenario.get("xdist_strategy") != design.get("xdist_strategy")
        ):
            raise ValueError(f"{path}: scenario provenance/design mismatch in {entry['id']}")
        counts.add(int(scenario["test_count"]))
        workers.add(str(scenario["workers_requested"]))
        layouts.add(str(scenario["module_layout"]))
        histories.add(str(scenario["history_mode"]))
        sharding_modes.add(str(scenario.get("sharding_mode", "disabled")))
        for runner in ("pytest", "pytest_xdist", "testenix"):
            measurement = measurements.get(runner, {})
            samples = measurement.get("samples")
            stdout_bytes = measurement.get("stdout_bytes")
            stderr_bytes = measurement.get("stderr_bytes")
            if (
                not isinstance(samples, list)
                or len(samples) != int(scenario["repeats"])
                or any(float(sample) <= 0 for sample in samples)
                or not isinstance(stdout_bytes, list)
                or len(stdout_bytes) != len(samples)
                or not isinstance(stderr_bytes, list)
                or len(stderr_bytes) != len(samples)
            ):
                raise ValueError(f"{path}: invalid {runner} samples in {entry['id']}")
        if scenario["workers_requested"] == "auto":
            observed_workers = measurements["testenix"].get("observed_workers")
            if (
                not isinstance(observed_workers, list)
                or len(observed_workers) != int(scenario["repeats"])
                or any(
                    isinstance(worker, bool) or not isinstance(worker, int) or worker < 1
                    for worker in observed_workers
                )
            ):
                raise ValueError(
                    f"{path}: auto scenario {entry['id']} has no valid observed worker counts"
                )
    if not {100, 500, 1_000, 3_000}.issubset(counts):
        raise ValueError(f"{path}: scaling counts are incomplete")
    if not {"1", "2", "4", "auto"}.issubset(workers):
        raise ValueError(f"{path}: worker coverage is incomplete")
    if not {"balanced", "dominant", "single"}.issubset(layouts):
        raise ValueError(f"{path}: module-layout coverage is incomplete")
    if not {"disabled", "default"}.issubset(histories):
        raise ValueError(f"{path}: history coverage is incomplete")
    if not {"disabled", "safe"}.issubset(sharding_modes):
        raise ValueError(f"{path}: safe-sharding coverage is incomplete")
    return data


def _load_benchmark(path: Path) -> Benchmark:
    data = json.loads(path.read_text(encoding="utf-8"))
    scenario = data["scenario"]
    raw_measurements = data["measurements"]
    legacy = "ptf" in raw_measurements
    testenix = raw_measurements.get("testenix", raw_measurements.get("ptf"))
    if testenix is None:
        raise ValueError(f"{path} has no Testenix measurement")

    measurements = {
        "testenix": testenix,
        "pytest": raw_measurements["pytest"],
        "pytest_xdist": raw_measurements["pytest_xdist"],
    }
    for runner, measurement in measurements.items():
        samples = measurement.get("samples")
        if not isinstance(samples, list) or len(samples) != scenario["repeats"]:
            raise ValueError(f"{path}: invalid sample count for {runner}")
        if any(float(sample) <= 0 for sample in samples):
            raise ValueError(f"{path}: non-positive duration for {runner}")

    return Benchmark(
        source=path,
        test_count=int(scenario["test_count"]),
        module_count=int(scenario["test_modules"]),
        workers=int(scenario["workers"]),
        uneven=bool(scenario["uneven"]),
        repeats=int(scenario["repeats"]),
        warmups=int(scenario["warmups"]),
        environment=dict(data["environment"]),
        measurements=measurements,
        legacy_runner_id=legacy,
        provenance=dict(data.get("provenance", {})),
        recorded_at=data.get("recorded_at"),
    )


def _load_migration_benchmark(path: Path) -> MigrationBenchmark:
    data = json.loads(path.read_text(encoding="utf-8"))
    scenario = data["scenario"]
    measurements = data["measurements"]
    framework = str(scenario["framework"])
    source_name = f"source_{framework}"
    source_measurement = dict(measurements[source_name])
    native_measurement = dict(measurements["testenix_native"])
    repeats = int(scenario["repeats"])
    for name, measurement in (
        (source_name, source_measurement),
        ("testenix_native", native_measurement),
    ):
        samples = measurement.get("samples_seconds")
        if not isinstance(samples, list) or len(samples) != repeats:
            raise ValueError(f"{path}: invalid sample count for {name}")
        if any(float(sample) <= 0 for sample in samples):
            raise ValueError(f"{path}: non-positive duration for {name}")
    migration = data["migration"]
    migration_report = migration["report"]
    provenance = dict(data.get("provenance", {}))
    test_count = int(scenario["generated_test_count"])
    if (
        data.get("originals_modified") is not False
        or migration_report.get("originals_modified") is not False
        or migration_report.get("status") != "published"
        or migration_report.get("converted_tests") != test_count
    ):
        raise ValueError(f"{path}: migration integrity gates did not pass")
    if provenance.get("dirty") is not False or not provenance.get("commit"):
        raise ValueError(f"{path}: migration baseline must come from a clean source commit")
    recorded_at = data.get("recorded_at")
    if not isinstance(recorded_at, str) or not recorded_at:
        raise ValueError(f"{path}: migration baseline has no recording timestamp")
    return MigrationBenchmark(
        source=path,
        framework=framework,
        test_count=test_count,
        module_count=int(scenario["generated_module_count"]),
        workers=int(scenario["workers"]),
        delay_ms=float(scenario.get("delay_ms", 0.0)),
        repeats=repeats,
        warmups=int(scenario["warmups"]),
        environment=dict(data["environment"]),
        source_measurement=source_measurement,
        native_measurement=native_measurement,
        migration_seconds=float(migration["wall_seconds"]),
        originals_modified=False,
        provenance=provenance,
        recorded_at=recorded_at,
    )


def _seconds(value: Any) -> str:
    return f"{float(value):.3f} s"


def _raw_link(benchmark: Benchmark | MigrationBenchmark) -> str:
    relative = benchmark.source.relative_to(ROOT).as_posix()
    return f"{REPOSITORY_URL}/blob/main/{relative}"


def _migration_comparison(benchmark: MigrationBenchmark) -> str:
    if benchmark.ratio >= 1:
        return f"{benchmark.ratio:.2f}× faster"
    return f"{1 / benchmark.ratio:.2f}× slower"


def _render_migration_results(benchmarks: tuple[MigrationBenchmark, ...]) -> str:
    rows = []
    detail_sections = []
    for benchmark in benchmarks:
        source = benchmark.source_measurement
        native = benchmark.native_measurement
        source_name = (
            "pytest (sequential)"
            if benchmark.framework == "pytest"
            else ("unittest outcome probe (sequential)")
        )
        rows.append(
            "| "
            + " | ".join(
                (
                    source_name,
                    benchmark.workload,
                    f"{benchmark.test_count:,} / {benchmark.module_count:,}",
                    _seconds(source["median_seconds"]),
                    _seconds(native["median_seconds"]),
                    _migration_comparison(benchmark),
                    _seconds(benchmark.migration_seconds),
                )
            )
            + " |"
        )
        source_samples = ", ".join(f"{float(value):.3f}" for value in source["samples_seconds"])
        native_samples = ", ".join(f"{float(value):.3f}" for value in native["samples_seconds"])
        native_range = (
            f"{_seconds(native['minimum_seconds'])}–{_seconds(native['maximum_seconds'])}"
        )
        versions = ", ".join(
            f"{name}={value}"
            for name, value in sorted(benchmark.provenance.get("versions", {}).items())
        )
        environment_fields = ", ".join(
            f"{name}={value}" for name, value in sorted(benchmark.environment.items())
        )
        commit = str(benchmark.provenance["commit"])
        detail_sections.append(
            f"""### {benchmark.framework} / {benchmark.workload}

- Source command: `{source["command"]}`
- Native command: `{native["command"]}`
- Source median: {_seconds(source["median_seconds"])}
- Source range: {_seconds(source["minimum_seconds"])}–{_seconds(source["maximum_seconds"])};
  standard deviation: {_seconds(source["stdev_seconds"])}
- Source raw samples: {source_samples} seconds
- Native Testenix median: {_seconds(native["median_seconds"])}
- Native Testenix range: {native_range};
  standard deviation: {_seconds(native["stdev_seconds"])}
- Native Testenix raw samples: {native_samples} seconds
- Native workers: {benchmark.workers}
- Measured rounds: {benchmark.repeats}; warmups: {benchmark.warmups}
- One-time copy, validation, and publication transaction: {_seconds(benchmark.migration_seconds)}
- Integrity gates: {benchmark.test_count:,} converted tests, matching source/native outcomes,
  original SHA-256 values unchanged
- Recorded at: `{benchmark.recorded_at}`
- Source commit: [`{commit}`]({REPOSITORY_URL}/commit/{commit}); worktree clean
- Lock SHA-256: `{benchmark.provenance.get("lock_sha256", "unknown")}`
- Versions: {versions}
- Environment: {environment_fields}
- [Raw JSON]({_raw_link(benchmark)})
"""
        )

    environment = benchmarks[0].environment
    if any(benchmark.environment != environment for benchmark in benchmarks[1:]):
        raise ValueError("published migration baselines do not share one environment")
    table_header = (
        "| Source runner | Workload | Tests / modules | Source median | Native median "
        "| Native vs source | Migration transaction |"
    )

    return f"""## Migrated-suite measurements

These separate measurements start with generated pytest or unittest sources, complete one safe
copy-and-validate migration, and then compare recurring source-suite runs with recurring native
Testenix runs. The migration transaction is a one-time cost shown separately; it is not included
in either execution median. These records came from the pre-v0.2 source commit linked below; its
distribution metadata still reported `0.1.0`. They are historical evidence, not measurements of
the current release.

{table_header}
| --- | --- | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(rows)}

The native side used four workers. The source pytest and unittest outcome-probe baselines were
sequential, so these rows do not compare Testenix with pytest-xdist or another parallel unittest
runner. The unittest probe uses the standard-library loader and result semantics, then serializes
per-test outcomes for parity checking; its timing therefore includes that small audit overhead.
The no-op unittest wrappers are 7.40× slower than the probe because wrapper, loading, and
result-adaptation costs dominate an empty body. With 1 ms of synthetic work per unittest method,
parallel native execution is 1.58× faster in this 64-module layout. Module count and duration are
therefore material, and none of these synthetic rows predicts a specific real project.

### Raw migration samples and variance

{chr(10).join(detail_sections)}
"""


def _render_current_matrix(matrix: dict[str, Any] | None, *, current_version: str) -> str:
    if matrix is None:
        matrix_section = f"""## Testenix {current_version} scaling matrix

No current-version matrix is checked in yet. The historical results below must therefore not be
described as Testenix {current_version} performance. The new provenance-gated harness covers
100/500/1,000/3,000 tests, balanced/dominant/single-module layouts, 1/2/4/auto workers, and both
default history and `--no-history`, plus explicit safe-module sharding. Its default design uses
dimension sweeps; use
`--full-cross-product` only when the much larger run is intentional.

`auto` is passed literally to Testenix and remains adaptive; observed Testenix worker counts are
stored per sample. pytest-xdist resolves its side of an `auto` row separately to the machine's
logical CPU count.

```console
$ uv run --no-editable python benchmarks/run_scaling_matrix.py \\
    --output benchmarks/scaling_matrix_0_3_0.json
```

The command refuses a dirty worktree or an installed Testenix version that differs from
`pyproject.toml`. `--allow-dirty` is available only for unpublished smoke runs. A matrix becomes
publishable here only after five measured rounds, one warm-up, clean commit provenance, and full
axis coverage pass the documentation generator's validation.
"""
    else:
        rows: list[str] = []
        for entry in matrix["scenarios"]:
            result = entry["result"]
            scenario = result["scenario"]
            measurements = result["measurements"]
            native = float(measurements["testenix"]["median"])
            pytest = float(measurements["pytest"]["median"])
            xdist = float(measurements["pytest_xdist"]["median"])
            history = "default" if scenario["history_mode"] == "default" else "disabled"
            sharding = str(scenario.get("sharding_mode", "disabled"))
            workers = str(scenario["workers_requested"])
            if workers == "auto":
                observed = sorted(set(measurements["testenix"]["observed_workers"]))
                workers = f"auto ({'/'.join(str(worker) for worker in observed)} observed)"
            rows.append(
                "| "
                + " | ".join(
                    (
                        str(entry["id"]),
                        f"{int(scenario['test_count']):,}",
                        f"{int(scenario['test_modules']):,}",
                        str(scenario["module_layout"]),
                        workers,
                        history,
                        sharding,
                        _seconds(native),
                        _seconds(pytest),
                        _seconds(xdist),
                        f"{pytest / native:.2f}×",
                    )
                )
                + " |"
            )
        commit = str(matrix["provenance"]["commit"])
        matrix_section = f"""## Testenix {current_version} scaling matrix

This current-version matrix passed the clean-worktree, version, sample-count, and axis-coverage
publication gates. Ratios still apply only to the recorded environment and exact row.

| Scenario | Tests | Mods | Layout | Workers | History | Shard | Native | pytest | xdist | ratio |
| --- | ---: | ---: | --- | ---: | --- | --- | ---: | ---: | ---: | ---: |
{chr(10).join(rows)}

- Measured rounds: {matrix["design"]["repeats"]}; warmups: {matrix["design"]["warmups"]}
- pytest-xdist strategy: `{matrix["design"]["xdist_strategy"]}`
- Clean source commit: [`{commit}`]({REPOSITORY_URL}/commit/{commit})
- [Raw JSON]({_raw_link_path(SCALING_MATRIX)})
"""

    return (
        matrix_section
        + """

## Real-project harness

The 118-test project used during v0.2 migration validation was a semantic parity gate, not a
publishable benchmark: its release-note timings were single observations without a committed
multi-round record. Use the redaction-safe manifest harness for a real repository:

```console
$ cp benchmarks/real_project_manifest.example.json /tmp/testenix-project-benchmark.json
$ uv run --no-editable python benchmarks/run_project_benchmark.py \\
    --project /absolute/path/to/project \\
    --manifest /tmp/testenix-project-benchmark.json \\
    --output /tmp/testenix-project-result.json
```

The manifest stores argument arrays, never shell fragments. The result omits stdout, stderr,
environment values, absolute project paths, and private source. It records only timings, aggregate
output sizes, optional tree fingerprints, and redacted Git provenance. A migrated-suite comparison
must point the manifest at a successful migration report to become publication-eligible. The
harness verifies the report's exact per-test inventory and outcomes, complete source and generated
Python-file inventories, current hashes, and binds canonical `python -m pytest` /
`python -m testenix run` commands to the report's source/output roots. Publishable source roots are
directories so support files such as `conftest.py` are covered. Without the report the result is
diagnostic-only. Commands are retained for
reproducibility. Publishable commands put options before `--` and exact suite targets after it, so
an option value cannot impersonate a migration root. Keep secrets in the environment or list
sensitive argument indexes in `redact_arguments`.
"""
    )


def _raw_link_path(path: Path) -> str:
    relative = path.relative_to(ROOT).as_posix()
    return f"{REPOSITORY_URL}/blob/main/{relative}"


def _render_benchmark_results(
    benchmarks: tuple[Benchmark, ...],
    migration_benchmarks: tuple[MigrationBenchmark, ...],
    scaling_matrix: dict[str, Any] | None,
    *,
    current_version: str,
) -> str:
    historical_version = _historical_version(benchmarks)
    xdist_version = benchmarks[0].provenance.get("versions", {}).get("pytest_xdist", "unknown")
    rows = []
    detail_sections = []
    for benchmark in benchmarks:
        testenix = benchmark.testenix
        pytest = benchmark.measurements["pytest"]
        xdist = benchmark.measurements["pytest_xdist"]
        rows.append(
            "| "
            + " | ".join(
                (
                    benchmark.label,
                    _seconds(testenix["median"]),
                    _seconds(pytest["median"]),
                    _seconds(xdist["median"]),
                    f"{benchmark.speedup_vs_pytest:.2f}×",
                    f"{benchmark.speedup_vs_xdist:.2f}×",
                )
            )
            + " |"
        )
        runner_details: list[str] = []
        for display_name, measurement in (
            ("Testenix", testenix),
            ("pytest", pytest),
            ("pytest-xdist", xdist),
        ):
            samples = ", ".join(f"{float(value):.3f}" for value in measurement["samples"])
            runner_details.extend(
                (
                    f"- {display_name} range: {_seconds(measurement['minimum'])}–"
                    f"{_seconds(measurement['maximum'])}",
                    f"- {display_name} standard deviation: {_seconds(measurement['stdev'])}",
                    f"- {display_name} raw samples: {samples} seconds",
                )
            )
        provenance_details = []
        if benchmark.recorded_at:
            provenance_details.append(f"- Recorded at: `{benchmark.recorded_at}`")
        if commit := benchmark.provenance.get("commit"):
            provenance_details.append(f"- Commit: `{commit}`")
        if benchmark.provenance.get("dirty") is not None:
            clean = "yes" if not benchmark.provenance["dirty"] else "no"
            provenance_details.append(f"- Clean working tree at capture: {clean}")
        detail_sections.append(
            f"""### {benchmark.label}

{chr(10).join(runner_details)}
- Measured rounds: {benchmark.repeats}; warmups: {benchmark.warmups}
- Workers: {benchmark.workers}
- Testenix history: disabled with `--no-history`
- pytest-xdist strategy: default `load`
{chr(10).join(provenance_details)}
- [Raw JSON]({_raw_link(benchmark)})
"""
        )

    environment = benchmarks[0].environment
    if any(benchmark.environment != environment for benchmark in benchmarks[1:]):
        raise ValueError("published baseline files do not share one environment")
    cpu_model = str(environment.get("cpu_model") or "not recorded in this legacy baseline")

    largest = max(benchmarks, key=lambda item: item.test_count)
    if largest.repeats < 5 or largest.warmups < 1:
        publication_note = f"""
The {largest.test_count:,}-test result has only {largest.repeats} measured rounds and
{largest.warmups} warmups. It is published for transparency, but it does not yet satisfy the
project's five-run, one-warmup minimum for a broad promotional claim.
""".strip()
    else:
        publication_note = f"""
The {largest.test_count:,}-test result meets the project's local five-run, one-warmup minimum.
It remains a synthetic result from one machine, not a universal performance promise.
""".strip()

    legacy_note = ""
    if any(benchmark.legacy_runner_id for benchmark in benchmarks):
        legacy_note = """
The raw JSON files retain the pre-release `ptf` runner identifier because the measurements were
recorded before the project was named Testenix. The performance analysis documents that provenance;
future approved baselines must use the `testenix` identifier and record their commit SHA.
"""

    return (
        f"""# Published benchmark results

These tables are generated from the raw JSON committed in `benchmarks/`. They are development
evidence for specific synthetic workloads, not a universal claim that Testenix is always faster
than pytest. `Testenix` in these results means the native `testenix run` engine. The
`testenix pytest` compatibility bridge delegates to pytest and is not represented here.

{_render_current_matrix(scaling_matrix, current_version=current_version)}

## Historical Testenix {historical_version} synthetic baseline

The checked-in `3.15×` figure is a Testenix {historical_version} result for 100,000 generated no-op
tests across 16 modules, four workers, disabled history (`--no-history`), and pytest-xdist's default
`load` strategy. It is retained as transparent historical evidence; it is not a measurement of
Testenix {current_version}.

![Historical Testenix {historical_version} throughput ratios](../_static/benchmark-speedup.svg)

### Median wall-clock time

Lower time is better. A speedup of `{benchmarks[0].speedup_vs_pytest:.2f}×` means pytest's median
wall time was {benchmarks[0].speedup_vs_pytest:.2f} times the Testenix median for that exact
scenario.

| Scenario | Testenix | pytest | pytest-xdist | vs pytest | vs xdist |
| --- | ---: | ---: | ---: | ---: | ---: |
"""
        + "\n".join(rows)
        + f"""

<div class="benchmark-caveat">
{publication_note}
</div>

### Environment and controls

- CPU: {cpu_model} ({environment["cpu_count"]} logical CPUs)
- Machine: `{environment["machine"]}`
- Platform: `{environment["platform"]}`
- Python: `{environment["python"]}`
- Testenix: `{historical_version}`
- Workers: four for Testenix and pytest-xdist
- Testenix history: disabled with `--no-history`
- pytest-xdist: version `{xdist_version}`,
  default `load` distribution
- Measurement: complete subprocess wall-clock time, including discovery, execution, aggregation,
  and console rendering
- Correctness gate: every command had to exit successfully and report the expected test count

{legacy_note}

### Raw samples and variance

"""
        + "\n".join(detail_sections)
        + "\n"
        + _render_migration_results(migration_benchmarks)
        + """
## Interpretation

The historical checked-in results show that Testenix 0.1.0 had low per-test overhead for the large
generated suites above and was competitive with sequential pytest and pytest-xdist's default
`load` strategy in those scenarios. They are not evidence for the current release.

They do **not** yet answer how Testenix performs for import-heavy applications, complex fixture
graphs, assertion failures, real repositories, or different operating systems. Pytest also has a
far larger plugin and tooling ecosystem. Read the
[full performance analysis](../performance-analysis.md) for profiling details, memory notes,
implemented optimizations, and the Rust/PyO3 decision.

## Reproduce

Run the same harness from a locked development environment:

```console
$ uv sync --locked --dev --no-editable
$ uv run python benchmarks/run_benchmark.py --tests 10000 --workers 4 --repeats 5
$ uv run python benchmarks/run_benchmark.py --tests 10000 --workers 4 --repeats 5 --uneven
$ uv run python benchmarks/run_benchmark.py --tests 100000 --workers 4 --repeats 5
$ uv run python benchmarks/run_migration_benchmark.py --framework pytest --tests 3000 \\
    --modules 64 --workers 4 --warmups 1 --repeats 5 \\
    --output benchmarks/migration_baseline_pytest_3000.json
$ uv run python benchmarks/run_migration_benchmark.py --framework unittest --tests 3000 \\
    --modules 64 --workers 4 --warmups 1 --repeats 5 \\
    --output benchmarks/migration_baseline_unittest_3000.json
$ uv run python benchmarks/run_migration_benchmark.py --framework unittest --tests 3000 \\
    --modules 64 --workers 4 --delay-ms 1 --warmups 1 --repeats 5 \\
    --output benchmarks/migration_baseline_unittest_3000_delay_1ms.json
```

Review the [benchmarking contract](../benchmarking.md) before comparing or publishing new data.
"""
    )


def _render_benchmark_svg(benchmarks: tuple[Benchmark, ...]) -> str:
    historical_version = _historical_version(benchmarks)
    width = 980
    height = 130 + len(benchmarks) * 112
    plot_x = 285
    plot_width = 620
    maximum = max(max(item.speedup_vs_pytest, item.speedup_vs_xdist) for item in benchmarks)
    axis_max = max(3, math.ceil(maximum))
    scale = plot_width / axis_max
    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        'viewBox="0 0 980 '
        f'{height}" role="img" aria-labelledby="title description">',
        f'<title id="title">Historical Testenix {historical_version} '
        "benchmark throughput ratios</title>",
        '<desc id="description">Horizontal bars compare historical Testenix '
        f"{historical_version} throughput with pytest and pytest-xdist for three checked-in "
        "synthetic benchmark scenarios using four workers and disabled Testenix history.</desc>",
        "<style>"
        ".title{font:700 22px system-ui,sans-serif;fill:#0f172a}"
        ".subtitle,.tick,.legend{font:13px system-ui,sans-serif;fill:#475569}"
        ".label{font:600 14px system-ui,sans-serif;fill:#1e293b}"
        ".value{font:700 13px system-ui,sans-serif;fill:#0f172a}"
        ".grid{stroke:#cbd5e1;stroke-width:1}"
        "</style>",
        '<rect width="980" height="100%" rx="16" fill="#ffffff"/>',
        f'<text class="title" x="32" y="36">Historical Testenix {historical_version} '
        "synthetic ratios</text>",
        '<text class="subtitle" x="32" y="60">4 workers · --no-history · higher is better</text>',
    ]
    for tick in range(axis_max + 1):
        x = plot_x + tick * scale
        elements.append(
            f'<line class="grid" x1="{x:.1f}" y1="82" x2="{x:.1f}" y2="{height - 42}"/>'
        )
        elements.append(
            f'<text class="tick" x="{x:.1f}" y="78" text-anchor="middle">{tick}×</text>'
        )

    for index, benchmark in enumerate(benchmarks):
        base_y = 108 + index * 112
        elements.append(
            f'<text class="label" x="32" y="{base_y + 11}">{escape(benchmark.short_label)}</text>'
        )
        pairs = (
            ("vs pytest", benchmark.speedup_vs_pytest, "#2563eb"),
            ("vs pytest-xdist", benchmark.speedup_vs_xdist, "#7c3aed"),
        )
        for offset, (label, value, color) in enumerate(pairs):
            y = base_y + offset * 34
            bar_width = value * scale
            elements.append(
                f'<text class="legend" x="{plot_x - 12}" y="{y + 15}" '
                f'text-anchor="end">{label}</text>'
            )
            elements.append(
                f'<rect x="{plot_x}" y="{y}" width="{bar_width:.1f}" height="22" '
                f'rx="5" fill="{color}"/>'
            )
            elements.append(
                f'<text class="value" x="{plot_x + bar_width + 8:.1f}" y="{y + 16}">'
                f"{value:.2f}×</text>"
            )
    environment = benchmarks[0].environment
    cpu_model = escape(str(environment.get("cpu_model") or "legacy development machine"))
    python_version = escape(str(environment["python"]))
    elements.append(
        f'<text class="subtitle" x="32" y="{height - 16}">'
        f"Historical baseline · {cpu_model} · CPython {python_version} · raw samples linked below"
        "</text>"
    )
    elements.append("</svg>")
    return "\n".join(elements) + "\n"


def _public_api_snapshot() -> str:
    sys.path.insert(0, str(ROOT / "src"))
    import testenix  # noqa: PLC0415

    fallback_docs = {
        "skip": "Mark a test as skipped, optionally only when a condition is true.",
        "xfail": "Mark a test as expected to fail, optionally only when a condition is true.",
    }
    sections = [
        "# Expanded public API snapshot",
        "",
        "This section is generated from `testenix.__all__` so an LLM receives concrete symbols, "
        "signatures, enum values, dataclass fields, and docstrings rather than unresolved "
        "documentation directives.",
        "",
    ]
    for name in testenix.__all__:
        value = getattr(testenix, name)
        sections.extend((f"## `testenix.{name}`", ""))
        enum_type = value if inspect.isclass(value) and issubclass(value, Enum) else None
        if enum_type is not None:
            # EnumMeta exposes a different introspected signature across supported
            # Python versions. An enum's stable public constructor is its value.
            signature = inspect.Signature(
                parameters=(inspect.Parameter("value", inspect.Parameter.POSITIONAL_OR_KEYWORD),)
            )
        else:
            try:
                signature = inspect.signature(value)
            except (TypeError, ValueError):
                signature = None
        if signature is not None:
            sections.extend(("```text", f"{name}{signature}", "```", ""))

        if enum_type is not None:
            members = ", ".join(f"{item.name}={item.value!r}" for item in enum_type)
            sections.extend((f"Values: {members}", ""))
        elif inspect.isclass(value) and is_dataclass(value):
            sections.extend(("Fields:", ""))
            for field in fields(value):
                sections.append(f"- `{field.name}`: `{field.type}`")
            sections.append("")

        documentation = inspect.getdoc(value) or fallback_docs.get(name)
        if documentation:
            sections.extend((documentation, ""))
    return "\n".join(sections).rstrip() + "\n"


def _canonical_url(path_or_url: str) -> str:
    if path_or_url.startswith("https://"):
        return path_or_url
    return SITE_URL + path_or_url


def _absolute_document_target(source: Path, target: str) -> str:
    if target.startswith(("#", "data:", "http://", "https://", "mailto:")):
        return target

    path, separator, fragment = target.partition("#")
    path, query_separator, query = path.partition("?")
    normalized = Path(posixpath.normpath((source.parent / path).as_posix()))

    if normalized.parts and normalized.parts[0] == "docs":
        public_path = normalized.relative_to("docs").as_posix()
        if public_path.endswith(".md"):
            public_path = public_path.removesuffix(".md")
            public_path = "" if public_path == "index" else public_path.rstrip("/") + "/"
        elif (ROOT / f"{normalized.as_posix()}.md").exists():
            public_path = public_path.rstrip("/") + "/"
        absolute = SITE_URL + public_path
    else:
        absolute = f"{REPOSITORY_URL}/blob/main/{normalized.as_posix()}"

    if query_separator:
        absolute += f"?{query}"
    if separator:
        absolute += f"#{fragment}"
    return absolute


def _absolutize_document_links(content: str, source: Path) -> str:
    markdown_link = re.compile(r"(?P<prefix>!?\[[^\]\n]*\]\()(?P<target>[^)\s]+)(?P<suffix>\))")
    html_link = re.compile(
        r"(?P<prefix>\b(?:href|src)=[\"'])(?P<target>[^\"']+)(?P<suffix>[\"'])",
        re.IGNORECASE,
    )

    def replace(match: re.Match[str]) -> str:
        return (
            match.group("prefix")
            + _absolute_document_target(source, match.group("target"))
            + match.group("suffix")
        )

    return html_link.sub(replace, markdown_link.sub(replace, content))


def _render_llms_index() -> str:
    return f"""# Testenix

> Testenix is an alpha, async-native, parallel-first Python testing framework with a
> dependency-free native runtime, a lossless result model, and an optional pytest bridge.

Canonical documentation: {SITE_URL}
Source repository: {REPOSITORY_URL}
Package index: https://pypi.org/project/testenix/

## Start here

- [Overview]({SITE_URL}): project positioning, example, guarantees, and maturity.
- [Getting started]({SITE_URL}getting-started/): installation and the first run.
- [Pytest compatibility]({SITE_URL}guides/pytest-compatibility/): run existing suites unchanged,
  compare capabilities, and choose a migration boundary.
- [Safe migration]({SITE_URL}guides/migration/): convert supported pytest and unittest suites,
  validate parity, preserve originals, and interpret migrated-suite benchmarks.
- [Writing tests]({SITE_URL}guides/writing-tests/): tests, cases, tags, skips, xfail, and retries.
- [Fixtures]({SITE_URL}guides/fixtures/): dependency graphs, cleanup, and scopes.
- [Parallel execution]({SITE_URL}guides/parallelism/): workers, scheduling, crashes, and timeouts.

## Reference

- [CLI]({SITE_URL}reference/cli/): commands, options, and exit codes.
- [Configuration]({SITE_URL}reference/configuration/): every `[tool.testenix]` option.
- [Python API]({SITE_URL}reference/api/): authoring, execution, result, and event APIs.
- [Architecture]({SITE_URL}architecture/): boundaries, invariants, and worker protocol.

## Performance evidence

- [Benchmark results]({SITE_URL}benchmarks/results/): generated tables, chart, environment, raw
  samples, and limitations.
- [Benchmark contract]({SITE_URL}benchmarking/): required scenarios and validity rules.
- [Performance analysis]({SITE_URL}performance-analysis/): profiles, optimizations, memory, and
  native-code decision.

## Complete context

- [llms-full.txt]({SITE_URL}llms-full.txt): all guides, reference sources, expanded public API,
  architecture, roadmap, benchmark evidence, changelog, and security policy in one text file.
"""


def _document_content(path: Path, generated: dict[Path, str]) -> str:
    if path in generated:
        content = generated[path].strip()
    else:
        content = (ROOT / path).read_text(encoding="utf-8").strip()
    return _absolutize_document_links(content, path)


def _render_llms_full(generated: dict[Path, str]) -> str:
    sections = [
        "# Testenix complete documentation",
        "",
        "> Generated from the Testenix repository. Treat current documented behavior separately "
        "from roadmap items, and treat every benchmark as workload-specific.",
        "",
        f"Canonical documentation: {SITE_URL}",
        f"Source repository: {REPOSITORY_URL}",
        "",
    ]
    for title, path, url in LLM_DOCUMENTS:
        sections.extend(
            (
                "---",
                "",
                f"# Document: {title}",
                "",
                f"Canonical URL: {_canonical_url(url)}",
                f"Source: {path.as_posix()}",
                "",
                _document_content(path, generated),
                "",
            )
        )
    sections.extend(("---", "", _public_api_snapshot().strip(), ""))
    return "\n".join(sections).rstrip() + "\n"


def _outputs() -> dict[Path, str]:
    current_version = _project_version()
    benchmarks = tuple(_load_benchmark(path) for path in BASELINES)
    migration_benchmarks = tuple(_load_migration_benchmark(path) for path in MIGRATION_BASELINES)
    scaling_matrix = _load_scaling_matrix(SCALING_MATRIX, expected_version=current_version)
    results_path = Path("docs/benchmarks/results.md")
    generated = {
        results_path: _render_benchmark_results(
            benchmarks,
            migration_benchmarks,
            scaling_matrix,
            current_version=current_version,
        ),
        Path("docs/_static/benchmark-speedup.svg"): _render_benchmark_svg(benchmarks),
    }
    llms_index = _render_llms_index()
    llms_full = _render_llms_full(generated)
    generated.update(
        {
            Path("llms.txt"): llms_index,
            Path("docs/llms.txt"): llms_index,
            Path("llms-full.txt"): llms_full,
            Path("docs/llms-full.txt"): llms_full,
        }
    )
    return generated


def _write_or_check(outputs: dict[Path, str], *, check: bool) -> int:
    stale: list[str] = []
    for relative, content in outputs.items():
        path = ROOT / relative
        normalized = content.rstrip() + "\n"
        if check:
            if not path.exists() or path.read_text(encoding="utf-8") != normalized:
                stale.append(relative.as_posix())
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(normalized, encoding="utf-8")

    if stale:
        print("Generated documentation assets are stale:", file=sys.stderr)
        for stale_path in stale:
            print(f"  - {stale_path}", file=sys.stderr)
        print("Run: python scripts/generate_docs_assets.py", file=sys.stderr)
        return 1
    if not check:
        for path in outputs:
            print(path.as_posix())
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when checked-in generated assets differ from canonical sources",
    )
    arguments = parser.parse_args()
    return _write_or_check(_outputs(), check=arguments.check)


if __name__ == "__main__":
    raise SystemExit(main())
