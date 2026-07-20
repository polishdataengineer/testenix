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

## Scopes

```python
@fixture(scope="module")
def module_resource() -> Resource:
    return Resource()


@fixture(scope="session")
def worker_resource() -> Resource:
    return Resource()
```

| Scope | Lifetime in Testenix 0.1 |
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
