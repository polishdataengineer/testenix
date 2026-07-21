# Configuration reference

Project defaults live in `[tool.testenix]` in `pyproject.toml`.

This table configures only the native `testenix run` engine. The `testenix pytest` compatibility
command delegates configuration to pytest and therefore uses `pytest.ini`, `pyproject.toml`
`[tool.pytest.ini_options]`, or arguments passed after the subcommand.

```toml
[tool.testenix]
paths = ["tests"]
workers = "auto"
retries = 0
# timeout = 10.0
tags = []
# shard_modules = true
# manifest = ".testenix/collection.json"
# json = "reports/testenix.json"
# junit = "reports/junit.xml"
history = ".testenix/history.sqlite3"
```

Unknown options are rejected instead of being silently ignored.

## Options

### `paths`

- Type: string or list of strings
- Default: `["tests"]`

Files and directories used when no positional path is passed to `testenix run`.

### `workers`

- Type: positive integer or `"auto"`
- Default: `"auto"`

`auto` is adaptive rather than equal to Python's logical CPU count. After collection and selection,
Testenix caps concurrency by the available CPUs and the number of independently schedulable units.
Reliable duration history feeds a worker-startup/makespan estimate; without enough history, a
conservative cold-start cap avoids oversubscribing short suites. The smallest worker count within a
narrow tolerance of the predicted best is selected.

An explicit number is recommended for strict CI resource limits and publishable benchmark runs.
Use `testenix tune` (or its `testenix benchmark` alias) to measure native candidates for this
project, and `testenix tune --write` to persist its recommendation explicitly.

### `retries`

- Type: non-negative integer
- Default: `0`

Number of user-requested attempts after a gating outcome. Earlier failures remain part of the
result, so a later pass is finalized as `FLAKY`.

### `timeout`

- Type: positive finite number in seconds
- Default: none

Global hard timeout for selected tests. Timed tests run in isolated processes.

### `tags`

- Type: string or list of strings
- Default: empty

Every configured tag must be present on a test for it to be selected. A comma-separated string and
a TOML list are both accepted.

### `json`

- Type: filesystem path or `null`
- Default: none

Location of the lossless JSON report. The programmatic field is `json_path`.

### `junit`

- Type: filesystem path or `null`
- Default: none

Location of the JUnit XML report. The programmatic field is `junit_path`.

### `history`

- Type: filesystem path, `false`, or `null`
- Default: `.testenix/history.sqlite3`

Duration-history database. Set it to `false` to disable history in TOML:

```toml
[tool.testenix]
history = false
```

The programmatic field is `history_path` and accepts a `pathlib.Path` or `None`.

### `shard_modules`

- Type: boolean
- Default: `false`

Allow eligible modules to be divided into finer per-test execution units. The static analyzer
keeps module affinity when it sees module/session fixtures, mutation of obvious module-global
state, or import-time lifecycle hazards. Function-scoped fixtures, including autouse fixtures, do
not block sharding.

This option is explicitly opt-in because static analysis cannot prove the absence of all dynamic
side effects. Validate the project with and without sharding before enabling it in CI.

### `manifest`

- Type: filesystem path or `null`
- Default: none

Path to a trusted collection manifest generated explicitly with:

```console
$ testenix manifest tests --output .testenix/collection.json
```

On each run Testenix verifies the requested collection roots, selected test files, statically
discoverable project-local Python import dependencies, and SHA-256 source digests. An exact match
bypasses the collection-side imports; a stale
but well-formed manifest falls back to normal supervised collection. Malformed JSON is rejected.
Execution workers still import the modules they run. Test parameter names remain available for
diagnostics, but their values are stored only as `<redacted>` to avoid persisting secrets obtained
during collection. Regenerate the manifest when source files or dynamic collection inputs change.

The programmatic field is `manifest_path` and accepts a `pathlib.Path` or `None`.

## Programmatic configuration

```python
from pathlib import Path

from testenix import TestenixConfig

config = TestenixConfig(
    paths=("tests/unit",),
    workers=4,
    retries=1,
    timeout=5.0,
    tags=("unit",),
    shard_modules=True,
    manifest_path=Path(".testenix/collection.json"),
    json_path=Path("reports/run.json"),
    history_path=None,
)
```

`TestenixConfig` is immutable. Use `with_overrides` to derive a validated copy.
