# Writing tests

Testenix tests are ordinary typed functions. Discovery recognizes `test_*` names, while decorators
add explicit metadata without wrapping or changing the callable.

## Plain functions

```python
def test_total() -> None:
    assert sum([2, 3, 5]) == 10
```

Both synchronous functions and coroutines are supported:

```python
async def test_async_client() -> None:
    response = await fetch_record(42)
    assert response["id"] == 42
```

## Descriptions, tags, and timeouts

```python
from testenix import test


@test(
    "the cache expires stale entries",
    tags={"unit", "cache"},
    timeout=1.5,
)
async def cache_expiry() -> None:
    ...
```

The decorator attaches immutable metadata and preserves the original function signature,
annotations, and traceback.

A timeout is a hard process deadline. This is more expensive than shared-worker execution, but it
can stop both a blocked coroutine and a blocking synchronous call.

## Parameter cases

Use `case` for named examples:

```python
from testenix import case, cases, test


@test("discount rules")
@cases(
    case("regular", amount=100, rate=0.0, expected=100),
    case("member", amount=100, rate=0.1, expected=90),
)
def discount(amount: int, rate: float, expected: float) -> None:
    assert amount * (1 - rate) == expected
```

Use keyword dimensions to create a Cartesian product:

```python
@cases(
    role=["admin", "editor"],
    active=[True, False],
)
def test_permissions(role: str, active: bool) -> None:
    ...
```

Case values are rebuilt when the worker rediscovers the module, so they do not need to cross the
process boundary through pickle. They must still be reproducible during module import.

## Skip and expected failure

```python
import sys

from testenix import skip, xfail


@skip("Windows-only behavior", when=sys.platform != "win32")
def test_windows_registry() -> None:
    ...


@xfail("tracked as issue #42")
def test_known_edge_case() -> None:
    assert current_behavior() == desired_behavior()
```

An expected failure becomes `XFAIL`. If the test unexpectedly passes, the result is `XPASS` and
gates the run.

## Retries and flakiness

Retries can be configured globally or on the command line:

```console
$ testenix run --retries 2
```

Every attempt remains in the result model. `FAIL -> PASS` is finalized as `FLAKY` and returns a
gating exit code; Testenix never hides the initial failure.

## Tags

Tags are normalized strings stored in the test specification:

```python
@test(tags={"integration", "database"})
def database_round_trip() -> None:
    ...
```

Run tests containing every requested tag:

```console
$ testenix run --tag integration --tag database
```

An explicit tag filter that selects no tests is treated as a usage error and exits with code `2`.
