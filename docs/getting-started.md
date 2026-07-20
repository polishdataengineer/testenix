# Getting started

This guide takes a new project from installation to its first parallel Testenix run.

## Requirements

- CPython 3.11 or newer
- Linux, macOS, or Windows
- no runtime dependencies beyond the Python standard library

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

Until the first PyPI release is visible, install directly from the protected `main` branch:

```console
$ python -m pip install "testenix @ git+https://github.com/polishdataengineer/testenix.git@main"
```

## Create a test

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

## Use it from Python

The runner also exposes a typed API:

```python
from testenix import Status, TestenixConfig, run

result = run("tests", TestenixConfig(workers=4, history_path=None))
failed = [item.test.id for item in result.tests if item.status is not Status.PASS]
```

Async applications can call `await testenix.run_async(...)`. Cancellation terminates active
collection and execution process trees before control returns to the caller.

## Next steps

- [Write tests, cases, tags, skips, and expected failures](guides/writing-tests.md)
- [Build fixture graphs](guides/fixtures.md)
- [Understand process parallelism and timeouts](guides/parallelism.md)
- [Produce JSON and JUnit reports](guides/reports.md)
- [Review every configuration option](reference/configuration.md)
