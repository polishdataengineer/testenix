# Testenix architecture

Testenix is a native Python testing framework. Its core does not depend on pytest.
Compatibility adapters may translate foreign test frameworks into the same manifest and event
contracts, but they are not part of native execution.

The first pytest compatibility bridge deliberately does not translate pytest internals. On POSIX,
`testenix pytest` uses `os.execv` to replace itself with `sys.executable -m pytest`. On Windows it
calls pytest's public `console_main` entry point in the existing process because the platform's
`exec` family does not provide equivalent replacement semantics. Both paths keep pytest in the
foreground CLI process with the same working directory, environment, terminal, streams, signal
handling, and exit status. This preserves semantics without making the native core depend on
pytest:

```text
POSIX:   testenix pytest ==exec==> Python -m pytest -> collector/plugins/executor -> output/status
Windows: testenix pytest =========> pytest.console_main -> collector/plugins/executor -> output/status
```

The bridge is a CLI infrastructure adapter, not a native collection adapter. It does not emit
Testenix events or construct a `RunResult` in version 0.2.

The migration adapter is separate from that handoff. It statically converts a deliberately small
pytest subset or generates SHA-pinned wrappers around the standard unittest protocol. Its
application service owns a fail-closed transaction:

```text
immutable source snapshot
        |--- original runner in project shadow --------------------> baseline summary
        |--- generated copy -> native 1-worker shadow ------------> serial summary
        |--- generated copy -> native parallel shadow ------------> parallel summary
        +--- per-mapping/count/outcome/hash parity -> no-replace rename -> new output
```

Converters consume serializable `SourceFile` values and return generated artifacts, mappings, and
line-addressed diagnostics. They do not access the live filesystem. `migration_fs` is the only
publication adapter: it rejects link traversal and overlapping paths, creates private same-filesystem
staging, rehashes the source, and uses an operating-system atomic no-replace primitive. POSIX
staging creation, writes, publication, and cleanup remain anchored to captured directory identities;
platforms without safe recursive descriptor deletion retain a non-empty failed staging tree. A
pre-rename failure has no destination to roll back because the source was never a write target. A
post-rename durability or audit-report warning is reported as published rather than as a fictional
rollback.

Unittest wrappers deliberately keep original `TestCase.run()` as the semantic authority. They are
native scheduler units, but resolve the original through an exact wrapper-relative path and load
the class only after verifying the complete selected-Python-source SHA-256 manifest.
This avoids approximating setup, teardown, cleanup, assertion, mocking, async, skip, and expected
failure behavior.

## Product contract

Testenix aims to be typed, async-native, parallel-first, deterministic, and lossless when reporting
test outcomes. A retry never overwrites an earlier attempt, infrastructure failures are distinct
from test failures, and setup/call/teardown are preserved as separate phases.

```text
Authoring API -> supervised collection -------> inert manifest -> scheduler -> process workers
                 ^                                  ^               |             -> streamed attempts
                 |                                  |               +------------> append-only events
trusted manifest +-- roots/inventory/SHA-256 verify                                -> reducer
       exact match bypasses collection imports                                     -> reports/history
```

## Dependency rules

- `contracts` contains serializable domain values and imports no infrastructure.
- `api`, `discovery`, `fixtures`, and `executor` form the native engine.
- `events`, `aggregate`, and `scheduler` remain engine-independent.
- `runner` is the application service connecting the native engine with execution policy.
- `tuning` models adaptive worker selection and runs explicit project-local candidate measurements.
- `sharding` contains fail-closed static module decisions and the versioned trusted-manifest
  serialization/verification boundary.
- reporters and storage consume completed domain results or versioned events.
- optional compatibility adapters stay at the CLI boundary; the native core never imports pytest.
- migration analyzers depend on serializable migration contracts, while shadow execution and
  atomic publication remain application/infrastructure concerns.

## Version 0.2 scope

- explicit `@test` and `@fixture` authoring API, plus conventional `test_*` discovery;
- sync functions, coroutines, generators, and async-generator fixture teardown;
- explicit cases, tags, skip, expected failure, and per-test timeout metadata;
- fixture scopes: test, module, session, with broader scopes currently bounded by a worker shard;
- sequential and local process execution;
- deterministic scheduling based on historical durations;
- adaptive `workers = "auto"` capped by real execution units, history-informed predicted makespan,
  process-start cost, and CPU capacity;
- explicit project-local worker tuning, conservative opt-in intra-module sharding, and an
  explicitly generated source-verified collection manifest;
- append-only JSONL events and a pure reducer;
- console, JSON, and JUnit output plus local SQLite duration history;
- retries represented as immutable attempts and finalized as `FLAKY` when appropriate;
- an optional platform-aware pytest handoff for unchanged legacy suites.
- conservative pytest/unittest migration with static diagnostics, differential validation, source
  fingerprints, and create-only publication.
- dependency-free `tmp_path` and reversible `monkeypatch` fixtures, plus native autouse resolution;
- conservative migration of bare pytest-asyncio coroutine markers through isolated fresh-loop
  wrappers, plus simple pytest classes.

Remote workers, distributed storage, result caching, automatic quarantine, and a stable third-party
plugin SDK are deliberately outside version 0.2.

## Fixture scopes and process isolation

By default, the scheduler treats every normal test module as one affinity unit and never splits
that unit between parallel shared workers. Multiple modules assigned to one shard execute in one
persistent process and fixture runtime. A test with an explicit timeout (including a global timeout
applied at selection) is instead a single-test isolation unit with a hard process deadline.

An explicit intra-module sharding policy can turn tests in an eligible module into finer units.
The static analysis fails closed for module/session fixtures, writes or obvious mutations of module
globals, and import-time lifecycle behavior. Function-scoped fixtures can be recreated per worker.
Because arbitrary dynamic calls and external effects cannot be proven safe, passing this policy is
a caller trust decision; ineligible modules keep normal affinity.

Scope therefore has the following concrete meaning in version 0.2:

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

A caller may instead supply a `TrustedCollectionManifest` created by an earlier explicit
collection. Before using it, Testenix enumerates the current roots and verifies the complete file
set and every SHA-256 digest. An exact match bypasses collection imports; a stale manifest falls
back to the supervised collector. Malformed serialized input is rejected at the adapter boundary.
Manifest parameter values are redacted at creation and serialization; only their names remain.
The manifest also carries prior sharding decisions so collection and scheduling agree. A module
using a fixture provider from outside its fingerprinted source fails closed to module affinity.
This removes
one module import per unchanged run, not the execution-worker import needed to reconstruct Python
objects. Inputs to dynamic collection beyond fingerprinted source bytes remain the producer's trust
responsibility.

Workers normally create a separate POSIX process session (or use recursive tree termination on
Windows). During migration validation they remain in the validator's process group so an outer
validation deadline can terminate native workers too. Timeout and cancellation terminate ordinary
descendants started by a test as well as the direct worker; this is process supervision, not an OS
sandbox against a test that deliberately detaches itself.
