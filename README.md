# Testenix

[![CI](https://github.com/polishdataengineer/testenix/actions/workflows/ci.yml/badge.svg)](https://github.com/polishdataengineer/testenix/actions/workflows/ci.yml)
[![Documentation](https://github.com/polishdataengineer/testenix/actions/workflows/docs.yml/badge.svg)](https://polishdataengineer.github.io/testenix/)
[![PyPI](https://img.shields.io/pypi/v/testenix.svg?cacheSeconds=300)](https://pypi.org/project/testenix/)
[![Python](https://img.shields.io/pypi/pyversions/testenix.svg?cacheSeconds=300)](https://pypi.org/project/testenix/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/polishdataengineer/testenix/blob/main/LICENSE)

**Fast tests. Clear results.**

Testenix is an experimental native Python testing framework built around five guarantees:

1. tests and fixtures are ordinary typed Python functions;
2. synchronous and asynchronous code use the same execution model;
3. local parallel execution is a core feature rather than a plugin;
4. retries never erase previous failures;
5. every report is derived from one versioned, lossless result model.

The project is currently an alpha. Its native runtime has no third-party dependencies and does not
depend on pytest. An optional compatibility bridge can delegate unchanged pytest suites to the
pytest installation already present in a project.

## Installation

Install the published package with:

```bash
python -m pip install testenix
```

For an existing pytest project, install the optional convenience extra:

```bash
python -m pip install "testenix[pytest]"
```

If a supported pytest (`>=8.3,<10`) is already installed in the same environment, the base
`testenix` package is sufficient.

Testenix requires Python 3.11 or newer. The project is currently an alpha; pin the version before
using it in CI. Use the GitHub installation below only when you intentionally want unreleased
changes from the current development branch.

To try the current checkout before publication:

```bash
python -m pip install .
# or, for an isolated CLI installation
uv tool install .
```

You can also install the latest source directly from GitHub:

```bash
python -m pip install "testenix @ git+https://github.com/polishdataengineer/testenix.git@main"
# include pytest when the project environment does not already provide it
python -m pip install "testenix[pytest] @ git+https://github.com/polishdataengineer/testenix.git@main"
```

## Run an existing pytest suite

Yes. Testenix provides a transparent compatibility bridge for existing pytest projects:

```bash
testenix pytest -q --tb=short tests
```

Everything after `testenix pytest` is forwarded unchanged to the same interpreter as
`python -m pytest`. This preserves pytest collection, `conftest.py`, fixtures, parametrization,
markers, assertion rewriting, plugins, configuration, output, node IDs, and exit codes.
Consequently, output produced by `uv run pytest tests` or `testenix pytest ...` is pytest's UI,
not the native Testenix console reporter.

```bash
testenix pytest tests/test_api.py::test_health -k smoke --maxfail=1
testenix pytest -n auto tests  # requires pytest-xdist in the same environment
testenix pytest --junitxml=reports/pytest.xml tests
```

This command is a compatibility and migration bridge. It does not use the native Testenix
collector, scheduler, worker pool, retries, history, result model, or reporters. Its performance is
therefore pytest performance plus launcher and adapter overhead; the native benchmark results do
not apply to it. See the
[pytest compatibility guide](https://polishdataengineer.github.io/testenix/guides/pytest-compatibility/)
for the complete capability matrix and migration choices.

## Convert pytest or unittest tests to native Testenix

The safe migrator creates a new native suite while leaving every original test untouched:

```bash
# inspect static support only
testenix migrate auto tests --dry-run

# run the source baseline plus serial and parallel candidates, but publish nothing
testenix migrate auto tests --check --report-json reports/migration-check.json

# validate and atomically create a new directory
testenix migrate pytest tests --output tests_testenix \
  --report-json reports/migration.json
testenix run tests_testenix
```

For pytest migration, install `testenix[pytest]`; unittest migration uses the standard library.
`auto` supports pytest and unittest in separate modules within one selection.

Large unsupported suites get a grouped console summary instead of one wall of repeated lines.
Use `--report-json FILE` or `--report-json -` to retain every individual diagnostic with its source
and line. A blocked run labels safe in-memory mappings as a *statically convertible subset*; only a
report with `status: published` means that Testenix created the requested output.

This is a copy-and-validate transaction, not an in-place rewrite. Testenix fingerprints the
sources, generates into private staging, runs the green original suite in a disposable project
copy, runs the native candidate with one worker and again in parallel, compares inventory and
outcome totals plus every mapped test outcome, rechecks every source hash, and only then performs
an atomic no-overwrite publish. Any validation or publication failure before that rename leaves
the requested output absent. If only the optional audit-report write fails after a successful
rename, Testenix warns without pretending the already published output was rolled back. Report
paths must be new, inside the project, and disjoint from both source and generated suites. There is
no `--force` option, and old tests are never deleted or renamed.

The converter stops on semantics it cannot preserve. The v0.2 pytest subset covers module
functions, one static parametrization, simple local/adjacent-conftest fixtures, statically declared
autouse fixtures, bare `@pytest.mark.asyncio` coroutine tests through fresh function-scoped loop
wrappers, and simple pytest classes. Native `tmp_path` and a dependency-free `monkeypatch`
implementation cover the common `setattr` and `setenv` forms with automatic per-test rollback,
including calls through statically provable module-local helpers. Complex class lifecycle, async
fixtures, unmarked async tests, configured async loop scopes or debug mode, custom
`event_loop_policy`, and the rest of pytest's built-in fixtures remain blocked. The unittest adapter
preserves per-test lifecycle and assertions by generating native wrappers around the original
`TestCase.run()` protocol; those wrappers locate originals independently of `cwd` and verify the
complete selected-Python-source SHA-256 manifest, so the old unittest files must remain present.
Keep the generated unittest directory at its published path as well; rerun migration after moving
either side.

See the full [safe migration guide](https://polishdataengineer.github.io/testenix/guides/migration/)
for the support matrix, rollback contract, CI rollout, audit-report schema, and performance
interpretation for suites with thousands of tests.

## Native quick start

Create `tests/test_multiplication.py` with this complete example:

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

Run it with:

```bash
testenix run tests
```

The native command uses a compact, file-level report by default and still prints complete failure
details and a final summary. A typical failing run looks like this (the run ID and timings vary):

```console
$ testenix run tests
Testenix  |  4 tests  |  2 files  |  2 workers

PASS  tests/test_multiplication.py                  2 passed           [8ms]
FAIL  tests/test_checkout.py                        1 passed, 1 failed [12ms]

Problems (1)
FAIL      tests/test_checkout.py::test_rejects_expired_card
          attempt 1, call: expected status 402, got 200

4 tests, 3 passed, 1 failed in 0.084s
```

Use `-q` to hide the header and file table while retaining collection errors, failure details, and
the final summary. `-v` prints one result row per test; `-vv` also exposes worker, attempt, and
phase metadata. `--show-skips` includes skip and expected-failure reasons, `--durations N` lists
the `N` slowest tests (`--durations 0` lists all), and `--color auto|always|never` controls ANSI
styling.

Plain `test_*` functions are collected without `@test`; the decorator is useful for descriptions,
tags, and per-test timeouts.

Configuration lives in `pyproject.toml`:

```toml
[tool.testenix]
workers = "auto"
retries = 0
paths = ["tests"]
history = ".testenix/history.sqlite3"
# json = "reports/testenix.json"
# junit = "reports/junit.xml"
```

Command-line options override this table:

```text
testenix run [PATH ...] [--workers auto|N] [--retries N] [--timeout SECONDS]
                    [--tag TAG ...] [--json FILE] [--junit FILE]
                    [--history FILE | --no-history] [-q | -v | -vv]
                    [--color auto|always|never | --no-color]
                    [--show-skips] [--durations N]
```

Repeated `--tag` options use AND semantics: a selected test must contain every requested tag.
The console report is always printed after execution; it is deterministic output, not a live
progress display. JSON preserves the complete run/test/attempt/phase model,
JUnit targets CI systems, and SQLite history supplies duration estimates to later runs. History is
enabled at `.testenix/history.sqlite3` by default; use `--no-history` for a side-effect-free run.

The same runner is available as a typed library API:

```python
from testenix import TestenixConfig, Status, run

result = run("tests", TestenixConfig(workers="auto", history_path=None))
failed_ids = [test.test.id for test in result.tests if test.status is not Status.PASS]
```

Async applications can `await testenix.run_async(...)`; cancellation terminates active collection and
execution process trees before returning control to the caller.

## Exit codes

| Code | Meaning |
| ---: | --- |
| `0` | No gating failure. Passing, skipped, xfailed, and cached tests are non-gating. |
| `1` | A test failed, errored, timed out, crashed, unexpectedly passed, or was finalized as flaky. |
| `2` | Collection, command/configuration error, or an explicit tag filter selected no tests. |
| `3` | Internal runner, report, or history error. |
| `4` | Migration found an unsupported construct and published nothing. |
| `130` | Interrupted by the user. |

## Result semantics

Testenix preserves the complete hierarchy `run -> test -> attempt -> phase`. The phases are `setup`,
`call`, and `teardown`. A failed attempt followed by a successful retry is reported as `FLAKY`,
not `PASS`. Setup errors, teardown errors, timeouts, process crashes, expected failures, unexpected
passes, and infrastructure failures remain distinct outcomes.

A process crash gets one automatic recovery attempt, but `CRASH -> PASS` is still `FLAKY` and
gates CI; Testenix cannot prove that an abrupt process exit was unrelated to the test. An unfinished
asyncio task is cancelled at the test boundary and fails its owner instead of leaking into the next
test.

## Where Testenix is deliberately different

The `testenix run` engine is not a native drop-in reimplementation of pytest. Its v0.2 value is a
smaller, coherent native stack: async tests and async fixtures need no plugin, parallel execution
and duration-aware scheduling need no xdist, every retry remains visible, and a worker crash cannot
silently erase tests that completed before it. The native runtime has no third-party dependencies.

For unchanged pytest semantics, use `testenix pytest`. That bridge intentionally delegates to
pytest instead of silently approximating fixtures, markers, plugins, or hook behavior.

For supported pytest and unittest modules, `testenix migrate` provides a conservative path to the
native engine. Differential validation is part of that command; a syntactically successful rewrite
alone is never treated as a completed migration.

The trade-off is ecosystem maturity. pytest currently has much broader plugin, IDE, assertion
rewriting, and migration support. Testenix should only claim to be better for the guarantees above
until the adoption work in the roadmap is complete and measured on real projects.

## Documentation and LLM reference

The complete documentation is published at
[polishdataengineer.github.io/testenix](https://polishdataengineer.github.io/testenix/). Every page
can copy its own text or the complete project reference for an LLM.

- [`llms.txt`](https://polishdataengineer.github.io/testenix/llms.txt) provides a compact index.
- [`llms-full.txt`](https://polishdataengineer.github.io/testenix/llms-full.txt) contains the guides,
  public API, architecture, roadmap, and benchmark context in one file.
- [Documentation for LLMs](https://polishdataengineer.github.io/testenix/for-llms/) explains the
  source-of-truth and copying workflow.

## Benchmarks

In the checked-in M4 Pro/CPython 3.11 synthetic baseline, native `testenix run` completed 100,000
empty tests across 16 modules in a median 8.04 seconds, compared with 25.33 seconds for pytest and
21.30 seconds for pytest-xdist. That is 3.15x the throughput of pytest for this specific workload,
not a universal performance promise. The result includes one warm-up and five measured,
counterbalanced rounds. It does not describe `testenix pytest`, which executes through pytest.

The separate safe-migration benchmark used 3,000 tests across 64 modules and four native workers.
After conversion, pytest no-op tests ran in 0.521 seconds versus 1.539 seconds through sequential
pytest (2.96x faster). The result reverses for empty unittest methods: native wrappers took 1.192
seconds versus 0.161 seconds through a sequential stdlib-based outcome probe (7.40x slower). Adding
1 ms of synthetic work per unittest method changed the medians to 2.577 versus 4.066 seconds (1.58x
faster). Migration itself was a one-time 5.94–17.25-second validation-and-publication transaction
and is not included in those recurring-run medians. These synthetic comparisons depend on module
layout, test duration, and worker count; they do not establish a universal advantage over pytest,
pytest-xdist, unittest, or real project suites.

See the [generated results and chart](https://polishdataengineer.github.io/testenix/benchmarks/results/),
[raw JSON](https://github.com/polishdataengineer/testenix/tree/main/benchmarks),
[methodology](https://polishdataengineer.github.io/testenix/benchmarking/), and
[performance analysis](https://polishdataengineer.github.io/testenix/performance-analysis/).

## Current limitations

- Parallel workers are isolated processes. Normal tests from one module stay together, so a
  module-scoped fixture is not duplicated merely because `--workers` is greater than one.
- Reproducible 1k/10k/100k comparisons, profiler findings, memory measurements, and the Rust/PyO3
  decision are documented in
  [the performance analysis](https://polishdataengineer.github.io/testenix/performance-analysis/).
  Session-scoped fixtures remain per worker process, not run-global.
- Every test with an explicit or global timeout is hard-isolated in its own process. This makes a
  blocking synchronous call killable on every supported platform, but module/session fixtures used
  by timed tests cannot be shared with neighbouring tests.
- Collection imports user modules in a supervised process. A crash becomes a collection error and
  a hung import has a bounded 30-second deadline instead of taking down the coordinator.
- Case values are reconstructed by rediscovering the module in the worker and do not need to be
  pickle-serializable. They must still be reproducible during module import; reports store a
  JSON-safe representation when a value itself is not serializable.
- Synchronous test and fixture bodies run outside Testenix's internal asyncio loop. APIs restricted
  to Python's main thread, such as installing signal handlers, are not supported inside those
  bodies in v0.2. Migrated pytest-asyncio wrappers are synchronous from Testenix's perspective and
  therefore share this restriction while creating a fresh event loop for each test or case.
- On Windows, a script that calls the programmatic `run()`/`run_async()` API must use the standard
  `if __name__ == "__main__":` multiprocessing guard. The `testenix` CLI handles process startup
  itself.
- The pytest bridge does not translate delegated outcomes into Testenix `RunResult`, JSON, history,
  retry, timeout, or scheduling semantics. Use pytest's own flags and installed plugins in that mode.
- Native migration requires a green source baseline and a new output directory. Filesystem changes
  inside the project are isolated by disposable copies during validation, but network, database,
  cloud, and other external test side effects are not sandboxed.
- A normal module is one scheduler-affinity unit. Converting 3,000 tests in one module does not
  create 3,000 parallel units; spread independent tests across modules and measure the generated
  suite before making a project-specific speed claim.
- Test impact analysis, result caching, remote workers, and deep pytest-result aggregation are not
  part of version 0.2.

## Project status

Testenix 0.2.1 is pre-1.0 software. The distribution, import package, CLI, configuration namespace,
and state directory consistently use `testenix`. The project is licensed under MIT and releases
are published to PyPI through Trusted Publishing.

## Development

The project targets Python 3.11 and newer.

```bash
uv sync --no-editable
uv run --no-editable pytest
uv run --no-editable ruff check .
```

See [the architecture](https://polishdataengineer.github.io/testenix/architecture/),
[the roadmap](https://polishdataengineer.github.io/testenix/roadmap/), and
[the benchmarking contract](https://polishdataengineer.github.io/testenix/benchmarking/).
