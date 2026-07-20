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

For the supported static subset, `testenix migrate pytest tests` can instead create a validated
native copy without modifying the source. [The safe migration guide](migration.md) defines its
strict support and rollback contract.

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

| Capability | `testenix pytest` | `migrate` then `run` | Direct `testenix run` |
| --- | --- | --- | --- |
| Plain module-level pytest-default `test*` functions | Yes, through pytest | Yes | `test_` or native `@test` |
| Simple pytest classes | Yes | Fresh-instance wrappers | No direct class collection |
| Complex class lifecycle/inheritance | Yes | Blocked | No |
| Simple local fixture | Yes | Converted, including static autouse | Only native `@fixture` |
| Session-scoped pytest fixture | Yes | Blocked: run-global vs worker-local | Native scope is worker-local |
| `tmp_path` | Yes | Native built-in | Native built-in |
| `monkeypatch` | Yes | `setattr`/`setenv` subset | Native reversible subset |
| Other built-in/dynamic fixture | Yes | Blocked | No pytest fixture semantics |
| Adjacent `conftest.py` fixture | Yes | Simple static subset | No automatic conftest discovery |
| Static single `parametrize` | Yes | Converted to cases | Use `@case` or `@cases` |
| Skip and plain custom marker | Yes | Converted | Use Testenix decorators/tags |
| Pytest xfail | Yes | Blocked due semantic differences | Use native `@xfail` intentionally |
| Pytest assertion rewriting | Yes | No | No |
| Plugins, hooks, pytest config | Yes | Blocked/not translated | No |
| Bare `@pytest.mark.asyncio` coroutine | According to plugin | Fresh function-scoped loop wrapper | Native async needs no plugin |
| Configured async/anyio plugin semantics | According to plugins | Blocked | No plugin semantics |
| Testenix worker scheduler/history | No | Yes after migration | Yes |
| Testenix retries and lossless results | No | Yes after migration | Yes |
| Published native speedups | No | Measure generated suite | Only documented workloads |
| Source preservation | Runs source | Source stays untouched | Not applicable |

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
Then let the conservative migrator identify and validate modules whose pytest dependencies have
explicit equivalents:

1. keep the whole suite green with `testenix pytest`;
2. run `testenix migrate pytest tests --dry-run` and inspect every diagnostic;
3. use `--check --report-json ...` to execute the source, serial native, and parallel native gates;
4. publish to a new directory only after all three inventories/outcomes agree;
5. keep unsupported modules on `testenix pytest` and keep all originals during rollout;
6. benchmark the generated directory before making a native performance claim.

The migrator replaces simple fixtures, parametrization, skip conditions, plain markers, bare
pytest-asyncio coroutine markers with a fresh closed loop per test or case, and the supported
built-in fixtures in the generated copy. It wraps simple pytest class methods with a fresh instance
per test. It never performs edits in place.
Manual rewrites are still necessary for blocked plugin, hook, complex class lifecycle, xfail, and
dynamic behavior.

The current bridge does not convert pytest outcomes into a Testenix `RunResult`. Deeper event and
report aggregation is a future compatibility layer and requires explicit pytest hook integration.
