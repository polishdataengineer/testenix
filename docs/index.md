---
description: Testenix is an async-native, parallel-first Python testing framework with lossless results.
---

# Testenix

<div class="testenix-hero">
  <div class="testenix-kicker">Python testing framework · Alpha</div>
  <div class="testenix-title">Fast tests. Clear results.</div>
  <p>
    Testenix unifies synchronous and asynchronous tests, fixtures, local parallelism,
    retries, crash recovery, and machine-readable reports in one dependency-free runtime.
  </p>
  <div class="testenix-actions">
    <a href="getting-started/">Start testing</a>
    <a href="benchmarks/results/">See the benchmarks</a>
    <a href="for-llms/">Copy docs for an LLM</a>
  </div>
</div>

```{toctree}
:hidden:
:maxdepth: 2

getting-started
guides/writing-tests
guides/fixtures
guides/parallelism
guides/reports
reference/cli
reference/configuration
reference/api
benchmarks/results
benchmarking
performance-analysis
architecture
roadmap
for-llms
```

## Why Testenix

<div class="metric-grid">
  <div class="metric-card"><strong>0</strong><span>third-party runtime dependencies</span></div>
  <div class="metric-card"><strong>12</strong><span>Python and OS combinations in CI</span></div>
  <div class="metric-card"><strong>3</strong><span>console, JSON, and JUnit reports</span></div>
  <div class="metric-card"><strong>2.84×</strong><span>preliminary 100k synthetic throughput vs pytest</span></div>
</div>

Testenix is deliberately built around a few strong guarantees:

- **Async is native.** Coroutine tests and async-generator fixtures use the same model as
  synchronous code and do not require a plugin.
- **Parallelism is part of the runner.** Module affinity, process isolation, and
  duration-aware scheduling are designed together.
- **Retries preserve evidence.** A failed attempt followed by a pass is `FLAKY`, never silently
  rewritten as a clean pass.
- **Crashes cannot erase completed work.** Workers stream results as tests finish, and unfinished
  tests receive explicit terminal outcomes.
- **Reports share one model.** Console, JSON, JUnit, history, and the library API are derived from
  the same versioned result contracts.

## A complete first test

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

If the first PyPI release is not available yet, install the current source with
`python -m pip install "testenix @ git+https://github.com/polishdataengineer/testenix.git@main"`.

## Performance evidence, with context

The checked-in development baseline measured 100,000 empty tests across 16 generated modules on
an Apple M4 Pro and CPython 3.11. Testenix completed that specific workload in a median 11.96
seconds, compared with 33.96 seconds for pytest and 44.32 seconds for pytest-xdist.

<div class="benchmark-caveat">
This is a preliminary synthetic result from one machine, not a promise that every project will be
2.84× faster. The benchmark page publishes the raw samples, environment, variance, methodology,
and limitations so that the claim can be evaluated rather than taken on trust.
</div>

[Inspect the benchmark data](benchmarks/results/) or
[reproduce the harness](benchmarking/).

## Project maturity

Testenix is alpha software and is not yet a drop-in pytest replacement. Pytest has a much broader
plugin ecosystem, richer IDE integration, and mature assertion rewriting. Testenix is a good fit
when its native async model, built-in parallel execution, explicit failure semantics, or
dependency-free runtime are more important than ecosystem compatibility.
