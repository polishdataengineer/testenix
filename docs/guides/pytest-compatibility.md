---
description: Run existing pytest suites unchanged through Testenix and understand the native migration boundary.
---

# Pytest compatibility

Testenix can run an existing pytest suite without rewriting it. Use the compatibility bridge when
the suite depends on pytest fixtures, parametrization, markers, classes, plugins, hooks, or
configuration:

```console
$ python -m pip install "testenix[pytest]"
$ testenix pytest -q tests
```

If a supported pytest (`>=8.3,<10`) is already installed in the same Python environment, installing
the base `testenix` package is enough. The extra is a convenience for environments that do not
already contain pytest.

## What the command does

Everything after `testenix pytest` is forwarded unchanged to:

```console
$ python -m pytest [PYTEST_ARGS ...]
```

Testenix hands its current CLI process to pytest from the same Python interpreter. On POSIX it uses
a process overlay; on Windows it calls pytest's public console entry point in-process because the
platform does not provide the same overlay semantics. Both paths preserve the foreground process,
working directory, environment, terminal, standard streams, and pytest's signal handling. Pytest
therefore remains responsible for collection, execution, configuration, plugin loading, output,
descendants such as pytest-xdist workers, and exit status.

```console
$ testenix pytest tests/test_api.py::test_health -k smoke --maxfail=1
$ testenix pytest -m "unit and not slow" tests
$ testenix pytest -n auto tests
$ testenix pytest --junitxml=reports/pytest.xml tests
```

The `-n` example requires pytest-xdist in the same environment. Testenix does not translate its
native `--workers` option into an xdist option.

## Capability matrix

| Capability | `testenix pytest` | `testenix run` |
| --- | --- | --- |
| Plain module-level `test_*` functions | Yes, through pytest | Yes |
| Pytest classes and unittest-style tests | Yes | No |
| `pytest.fixture` and built-in fixtures | Yes | No |
| `conftest.py` fixture and hook discovery | Yes | No |
| `pytest.mark.parametrize` | Yes | No; use Testenix `@case` or `@cases` |
| Pytest skip, xfail, and custom markers | Yes | No; use Testenix decorators and tags |
| Pytest assertion rewriting | Yes | No |
| Pytest plugins and hooks | Yes, when installed in the same environment | No |
| `pytest.ini` and `[tool.pytest.ini_options]` | Yes | No |
| Pytest node IDs and CLI selectors | Yes | No |
| pytest-xdist | Yes, when installed and requested with `-n` | No; use native `--workers` |
| Async tests | According to the installed pytest plugins | Native, without a plugin |
| Testenix retries and `FLAKY` semantics | No | Yes |
| Testenix duration history and scheduling | No | Yes |
| Testenix lossless result model | No | Yes |
| Testenix JSON/JUnit reporters | No; use pytest options or plugins | Yes |
| Published native Testenix speedups | No | Only for the documented workloads |
| Exit status | Unchanged pytest or plugin status | Testenix exit-code contract |

Some simple pytest-authored functions also happen to run with `testenix run`, but that overlap is
not a compatibility guarantee. In particular, the native collector does not interpret pytest
markers. Running a pytest suite through the native command can turn a skip or xfail into an
ordinary execution, collapse parametrized cases, miss class-based tests, or fail fixture setup.
Use the explicit compatibility command until a suite has been intentionally migrated.

## Boundaries

`testenix pytest` does not use `[tool.testenix]`, the native collector, worker pool, retries,
timeouts, tags, history, event model, JSON reporter, or JUnit reporter. Pass the corresponding
pytest or plugin options after the subcommand. For example, use pytest's `--junitxml`, not
Testenix's native `--junit`.

Pytest and every required plugin must be installed beside the `testenix` executable in the same
interpreter environment. For uv-managed projects, prefer:

```console
$ uv add --dev "testenix[pytest]"
$ uv run testenix pytest -q tests
```

An isolated `uv tool install testenix` environment does not automatically see pytest plugins from
the project environment. Install the required packages into the tool environment or invoke
Testenix through the project environment instead.

After the handoff, pytest owns signal handling and returns any status chosen by pytest or a plugin,
unchanged. If pytest is missing, Testenix returns `2` with an installation hint before the handoff.
Failure to hand the process to pytest returns `3`.

## Performance and benchmark interpretation

Compatibility mode has pytest's execution performance plus launcher and adapter overhead, which has
not yet been measured separately. It is not expected to be faster than invoking `python -m pytest`
directly. The published Testenix benchmarks exercise the native `testenix run` engine and must not
be used to describe `testenix pytest`.

## Migration path

Use the bridge first to put Testenix in front of an unchanged suite without altering its semantics.
Migrate individual modules to native Testenix only when their pytest dependencies have explicit
equivalents:

1. keep the whole suite green with `testenix pytest`;
2. replace pytest fixtures with Testenix `@fixture` providers;
3. replace `parametrize` with `@case` or `@cases`;
4. replace pytest markers with Testenix tags, `@skip`, and `@xfail`;
5. run migrated modules with `testenix run` and leave the remainder on the bridge;
6. compare behavior before making native performance claims.

The current bridge does not convert pytest outcomes into a Testenix `RunResult`. Deeper event and
report aggregation is a future compatibility layer and requires explicit pytest hook integration.
