# Configuration reference

Project defaults live in `[tool.testenix]` in `pyproject.toml`.

```toml
[tool.testenix]
paths = ["tests"]
workers = "auto"
retries = 0
# timeout = 10.0
tags = []
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

`auto` uses Python's logical CPU count. An explicit number is recommended for reproducible CI and
benchmark runs.

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
    json_path=Path("reports/run.json"),
    history_path=None,
)
```

`TestenixConfig` is immutable. Use `with_overrides` to derive a validated copy.
