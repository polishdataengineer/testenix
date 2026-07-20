# Reports and history

Every output adapter consumes the same immutable run result. A status cannot be green in the
console and red in JSON because both are derived from one model.

## Console

The console report is always enabled:

```console
$ testenix run tests
```

It prints test outcomes, failure details, and a final summary suitable for local development.

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
