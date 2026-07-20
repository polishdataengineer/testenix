# Python API reference

The objects below are exported from `testenix` and form the public pre-1.0 API. Pre-1.0 releases
may still make documented breaking changes.

## Authoring

```{eval-rst}
.. autofunction:: testenix.test

.. autofunction:: testenix.fixture

.. autofunction:: testenix.case

.. autofunction:: testenix.cases
```

### `skip`

```python
@skip(reason_or_function=None, /, *, reason=None, when=True)
```

Mark a test as skipped. It can be used as `@skip`, `@skip("reason")`, or with a conditional
`when=...`.

### `xfail`

```python
@xfail(reason_or_function=None, /, *, reason=None, when=True)
```

Mark a test as expected to fail. An unexpected pass becomes the gating `XPASS` status.

```{eval-rst}
.. autoclass:: testenix.CaseDefinition
   :members:
```

## Discovery and execution

```{eval-rst}
.. autofunction:: testenix.discover

.. autofunction:: testenix.run

.. autofunction:: testenix.run_async

.. autoclass:: testenix.TestenixConfig
   :members:
```

## Result contracts

```{eval-rst}
.. autoclass:: testenix.RunResult
   :members:

.. autoclass:: testenix.TestResult
   :members:

.. autoclass:: testenix.TestSpec
   :members:

.. autoclass:: testenix.CollectionResult
   :members:

.. autoclass:: testenix.Status
   :members:

.. autoclass:: testenix.Scope
   :members:
```

## Migration

The programmatic API follows the same copy-only transaction as `testenix migrate`. `project_root`
defaults to the current directory. Prefer `dry_run=True` before a validating or publishing call.

```python
from pathlib import Path

from testenix import MigrationOptions, migrate

report = migrate(
    MigrationOptions(
        framework="auto",
        sources=(Path("tests"),),
        output=Path("tests_testenix"),
        check_only=True,
    )
)
assert report.originals_modified is False
```

```{eval-rst}
.. autofunction:: testenix.migrate

.. autoclass:: testenix.MigrationOptions
   :members:

.. autoclass:: testenix.MigrationReport
   :members:

.. autoclass:: testenix.MigrationStatus
   :members:

.. autoclass:: testenix.ValidationSummary
   :members:
```

## Events

```{eval-rst}
.. autoclass:: testenix.Event
   :members:

.. autoclass:: testenix.EventSink
   :members:
```
