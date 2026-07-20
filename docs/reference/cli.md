# Command-line reference

## Top-level command

```text
testenix [-h] [--version]
testenix [--config PYPROJECT] run [RUN_ARGS ...]
testenix pytest [PYTEST_ARGS ...]
```

| Option | Description |
| --- | --- |
| `-h`, `--help` | Show command help. |
| `--version` | Print the installed Testenix version. |
| `--config PATH` | Load `[tool.testenix]` for native `testenix run`. |

## `testenix run`

```text
testenix run [PATH ...]
             [--config PYPROJECT]
             [-w auto|N]
             [--retries N]
             [--timeout SECONDS]
             [-t TAG ...]
             [--json FILE]
             [--junit FILE]
             [--history FILE | --no-history]
```

| Argument | Default | Description |
| --- | --- | --- |
| `PATH ...` | configured `paths`, otherwise `tests` | Files or directories to discover. |
| `-w`, `--workers` | `auto` | Worker process count or logical CPU count. |
| `--retries` | `0` | Additional attempts after a gating outcome. |
| `--timeout` | none | Global hard deadline for every selected test. |
| `-t`, `--tag` | none | Required tag; repeat for AND selection. |
| `--json` | none | Write the lossless JSON run result. |
| `--junit` | none | Write a JUnit XML report. |
| `--history` | `.testenix/history.sqlite3` | Override the duration-history database. |
| `--no-history` | off | Disable reading and writing history. |

CLI options override `[tool.testenix]` values for the current run.

## `testenix pytest`

```text
testenix pytest [PYTEST_ARGS ...]
```

This compatibility command hands the current CLI process to pytest from the same interpreter and
forwards every argument without translation. It preserves pytest configuration, collection,
fixtures, parametrization, markers, classes, hooks, plugins, output, and normal exit status.

```console
$ testenix pytest -q tests
$ testenix pytest tests/test_api.py::test_health -k smoke --maxfail=1
$ testenix pytest -n auto tests
$ testenix pytest --junitxml=reports/pytest.xml tests
```

Pytest and its plugins must be installed in the same Python environment as Testenix. The optional
`testenix[pytest]` extra installs a supported pytest (`>=8.3,<10`) when needed. Testenix does not
consume a leading `--`: pytest receives it unchanged, including its normal end-of-options meaning.

`[tool.testenix]` and native options such as `--workers`, `--retries`, `--timeout`, `--tag`,
`--json`, `--junit`, and `--history` do not affect this command. Pass pytest or plugin options
instead. In particular, `testenix --config PATH pytest ...` is rejected; `pytest` must immediately
follow `testenix`. See [pytest compatibility](../guides/pytest-compatibility.md) for the full
boundary.

## Examples

```console
$ testenix run
$ testenix run tests/unit tests/integration --workers 4
$ testenix run --tag unit --tag fast
$ testenix run --retries 1 --timeout 10
$ testenix run --json reports/run.json --junit reports/junit.xml
$ testenix --config config/pyproject.toml run
$ testenix pytest -q tests
```

## Exit codes

The native `testenix run` command uses these codes:

| Code | Meaning |
| ---: | --- |
| `0` | The run has no gating result. |
| `1` | A test failed, errored, timed out, crashed, became flaky, or unexpectedly passed. |
| `2` | Invalid CLI/configuration, collection error, or empty explicit tag selection. |
| `3` | Internal runner, reporter, or history failure. |
| `130` | User interruption. |

`testenix pytest` hands the current CLI process to pytest, so pytest or plugin exit statuses are
returned unchanged. Standard pytest statuses are `0` success, `1` test failure, `2` interruption,
`3` internal error, `4` usage error, and `5` no tests collected. Before the handoff, Testenix
returns `2` when pytest is missing and `3` when pytest cannot take over the process.
