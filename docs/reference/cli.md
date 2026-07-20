# Command-line reference

## Top-level command

```text
testenix [-h] [--version] [--config PYPROJECT] {run} ...
```

| Option | Description |
| --- | --- |
| `-h`, `--help` | Show command help. |
| `--version` | Print the installed Testenix version. |
| `--config PATH` | Load `[tool.testenix]` from a specific pyproject file. |

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

## Examples

```console
$ testenix run
$ testenix run tests/unit tests/integration --workers 4
$ testenix run --tag unit --tag fast
$ testenix run --retries 1 --timeout 10
$ testenix run --json reports/run.json --junit reports/junit.xml
$ testenix --config config/pyproject.toml run
```

## Exit codes

| Code | Meaning |
| ---: | --- |
| `0` | The run has no gating result. |
| `1` | A test failed, errored, timed out, crashed, became flaky, or unexpectedly passed. |
| `2` | Invalid CLI/configuration, collection error, or empty explicit tag selection. |
| `3` | Internal runner, reporter, or history failure. |
| `130` | User interruption. |
