# Reports and history

Every output adapter consumes the same immutable run result. A status cannot be green in the
console and red in JSON because both are derived from one model.

## Console

The console report is always enabled. Its default mode is compact: Testenix prints an aggregate row
for each source file, then complete failure details and the final summary.

```console
$ testenix run tests
Testenix  |  5 tests  |  2 files  |  2 workers

PASS  tests/test_accounts.py                         3 passed           [9ms]
FAIL  tests/test_checkout.py                         1 passed, 1 failed [12ms]

Problems (1)
FAIL      tests/test_checkout.py::test_rejects_expired_card
          attempt 1, call: expected status 402, got 200

5 tests, 4 passed, 1 failed in 0.091s
```

Run IDs and timings naturally vary. The report is assembled in stable source order when execution
finishes; it is not a live-progress display.

Choose the amount of terminal detail without changing execution semantics:

| Mode | Output |
| --- | --- |
| default | Run header, compact per-file table, collection/failure details, final summary. |
| `-q`, `--quiet` | No header or file table; collection/failure details and final summary remain. |
| `-v` | One stable result row per test, including duration. |
| `-vv` | Per-test rows plus worker, attempt, and phase metadata, including captured output. |

Skipped and expected-failure reasons are hidden unless they are requested explicitly:

```console
$ testenix run --show-skips tests
```

List the slowest tests with `--durations N`; use `0` to list every test. The duration section is
printed immediately before the final summary:

```console
$ testenix run --durations 10 tests
$ testenix run --durations 0 tests
```

ANSI styling defaults to `--color auto`, which considers the output terminal plus `NO_COLOR`,
`FORCE_COLOR`, `CI`, and `TERM=dumb`. Force it with `--color always`, or produce plain output with
either `--color never` or `--no-color`:

```console
$ testenix run --no-color tests
```

These flags affect the native `testenix run` console only. The transparent `testenix pytest`
bridge preserves pytest's renderer and argument meanings; a concise bridge command is
`testenix pytest -q --tb=short tests`.

## JSON

```console
$ testenix run --json reports/testenix.json
```

The JSON report preserves the complete hierarchy:

```text
run
└── test
    └── attempt
        ├── setup
        ├── call
        └── teardown
```

Consumers can distinguish failed assertions, setup errors, teardown errors, timeouts, crashes,
expected failures, unexpected passes, flaky retries, and framework infrastructure errors.

## JUnit XML

```console
$ testenix run --junit reports/junit.xml
```

JUnit XML is designed for CI systems that already understand the common test-report format.
Testenix-specific identifiers and statuses are retained as properties where JUnit has no exact
equivalent.

## Duration history

By default, duration history is stored in `.testenix/history.sqlite3`. Later runs use the estimates
to schedule longer work earlier.

Choose a custom location:

```console
$ testenix run --history .cache/testenix.sqlite3
```

Disable all history writes:

```console
$ testenix run --no-history
```

History changes scheduling estimates, not test semantics. A test is never skipped or treated as
passing because of a historical record.

## Configure report paths

```toml
[tool.testenix]
json = "reports/testenix.json"
junit = "reports/junit.xml"
history = ".testenix/history.sqlite3"
```

CLI options override these paths for the current run.

## Exit codes

| Code | Meaning |
| ---: | --- |
| `0` | No gating failure. |
| `1` | Test failure, error, timeout, crash, unexpected pass, or flaky result. |
| `2` | Collection, command, configuration, or empty explicit selection error. |
| `3` | Internal runner, report, or history error. |
| `130` | Interrupted by the user. |
