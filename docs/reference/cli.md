# Command-line reference

## Top-level command

```text
testenix [-h] [--version]
testenix [--config PYPROJECT] run [RUN_ARGS ...]
testenix pytest [PYTEST_ARGS ...]
testenix migrate FRAMEWORK PATH [PATH ...] [MIGRATION_ARGS ...]
testenix [--config PYPROJECT] tune [PATH ...] [TUNING_ARGS ...]
testenix [--config PYPROJECT] benchmark [PATH ...] [TUNING_ARGS ...]
testenix manifest PATH [PATH ...] --output FILE
```

| Option | Description |
| --- | --- |
| `-h`, `--help` | Show command help. |
| `--version` | Print the installed Testenix version. |
| `--config PATH` | Load `[tool.testenix]` for native `run` and `tune`/`benchmark` commands. |

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
             [--shard-modules]
             [--manifest FILE]
             [-q | -v | -vv]
             [--color {auto,always,never} | --no-color]
             [--show-skips]
             [--durations N]
```

| Argument | Default | Description |
| --- | --- | --- |
| `PATH ...` | configured `paths`, otherwise `tests` | Files or directories to discover. |
| `-w`, `--workers` | `auto` | Positive worker count, or adaptive selection from schedulable units, history, startup cost, and CPU capacity. |
| `--retries` | `0` | Additional attempts after a gating outcome. |
| `--timeout` | none | Global hard deadline for every selected test. |
| `-t`, `--tag` | none | Required tag; repeat for AND selection. |
| `--json` | none | Write the lossless JSON run result. |
| `--junit` | none | Write a JUnit XML report. |
| `--history` | `.testenix/history.sqlite3` | Override the duration-history database. |
| `--no-history` | off | Disable reading and writing history. |
| `--shard-modules` | off | Opt eligible modules into per-test execution units after conservative static safety checks. |
| `--manifest FILE` | none | Reuse an explicitly generated, source-verified trusted collection manifest. |
| `-q`, `--quiet` | off | Hide the run header and compact per-file table. Collection errors, failure details, and the final summary remain visible. |
| `-v`, `--verbose` | off | Print one result row per test in the stable detailed format. Repeat for `-vv`. |
| `-vv` | off | Add worker, attempt, and phase metadata, including captured output. |
| `--color auto\|always\|never` | `auto` | Select automatic ANSI styling, force styling, or disable it. |
| `--no-color` | off | Alias for `--color never`, useful in logs and snapshots. |
| `--show-skips` | off | Include reasons for skipped and expected-failure tests. |
| `--durations N` | none | List the `N` slowest tests before the summary; `0` lists every test. |

CLI options override `[tool.testenix]` values for the current run.

With no presentation flags, Testenix prints a compact row for each source file, complete
collection/failure diagnostics, and a final summary. Console output is assembled in stable source
order after execution completes; these modes do not promise live progress updates. Presentation
flags change only terminal rendering, not selection, scheduling, result statuses, JSON, JUnit, or
exit codes.

`workers = auto` never means “start one process per logical CPU.” Testenix caps it by the number of
units that can run independently. Reliable duration history feeds a makespan/startup-cost model;
cold runs use a conservative cap. The final console and JSON results report the worker count
actually used. An explicit integer remains useful for fixed CI resource limits and reproducible
published benchmarks.

`--shard-modules` is an explicit safety/performance trade-off. Modules with module/session
fixtures, statically visible mutable global state, or import-time lifecycle hazards keep module
affinity; eligible modules may be split. Static analysis cannot prove every dynamic side effect.

`--manifest` accepts the versioned JSON produced by `testenix manifest`. Malformed input is a usage
error. An otherwise valid manifest whose roots, complete Python-file inventory, or SHA-256 digests
no longer match is treated as stale, and the run safely performs ordinary supervised collection.

In `auto` color mode, Testenix requires a terminal, respects `NO_COLOR`, allows `FORCE_COLOR`, and
disables styling for a truthy `CI` value or `TERM=dumb`. Explicit `always` or `never` takes
precedence.

## `testenix pytest`

```text
testenix pytest [PYTEST_ARGS ...]
```

This compatibility command hands the current CLI process to pytest from the same interpreter and
forwards every argument without translation. It preserves pytest configuration, collection,
fixtures, parametrization, markers, classes, hooks, plugins, output, and normal exit status. The
compact native Testenix renderer is not involved: output is pytest's own output, just as with
`uv run pytest tests`. For a concise bridge invocation, use `testenix pytest -q --tb=short tests`.

```console
$ testenix pytest -q --tb=short tests
$ testenix pytest tests/test_api.py::test_health -k smoke --maxfail=1
$ testenix pytest -n auto tests
$ testenix pytest --junitxml=reports/pytest.xml tests
```

Pytest and its plugins must be installed in the same Python environment as Testenix. The optional
`testenix[pytest]` extra installs a supported pytest (`>=8.3,<10`) when needed. Testenix does not
consume a leading `--`: pytest receives it unchanged, including its normal end-of-options meaning.

`[tool.testenix]` does not configure this command. Arguments after `pytest` are never interpreted as
native Testenix options, even when their names overlap: `-q`, `-v`, `--color`, `--show-skips`,
`--durations`, `--workers`, and every other token are forwarded unchanged. They can therefore be
handled by pytest or a plugin, or rejected by pytest if unsupported. Pytest's equivalent spellings
include `-rs`, `--durations=N`, and `--color=yes|no|auto`; Testenix does not translate native
spellings. In particular,
`testenix --config PATH pytest ...` is rejected; `pytest` must immediately follow `testenix`. See
[pytest compatibility](../guides/pytest-compatibility.md) for the full boundary.

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

## `testenix tune` / `testenix benchmark`

```text
testenix tune [PATH ...]
              [--config PYPROJECT]
              [--candidates N[,N...]]
              [--warmups N]
              [--repeats N]
              [--pytest-source PATH] ...
              [--json FILE|-]
              [--shard-modules]
              [--manifest FILE]
              [--write]
```

`testenix benchmark` is an exact alias for `testenix tune`.

| Argument | Default | Description |
| --- | --- | --- |
| `PATH ...` | configured `paths`, otherwise `tests` | Native Testenix suite to tune. |
| `--candidates N[,N...]` | resource-aware 1/2/4 sweep | Positive worker counts to measure. Counts above the number of execution units are deduplicated at that limit; values above four require this explicit option. |
| `--warmups N` | `1` | Unrecorded warmups for each native candidate and optional pytest source. |
| `--repeats N` | `5` | Recorded samples for each candidate. Candidate order alternates between rounds. |
| `--pytest-source PATH` | none | Also time a corresponding pytest source path; repeat for multiple paths. |
| `--json FILE\|-` | none | Write the complete tuning report to a new file, or standard output with `-`. |
| `--shard-modules` / `--no-shard-modules` | configured value | Tune with explicit safe intra-module sharding or module affinity. |
| `--manifest FILE` | configured value | Use one source-verified collection manifest for every native sample. |
| `--write` | off | Persist the measured recommendation as `[tool.testenix].workers`. |

The command measures fresh CLI processes with Testenix history disabled. A one-worker probe
establishes the inventory and outcomes, and every native candidate must match them. To avoid
persisting noise, it recommends the smallest worker count within a narrow tolerance of the best
median. `--write` is the only configuration-mutating mode; normal tuning and adaptive auto do not
edit configuration. A workers-only write is rejected when a transient sharding or manifest override
differs from the loaded project configuration, because that recommendation would not describe the
persisted execution profile.

The optional pytest row is useful for local orientation only when it represents the corresponding
source suite. A public pytest/Testenix claim still needs equivalent inventories and outcomes,
counterbalanced commands, environment/version provenance, and the complete
[benchmarking contract](../benchmarking.md).

## `testenix manifest`

```text
testenix manifest PATH [PATH ...] --output FILE
```

The command performs supervised native collection and creates a new deterministic trusted
manifest. It records collection roots, the complete selected Python-source inventory and SHA-256
fingerprints, collected tests and issues, and conservative module-sharding decisions. `FILE` must
not already exist; Testenix does not silently replace a previous trust artifact.

Use it explicitly on later runs:

```console
$ testenix manifest tests --output .testenix/collection.json
$ testenix run tests --manifest .testenix/collection.json
```

This can avoid importing every selected module once for collection and once again for execution.
Execution workers still import the modules they run. Source verification cannot cover dynamic
collection inputs such as environment variables or external services; regenerate the manifest
when those inputs change.

## Examples

```console
$ testenix run
$ testenix run tests/unit tests/integration --workers 4
$ testenix run --tag unit --tag fast
$ testenix run --retries 1 --timeout 10
$ testenix run -v --show-skips --durations 10
$ testenix run --color never
$ testenix run --json reports/run.json --junit reports/junit.xml
$ testenix run tests --shard-modules
$ testenix manifest tests --output .testenix/collection.json
$ testenix run tests --manifest .testenix/collection.json
$ testenix tune tests --candidates 1,2,4,8 --warmups 1 --repeats 5
$ testenix benchmark tests --json reports/tuning.json
$ testenix tune --write
$ testenix --config config/pyproject.toml run
$ testenix pytest -q --tb=short tests
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
