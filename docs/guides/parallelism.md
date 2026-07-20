# Parallel execution

Testenix treats local parallelism as part of the execution model rather than an optional plugin.

## Choose the worker count

```console
$ testenix run --workers auto
$ testenix run --workers 4
$ testenix run --workers 1
```

`auto` resolves to the logical CPU count reported by Python. For CI and benchmark runs, an explicit
count makes resource use and results easier to reproduce.

## Scheduling

Normal tests from one module form an affinity unit and execute in the same persistent worker. This
preserves module-fixture reuse and avoids splitting hidden module state between processes.

When duration history exists, Testenix schedules longer units first. This longest-processing-time
strategy is deterministic and reduces the chance that one slow shard becomes the tail of the run.

## Process isolation

Workers are spawned processes. A worker streams each completed attempt to the coordinator before
starting the next test. If the worker later crashes:

- completed results remain authoritative;
- unfinished tests receive explicit crash or infrastructure outcomes;
- the crashed unit receives one framework recovery attempt;
- `CRASH -> PASS` is still finalized as `FLAKY`.

The coordinator never infers that a missing result passed.

## Hard timeouts

Any test with an explicit or global timeout runs in its own supervised process:

```python
from testenix import test


@test(timeout=2)
def test_external_tool() -> None:
    call_external_tool()
```

This boundary allows Testenix to terminate a blocking synchronous call as well as a stuck
coroutine. Descendant processes are terminated with the timed-out worker.

The trade-off is fixture reuse: a timed test cannot share module/session fixture instances with
neighbouring tests.

## Cancellation

The async library API is cancellation-aware:

```python
import asyncio

from testenix import TestenixConfig, run_async


async def main() -> None:
    result = await run_async(("tests",), TestenixConfig(workers=4))
    print(result.exit_code)


asyncio.run(main())
```

Cancelling `run_async` terminates active collection and execution process trees before the
coroutine returns control.

## Platform note

On Windows, scripts that call `run()` or `run_async()` directly must use the standard
multiprocessing guard:

```python
if __name__ == "__main__":
    main()
```

The `testenix` command-line program handles process startup itself.
