# Fixtures

Fixtures are typed dependency providers. A test requests a fixture by using its name as a function
parameter.

## Return-value fixtures

```python
from testenix import fixture


@fixture
def customer_id() -> int:
    return 42


def test_customer(customer_id: int) -> None:
    assert customer_id > 0
```

## Cleanup with generators

Synchronous and asynchronous generator fixtures run cleanup after the consumer:

```python
from collections.abc import AsyncIterator

from testenix import fixture


@fixture
async def client() -> AsyncIterator[Client]:
    value = await Client.connect()
    try:
        yield value
    finally:
        await value.close()
```

Setup, call, and teardown are separate result phases. A teardown failure remains visible even when
the test body passed.

## Fixture dependencies

Fixtures can request other fixtures:

```python
@fixture
def database_url() -> str:
    return "sqlite:///:memory:"


@fixture
def repository(database_url: str) -> Repository:
    return Repository(database_url)


def test_empty_repository(repository: Repository) -> None:
    assert repository.count() == 0
```

The dependency graph is validated before execution. Missing fixtures and cycles become collection
issues instead of hanging the run.

## Built-in fixtures

Testenix 0.3 provides two dependency-free, test-scoped built-ins by name:

```python
from pathlib import Path


def test_isolated_file(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "value.txt"
    target.write_text("ok", encoding="utf-8")
    monkeypatch.setenv("TESTENIX_EXAMPLE", "enabled")
    assert target.read_text(encoding="utf-8") == "ok"
```

`tmp_path` is a fresh `pathlib.Path` removed during teardown. `monkeypatch` supports reversible
object/attribute and dotted-import `setattr`, `setenv`, and idempotent `undo`. Changes are restored
in LIFO order even when the test fails. Other pytest monkeypatch operations and pytest built-ins
such as `capsys`, `caplog`, and `request` are not native Testenix fixtures.

## Autouse fixtures

Use `autouse=True` when setup and cleanup must apply to every test that can see a fixture:

```python
@fixture(autouse=True)
def isolated_environment(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    yield
```

Explicitly requesting the same fixture still resolves one cached value for the test. A local
fixture definition overrides a visible imported definition with the same name before Testenix
chooses which fixtures run automatically.

## Scopes

```python
@fixture(scope="module")
def module_resource() -> Resource:
    return Resource()


@fixture(scope="session")
def worker_resource() -> Resource:
    return Resource()
```

| Scope | Lifetime in Testenix 0.3 |
| --- | --- |
| `test` | One instance for one concrete test attempt. |
| `module` | Shared by normal tests from the module inside one worker. |
| `session` | Shared by normal tests assigned to one worker process. |

Session scope is currently worker-local, not a single process-global instance. Timed tests run in
dedicated processes and therefore receive isolated module/session fixtures.

## Custom fixture names

```python
@fixture(name="api")
def build_client() -> Client:
    return Client()


def test_health(api: Client) -> None:
    assert api.health() == "ok"
```

The custom name changes dependency lookup without changing the Python function.
