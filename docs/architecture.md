# Testenix architecture

Testenix is a native Python testing framework. Its core does not depend on pytest.
Compatibility adapters may translate foreign test frameworks into the same manifest and event
contracts, but they are not part of native execution.

## Product contract

Testenix aims to be typed, async-native, parallel-first, deterministic, and lossless when reporting
test outcomes. A retry never overwrites an earlier attempt, infrastructure failures are distinct
from test failures, and setup/call/teardown are preserved as separate phases.

```text
Authoring API -> supervised collection -> inert manifest -> affinity scheduler -> process workers
                                                                  |          -> streamed attempts
                                                                  +----------> append-only events
                                                                              -> reducer
                                                                              -> reports/history
```

## Dependency rules

- `contracts` contains serializable domain values and imports no infrastructure.
- `api`, `discovery`, `fixtures`, and `executor` form the native engine.
- `events`, `aggregate`, and `scheduler` remain engine-independent.
- `runner` is the application service connecting the native engine with execution policy.
- reporters and storage consume completed domain results or versioned events.
- optional compatibility adapters depend inward; the core never imports them.

## Version 0.1 scope

- explicit `@test` and `@fixture` authoring API, plus conventional `test_*` discovery;
- sync functions, coroutines, generators, and async-generator fixture teardown;
- explicit cases, tags, skip, expected failure, and per-test timeout metadata;
- fixture scopes: test, module, session, with broader scopes currently bounded by a worker shard;
- sequential and local process execution;
- deterministic scheduling based on historical durations;
- append-only JSONL events and a pure reducer;
- console, JSON, and JUnit output plus local SQLite duration history;
- retries represented as immutable attempts and finalized as `FLAKY` when appropriate.

Remote workers, distributed storage, result caching, automatic quarantine, and a stable third-party
plugin SDK are deliberately outside version 0.1.

## Fixture scopes and process isolation

The scheduler treats every normal test module as one affinity unit and never splits that unit
between parallel shared workers. Multiple modules assigned to one shard execute in one persistent
process and fixture runtime. A test with an explicit timeout (including a global timeout applied at
selection) is instead a single-test isolation unit with a hard process deadline.

Scope therefore has the following concrete meaning in version 0.1:

| Scope | Lifetime |
| --- | --- |
| `test` | One instance for one concrete test attempt. |
| `module` | One instance for all normal attempts from that module in a shared worker; a timed test has an isolated instance. |
| `session` | One instance for all normal tests assigned to a shared worker process; one per isolated timed process. |

Session fixtures are intentionally not run-global. Suites that require a true global singleton
must keep that resource outside the fixture runtime until a coordinator-owned resource protocol is
implemented. Setting one worker gives one shared session for normal tests, but timed tests remain
isolated by design.

## Worker protocol and recovery

Workers receive only primitive rediscovery locators (path, function/case identity, and effective
timeout), so arbitrary case values never cross the spawn boundary. A worker streams each completed
`AttemptResult` before starting the next test and later republishes an owner if session teardown
changes its result. The final batch envelope is still sent for normal completion.

The supervisor drains the pipe while the child runs, avoiding pipe-buffer deadlocks for large
results. If a later test crashes the process, already streamed attempts remain authoritative and
only unfinished tests receive infrastructure/crash outcomes. A lost final ACK after all provisional
results is a teardown failure, never a green run. A process crash gets one automatic recovery
attempt without consuming the user's retry budget, but `CRASH -> PASS` remains `FLAKY`; only a
framework-owned `INFRA_ERROR -> PASS` recovery becomes plain `PASS`.

Collection itself uses the same spawn-based supervisor and returns only a JSON-safe manifest. A
top-level import crash or deadline becomes a `CollectionIssue`, so user import code cannot exit or
indefinitely block the coordinator. Timed execution units send a ready handshake after rediscovery;
the test deadline therefore does not include interpreter startup or module import.

Workers create a separate POSIX process session (or use recursive tree termination on Windows).
Timeout and cancellation terminate descendants started by a test as well as the direct worker.
