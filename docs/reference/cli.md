# Command-line reference

## Top-level command

```text
testenix [-h] [--version]
testenix [--config PYPROJECT] run [RUN_ARGS ...]
testenix pytest [PYTEST_ARGS ...]
testenix migrate FRAMEWORK PATH [PATH ...] [MIGRATION_ARGS ...]
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

## `testenix migrate`

```text
testenix migrate {auto,pytest,unittest} PATH [PATH ...]
                 [-o OUTPUT]
                 [-w auto|N]
                 [--validation-timeout SECONDS]
                 [--dry-run | --check]
                 [--report-json FILE|-]
```

| Argument | Default | Description |
| --- | --- | --- |
| `FRAMEWORK` | required | `pytest`, `unittest`, or `auto` for separate modules of both kinds. |
| `PATH ...` | required | Source files or directories. Sources are never modified. |
| `-o`, `--output` | `testenix_migrated` | New directory to publish; it must not exist. |
| `-w`, `--workers` | `auto` | Worker count for parallel candidate validation; an integer must be at least `2`. |
| `--validation-timeout` | `300` | Independent deadline for each source/native subprocess. |
| `--dry-run` | off | Static analysis and source-hash check only; run and publish nothing. |
| `--check` | off | Full differential validation, with no published output. |
| `--report-json` | none | New audit path inside the project and outside source/output suites, or `-` for clean JSON on standard output. |

Without `--dry-run` or `--check`, migration requires a green source baseline, an equal native
serial result, and an equal native parallel result, including every mapped test outcome. It then rechecks source hashes and atomically
renames a complete staging directory to the new output without replacement. A failure before that
rename leaves the output absent. A report-only failure after publication warns but leaves the
validated output and successful exit status intact. See [safe migration](../guides/migration.md) for supported constructs, unittest's
SHA-pinned wrapper model, rollback guarantees, and external-side-effect boundaries.

Human-readable output distinguishes an analyzed candidate, a validated candidate, a generated
candidate, a statically convertible subset, and a published conversion. Repeated diagnostics are
grouped by severity and code, with the first source location shown. Use `--report-json FILE` or
`--report-json -` when every individual line-addressed diagnostic is required. The JSON field
`converted_tests` counts source-to-target mappings built in memory; only `status: published` and
`published: true` mean that an output directory was created.

`MIG006` warns that a candidate has only one schedulable module affinity unit despite a parallel
worker setting. It is emitted only for `--check` or publication after static analysis succeeds,
because dry-run and unsupported transactions never execute the parallel gate.

## Examples

```console
$ testenix run
$ testenix run tests/unit tests/integration --workers 4
$ testenix run --tag unit --tag fast
$ testenix run --retries 1 --timeout 10
$ testenix run --json reports/run.json --junit reports/junit.xml
$ testenix --config config/pyproject.toml run
$ testenix pytest -q tests
$ testenix migrate pytest tests --dry-run
$ testenix migrate auto tests --check --report-json reports/migration.json
$ testenix migrate unittest tests --output tests_testenix
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

The `testenix migrate` command uses these codes:

| Code | Meaning |
| ---: | --- |
| `0` | Analysis, check, or publication succeeded; also retained after a report-only failure following publication. |
| `1` | Source/candidate execution failed, timed out, or differed. |
| `2` | Unsafe path, existing output, source drift, or invalid migration usage. |
| `3` | Internal process or report failure before publication. |
| `4` | An unsupported construct was found; nothing was published. |
| `130` | User interruption. |

`testenix pytest` hands the current CLI process to pytest, so pytest or plugin exit statuses are
returned unchanged. Standard pytest statuses are `0` success, `1` test failure, `2` interruption,
`3` internal error, `4` usage error, and `5` no tests collected. Before the handoff, Testenix
returns `2` when pytest is missing and `3` when pytest cannot take over the process.
