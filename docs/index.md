---
description: Testenix is an async-native, parallel-first Python testing framework with lossless results.
---

# Testenix

<div class="testenix-hero">
  <div class="testenix-kicker">Python testing framework · Alpha</div>
  <div class="testenix-title">Fast tests. Clear results.</div>
  <p>
    Testenix combines a dependency-free native runtime with a transparent bridge for
    running existing pytest suites unchanged.
  </p>
  <div class="testenix-actions">
    <a href="getting-started/">Start testing</a>
    <a href="guides/pytest-compatibility/">Run pytest suites</a>
  </div>
  <div class="testenix-quick-links">
    <a href="guides/migration/">Migration guide →</a>
    <a href="benchmarks/results/">Benchmarks →</a>
  </div>
</div>

```{toctree}
:hidden:
:caption: Start
:maxdepth: 2

getting-started
guides/pytest-compatibility
guides/migration
```

```{toctree}
:hidden:
:caption: Guides
:maxdepth: 2

guides/writing-tests
guides/fixtures
guides/parallelism
guides/reports
```

```{toctree}
:hidden:
:caption: Reference
:maxdepth: 2

reference/cli
reference/configuration
reference/api
```

```{toctree}
:hidden:
:caption: Project
:maxdepth: 2

benchmarks/results
benchmarking
performance-analysis
architecture
roadmap
for-llms
```

## Why Testenix

<div class="metric-grid">
  <div class="metric-card"><strong>0</strong><span>dependencies in the native runtime</span></div>
  <div class="metric-card"><strong>12</strong><span>Python and OS combinations in CI</span></div>
  <div class="metric-card"><strong>3</strong><span>console, JSON, and JUnit reports</span></div>
  <div class="metric-card"><strong>3.15×</strong><span>historical v0.1.0 result: 100k synthetic no-op tests, 4 workers, --no-history</span></div>
</div>

Testenix is deliberately built around a few strong guarantees:

- **Async is native.** Coroutine tests and async-generator fixtures use the same model as
  synchronous code and do not require a plugin.
- **Parallelism is part of the runner.** Adaptive worker selection, module affinity, optional
  safety-checked sharding, process isolation, and duration-aware scheduling are designed together.
- **Retries preserve evidence.** A failed attempt followed by a pass is `FLAKY`, never silently
  rewritten as a clean pass.
- **Crashes cannot erase completed work.** Workers stream results as tests finish, and unfinished
  tests receive explicit terminal outcomes.
- **Reports share one model.** Console, JSON, JUnit, history, and the library API are derived from
  the same versioned result contracts.

## A complete first test

Already have a pytest suite? Keep its fixtures, parametrization, markers, classes, configuration,
and plugins:

```console
$ python -m pip install "testenix[pytest]"
$ testenix pytest -q tests
```

[Read the compatibility contract](guides/pytest-compatibility/) before migrating individual
modules to the native engine.

To create a validated native copy without modifying the originals, use:

```console
$ testenix migrate auto tests --dry-run
$ testenix migrate auto tests --check
$ testenix migrate auto tests --output tests_testenix
```

The migrator executes the source baseline and both serial and parallel native candidates in
disposable project copies, compares their inventories and outcomes, and publishes only through an
atomic no-overwrite rename. [Read the safe migration contract](guides/migration/).

```python
from collections.abc import AsyncIterator

from testenix import case, cases, fixture, test


@fixture(scope="module")
async def multiplier() -> AsyncIterator[int]:
    yield 2


@test("multiplication uses an async fixture", tags={"unit"})
@cases(
    case(id="positive", value=3, expected=6),
    case(id="zero", value=0, expected=0),
)
async def multiplication(multiplier: int, value: int, expected: int) -> None:
    assert multiplier * value == expected
```

Run it locally:

```console
$ python -m pip install testenix
$ testenix run tests
```

`workers = "auto"` adapts to the schedulable work instead of launching one process per CPU. Use
`testenix tune` (also available as `testenix benchmark`) for a measured project recommendation.
Import-heavy native suites can explicitly generate a source-hashed collection manifest, and large
independent modules can opt into conservative `--shard-modules` scheduling. See
[parallel execution](guides/parallelism/) for both trust boundaries.

To evaluate unreleased source changes, install the current `main` branch with
`python -m pip install "testenix @ git+https://github.com/polishdataengineer/testenix.git@main"`.

## Performance evidence, with context

The checked-in development baseline measured **Testenix 0.1.0**, not the current release. Native
`testenix run` completed 100,000 generated no-op tests across 16 modules on one Apple M4 Pro and
CPython 3.11 machine in a median 8.04 seconds, compared with 25.33 seconds for pytest and 21.30
seconds for pytest-xdist. It used four workers, `--no-history`, and pytest-xdist's default `load`
scheduler. These measurements do not apply to Testenix 0.3.0, a real project, the default-history
mode, or the delegated `testenix pytest` command.

<div class="benchmark-caveat">
This is historical synthetic evidence from one machine, not a promise that every project will be
3.15× faster. No clean Testenix 0.3.0 scaling matrix is checked in yet. The benchmark page publishes
the raw samples, environment, variance, methodology, current matrix status, and limitations.
</div>

[Inspect the benchmark data](benchmarks/results/) or
[reproduce the harness](benchmarking/).

## Project maturity

Testenix is alpha software. The `testenix pytest` bridge preserves an existing suite by delegating
to real pytest, while `testenix run` is a distinct native engine rather than a drop-in pytest
reimplementation. Pytest still has a much broader plugin ecosystem, richer IDE integration, and
mature assertion rewriting. Choose the bridge for compatibility and the native engine when its
async model, built-in parallel execution, explicit failure semantics, or dependency-free core are
more important.
