# Getting started

This guide takes a new project from installation to its first parallel Testenix run.

## Requirements

- CPython 3.11 or newer
- Linux, macOS, or Windows
- no runtime dependencies beyond the Python standard library for native `testenix run`
- pytest in the same environment when using the optional compatibility bridge

## Install

Install the released package from PyPI:

```console
$ python -m pip install testenix
$ testenix --version
```

For a project managed by uv:

```console
$ uv add --dev testenix
$ uv run testenix --version
```

For an existing pytest project, install the compatibility extra:

```console
$ python -m pip install "testenix[pytest]"
# or
$ uv add --dev "testenix[pytest]"
```

To evaluate unreleased development changes, install directly from the protected `main` branch:

```console
$ python -m pip install "testenix @ git+https://github.com/polishdataengineer/testenix.git@main"
# include pytest when the project environment does not already provide it
$ python -m pip install "testenix[pytest] @ git+https://github.com/polishdataengineer/testenix.git@main"
```

## Choose an execution mode

Run an unchanged pytest suite through its real engine:

```console
$ testenix pytest -q --tb=short tests
```

Use `testenix run` for native Testenix tests and the built-in scheduler, retries, history, and
lossless reports. The two commands have deliberately separate semantics. See
[pytest compatibility](guides/pytest-compatibility.md) for the capability matrix and migration
boundary.

Create a validated native copy of supported pytest or unittest modules without changing the
originals:

```console
$ testenix migrate auto tests --dry-run
$ testenix migrate auto tests --output tests_testenix
$ testenix run tests_testenix
```

Migration requires a green source run and matching serial/parallel native runs before an atomic
new-directory publish. Read [safe migration](guides/migration.md) before using it on a suite with
external side effects.

## Create a native test

Testenix collects ordinary functions whose names begin with `test_`. Decorators are optional for
simple tests.

```python
# tests/test_math.py

def test_addition() -> None:
    assert 2 + 2 == 4
```

Run the suite:

```console
$ testenix run tests
```

The default console output is a compact per-file report followed by complete failure details and a
final summary. It is rendered deterministically after the run rather than updated as live progress.
Use `-q` to omit the header and file table, `-v` for one row per test, or `-vv` for worker, attempt,
and phase metadata. Collection errors and failure details remain visible with `-q`.

```console
$ testenix run -q tests
4 tests, 3 passed, 1 skipped in 0.071s
```

The process exits with code `0` when every selected test has a non-gating terminal status.

## Add native metadata

Use `@test` when a description, tags, or a hard timeout should be part of the test contract.

```python
from testenix import test


@test("addition remains correct", tags={"unit", "fast"}, timeout=2.0)
def addition() -> None:
    assert 2 + 2 == 4
```

Select tagged tests by repeating `--tag`. Repeated tags use AND semantics:

```console
$ testenix run --tag unit --tag fast
```

## Configure the project

Put stable defaults in `pyproject.toml`:

```toml
[tool.testenix]
paths = ["tests"]
workers = "auto"
retries = 0
history = ".testenix/history.sqlite3"
# shard_modules = true
# manifest = ".testenix/collection.json"
# timeout = 10
# json = "reports/testenix.json"
# junit = "reports/junit.xml"
```

Command-line values override the project table:

```console
$ testenix run --workers 4 --retries 1 --json reports/result.json
```

Use `--no-history` for a side-effect-free run. Duration history normally helps later runs schedule
long tests earlier.

`workers = "auto"` adapts to the selected suite instead of copying the logical CPU count. It is
capped by the actual schedulable units and uses duration history when enough is available. Measure
an explicit project setting with:

```console
$ testenix tune tests --warmups 1 --repeats 5
$ testenix tune --write
```

`testenix benchmark` is an alias for `testenix tune`.

Module affinity is the safe default. For a large module whose tests are known to be independent,
`--shard-modules` opts eligible tests into finer scheduling after conservative static checks. Read
[parallel execution](guides/parallelism.md) before enabling it.

To avoid repeating collection imports on later unchanged runs, create and trust a source-hashed
manifest explicitly:

```console
$ testenix manifest tests --output .testenix/collection.json
$ testenix run tests --manifest .testenix/collection.json
```

Testenix verifies the complete file inventory and every SHA-256 before reuse. A stale manifest
falls back to supervised collection rather than running a stale test selection. Parameter names
remain visible in the artifact, but their values are redacted.

## Use it from Python

The runner also exposes a typed API:

```python
from testenix import Status, TestenixConfig, run


def main() -> None:
    result = run("tests", TestenixConfig(workers=4, history_path=None))
    failed = [item.test.id for item in result.tests if item.status is not Status.PASS]
    print(failed)


if __name__ == "__main__":
    main()
```

Async applications can call `await testenix.run_async(...)`. Cancellation terminates active
collection and execution process trees before control returns to the caller. Executable scripts
must place the top-level call behind the same `if __name__ == "__main__":` guard on every platform.

## Next steps

- [Write tests, cases, tags, skips, and expected failures](guides/writing-tests.md)
- [Run or migrate an existing pytest suite](guides/pytest-compatibility.md)
- [Convert pytest or unittest safely without replacing originals](guides/migration.md)
- [Build fixture graphs](guides/fixtures.md)
- [Understand process parallelism and timeouts](guides/parallelism.md)
- [Produce JSON and JUnit reports](guides/reports.md)
- [Review every configuration option](reference/configuration.md)
