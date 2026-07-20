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
in either execution median.

{table_header}
| --- | --- | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(rows)}

The native side used four workers. The source pytest and unittest outcome-probe baselines were
sequential, so these rows do not compare Testenix with pytest-xdist or another parallel unittest
runner. The unittest probe uses the standard-library loader and result semantics, then serializes
per-test outcomes for parity checking; its timing therefore includes that small audit overhead.
The no-op unittest wrappers are 5.17× slower than the probe because wrapper, loading, and
result-adaptation costs dominate an empty body. With 1 ms of synthetic work per unittest method,
parallel native execution is 1.59× faster in this 64-module layout. Module count and duration are
therefore material, and none of these synthetic rows predicts a specific real project.

### Raw migration samples and variance

{chr(10).join(detail_sections)}
"""


def _render_benchmark_results(
    benchmarks: tuple[Benchmark, ...],
    migration_benchmarks: tuple[MigrationBenchmark, ...],
) -> str:
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

![Preliminary Testenix throughput ratios](../_static/benchmark-speedup.svg)

## Median wall-clock time

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

## Environment

- CPU: {cpu_model} ({environment["cpu_count"]} logical CPUs)
- Machine: `{environment["machine"]}`
- Platform: `{environment["platform"]}`
- Python: `{environment["python"]}`
- Measurement: complete subprocess wall-clock time, including discovery, execution, aggregation,
  and console rendering
- Correctness gate: every command had to exit successfully and report the expected test count

{legacy_note}

## Raw samples and variance

"""
        + "\n".join(detail_sections)
        + "\n"
        + _render_migration_results(migration_benchmarks)
        + """
## Interpretation

The checked-in results show that Testenix has low per-test overhead for large generated suites and
that its built-in process model is competitive with both sequential pytest and pytest-xdist in
those scenarios.

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
        '<title id="title">Preliminary Testenix benchmark throughput ratios</title>',
        '<desc id="description">Horizontal bars compare Testenix throughput with pytest and '
        "pytest-xdist for three checked-in synthetic benchmark scenarios.</desc>",
        "<style>"
        ".title{font:700 22px system-ui,sans-serif;fill:#0f172a}"
        ".subtitle,.tick,.legend{font:13px system-ui,sans-serif;fill:#475569}"
        ".label{font:600 14px system-ui,sans-serif;fill:#1e293b}"
        ".value{font:700 13px system-ui,sans-serif;fill:#0f172a}"
        ".grid{stroke:#cbd5e1;stroke-width:1}"
        "</style>",
        '<rect width="980" height="100%" rx="16" fill="#ffffff"/>',
        '<text class="title" x="32" y="36">Synthetic benchmark throughput ratio</text>',
        '<text class="subtitle" x="32" y="60">Higher is better · 1× means equal throughput</text>',
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
        f"Development baseline · {cpu_model} · CPython {python_version} · raw samples linked below"
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
        try:
            signature = inspect.signature(value)
        except (TypeError, ValueError):
            signature = None
        if signature is not None:
            sections.extend(("```text", f"{name}{signature}", "```", ""))

        if inspect.isclass(value) and issubclass(value, Enum):
            members = ", ".join(f"{item.name}={item.value!r}" for item in value)
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
        "> Generated from the Testenix repository. Treat current 0.1 behavior separately from "
        "roadmap items, and treat every benchmark as workload-specific.",
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
    benchmarks = tuple(_load_benchmark(path) for path in BASELINES)
    migration_benchmarks = tuple(_load_migration_benchmark(path) for path in MIGRATION_BASELINES)
    results_path = Path("docs/benchmarks/results.md")
    generated = {
        results_path: _render_benchmark_results(benchmarks, migration_benchmarks),
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
