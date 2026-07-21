# Parallel execution

Testenix treats local parallelism as part of the execution model rather than an optional plugin.

## Choose the worker count

```console
$ testenix run --workers auto
$ testenix run --workers 4
$ testenix run --workers 1
```

`auto` is adaptive; it is not an alias for the logical CPU count. Testenix first counts the
execution units it can actually schedule (module-affinity groups, isolated timeout tests, and any
eligible opt-in module shards). It caps the choice by that count and the available CPUs. With
reliable duration history, a startup-cost/makespan model chooses the smallest worker count within a
narrow tolerance of the predicted best. Without enough history, cold-start auto uses a conservative
cap so a short suite does not launch one process per CPU.

An explicit number remains a hard project setting. Prefer one in tightly budgeted CI; for a
project-specific measured value, use the tuner.

## Tune a project

```console
$ testenix tune tests --warmups 1 --repeats 5
$ testenix benchmark tests --candidates 1,2,4,8
$ testenix tune --json reports/tuning.json --write
```

`benchmark` is an alias for `tune`. The command runs fresh CLI processes, uses a green one-worker
inventory probe, disables history for all timing samples, measures native candidates in alternating
order, and rejects a candidate whose test inventory or outcomes differ. The recommendation is the
smallest worker count within a narrow tolerance of the best median, avoiding a larger setting for
measurement noise. `--write` stores that integer in
`[tool.testenix].workers`; it is an explicit file change, never an effect of `workers = "auto"`.
The automatic sweep respects the process-visible CPU capacity and tests at most 1/2/4 workers;
larger counts must be requested explicitly with `--candidates`.

Pass `--shard-modules` to tune that explicit mode and `--manifest FILE` to include the verified
single-import collection path in every native sample. When `--write` is present, Testenix refuses a
transient sharding or manifest override that differs from `[tool.testenix]`, because writing only
`workers` would persist a recommendation for a different execution profile. Configure the profile
first or tune it without `--write`.

Use `--pytest-source PATH` to add an optional pytest timing for a corresponding source suite. The
tuner's primary contract is worker selection for the native suite. Its optional pytest ratio is
orientation for that exact invocation, not sufficient evidence for a public speed claim; public
comparisons must also satisfy the [benchmarking contract](../benchmarking.md).

## Scheduling

Normal tests from one module form an affinity unit and execute in the same persistent worker. This
preserves module-fixture reuse and avoids splitting hidden module state between processes.

When duration history exists, Testenix schedules longer units first. This longest-processing-time
strategy is deterministic and reduces the chance that one slow shard becomes the tail of the run.

## Opt-in intra-module sharding

One large module normally exposes one unit no matter how many tests it contains. If its tests are
known to be independent, explicitly request finer units:

```console
$ testenix run tests --shard-modules
```

or configure:

```toml
[tool.testenix]
shard_modules = true
```

This is deliberately off by default. Before splitting a module, Testenix statically fails closed
when it detects module- or session-scoped fixtures, direct writes to module globals, mutation of
obvious module-level containers, or executable import-time lifecycle behavior. Function-scoped
fixtures, including autouse fixtures, may be recreated in separate workers and do not block
sharding. Eager calls in module assignments, annotations, decorators, function defaults, and class
bases or keywords are treated as import-time lifecycle behavior and keep the module intact.

Static analysis cannot prove that arbitrary calls, imported libraries, environment state, or
external services are free of shared effects. Enabling the option is therefore a project trust
decision. Validate the suite both with and without sharding before adopting it in CI. Modules that
fail the safety check retain normal module affinity while eligible modules can be split.

## Avoid the collection-side import

Without a manifest, safe supervised collection imports selected modules once, then execution
workers import their assigned modules again to materialize functions and case values. Projects for
which imports are a meaningful part of wall time can generate an explicit trusted manifest:

```console
$ testenix manifest tests --output .testenix/collection.json
$ testenix run tests --manifest .testenix/collection.json
```

The manifest records collection roots, the complete Python-source inventory and SHA-256 hashes,
test metadata, collection issues, and sharding decisions. Each run verifies the requested roots,
the exact file set, and every source digest before reusing it. Malformed manifest JSON is rejected
as invalid input. If a file was added or removed, a source changed, or current-source verification
cannot establish an exact match, the manifest is stale and Testenix falls back to the normal
isolated collection process. It does not execute a stale selection.

This optimization removes the collection-side import on an unchanged run; execution workers still
import the code they execute. It is not an implicit cache. If collection depends on environment
variables, generated files outside the selected roots, network state, or other inputs not captured
by source hashes, the producer must regenerate the manifest when those inputs change. Set
`manifest = ".testenix/collection.json"` in `[tool.testenix]` only when that trust boundary is
appropriate.

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


if __name__ == "__main__":
    asyncio.run(main())
```

Cancelling `run_async` terminates active collection and execution process trees before the
coroutine returns control.

## Platform note

On every supported platform, executable scripts that call `run()` or `run_async()` directly must
use the standard multiprocessing guard because the supervised worker protocol uses the `spawn`
start method:

```python
if __name__ == "__main__":
    main()
```

The `testenix` command-line program handles process startup itself.
