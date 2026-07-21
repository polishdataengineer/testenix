# Performance analysis

## Executive summary

The checked-in headline numbers are a **historical Testenix 0.1.0 synthetic baseline**, not current
Testenix 0.3.0 results. In the largest recorded comparison, 100,000 generated no-op tests were spread
evenly across 16 modules and run with four workers and `--no-history`. Testenix completed the suite
in a median 8.038 seconds, pytest in 25.333 seconds, and pytest-xdist 3.8's default `load` scheduler
in 21.300 seconds. Every command had to report the expected test count or the harness rejected the
sample. The baseline contains one warm-up and five counterbalanced measured rounds, and Testenix's
samples ranged from 7.912 to 8.096 seconds.

This is evidence for the tested workload and machine, not a universal claim about every Python
project. Import-heavy suites, fixture-heavy suites, slow tests, failure output, default history,
alternative pytest-xdist schedulers, different operating systems, and real repositories still need
independent measurements. No clean Testenix 0.3.0 scaling matrix is checked in yet, so `3.15×` must
not be presented as a 0.3.0 speedup.

These results do not apply to `testenix pytest`. The compatibility command delegates to pytest and
has pytest execution performance plus launcher and adapter overhead, which has not yet been
measured separately.

The pre-v0.2 safe-migration baseline is deliberately mixed rather than uniformly positive. For 3,000
no-op pytest tests across 64 modules, the generated native suite was 2.96x faster than sequential
pytest. Empty unittest wrappers were 7.40x slower than the sequential stdlib-based outcome probe,
while the same layout with 1 ms of work in each method was 1.58x faster under four native workers. These are
synthetic, sequential-source comparisons; they do not establish an advantage over pytest-xdist,
parallel unittest runners, or a real repository.

## Environment and method

- Apple M4 Pro, 14 logical CPUs, 24 GiB RAM;
- macOS 26.5.1 arm64;
- CPython 3.11.14;
- four workers unless stated otherwise;
- generated test modules accepted by both Testenix and pytest;
- full process wall-clock time, including discovery, execution, aggregation, and console render;
- deterministic rotated execution order instead of always running Testenix last;
- test-count validation from each runner's final output;
- disabled pytest plugin autoloading and cache, with pytest-xdist loaded explicitly;
- pytest-xdist's default `load` distribution rather than `loadfile`, `loadscope`, or `worksteal`;
- generated-suite working directory, isolated from repository-level pytest configuration;
- `--no-history` for the primary runner-overhead comparison.

The harness records every sample, median, mean, standard deviation, environment fingerprint, and
median throughput. All checked-in comparison files use one warm-up and five measured repetitions.
They also record the clean source commit, lockfile hash, timestamp, CPU model, and installed
Testenix, pytest, and pytest-xdist versions.

## Historical Testenix 0.1.0 results

| Scenario | pytest | pytest-xdist | Native Testenix | Native Testenix advantage |
|---|---:|---:|---:|---:|
| 10,000 no-op tests, 16 modules | 2.477 s | 2.106 s | 0.869 s | 2.85x vs pytest |
| 10,000 uneven tests, 16 modules | 3.076 s | 2.138 s | 1.345 s | 2.29x vs pytest |
| 100,000 no-op tests, 16 modules | 25.333 s | 21.300 s | 8.038 s | 3.15x vs pytest |

The 100,000-test median throughputs were 12,440 tests/s for Testenix 0.1.0, 3,947 tests/s for pytest, and
4,695 tests/s for pytest-xdist. The raw five-sample ranges and standard deviations are published in
the generated benchmark page and the checked-in JSON files.

In a separate exploratory 100,000-test profile, the coordinator's measured maximum resident set was
approximately 513 MiB after the final manifest/event optimization. Sequential pytest measured
approximately 520 MiB on the same generated suite. These older macOS `time` figures are not part of
the current baseline JSON and are process maxima, not aggregate memory across every xdist/Testenix
child process. The console renderer changed substantially after these captures, and the historical
harness did not record output byte counts. Current schema-version 2 runs do record stdout/stderr
sizes, but a clean 0.3.0 matrix is still pending.

### Migrated-suite measurements

The migration harness generates a source suite, migrates and validates it without modifying any
source SHA-256, then measures the original source runner and the published native copy in
alternating rounds. All three checked-in scenarios contain 3,000 tests across 64 modules, use one
warm-up and five measured rounds, and run native Testenix with four workers on the M4 Pro/CPython
3.11 environment described above.

| Source runner | Test body | Source median | Native Testenix median | Native vs source | Migration transaction |
|---|---|---:|---:|---:|---:|
| sequential pytest | no-op | 1.539 s | 0.521 s | 2.96x faster | 5.940 s |
| sequential unittest outcome probe | no-op | 0.161 s | 1.192 s | 7.40x slower | 6.742 s |
| sequential unittest outcome probe | 1 ms sleep | 4.066 s | 2.577 s | 1.58x faster | 17.251 s |

The transaction duration is reported separately from recurring execution. It includes generation,
the source baseline, serial and parallel native candidates, parity checks, source rehashing, and
atomic publication. The 1 ms unittest transaction is longer because validation executes the
synthetic work repeatedly. Conversion is therefore an occasional safety cost, not a per-CI-run
speedup input.

The no-op unittest row exposes the adapter's fixed cost: each native test is a wrapper that loads
the unchanged source and translates the result of `TestCase.run()`. The sequential source probe
uses the stdlib loader and result semantics, then serializes per-test outcomes; it wins
when the test body does essentially nothing. With 1 ms per method, four-worker execution across 64
modules amortizes that cost and overtakes the sequential source runner. By default, a suite
concentrated in one module exposes only one affinity unit; opt-in safety-checked sharding may change
that for an independent module. Too many workers can still add process and import overhead. Test
duration, module distribution, imports, fixtures, I/O, failures, operating system, and competing
parallel runners must all be measured on the target project.

The generated [benchmark results](benchmarks/results.md) publish every sample, range, standard
deviation, command, environment field, and raw JSON link. These three synthetic records are useful
for finding overhead boundaries; they are not evidence that converted suites are universally
faster.

### The 118-test validation was not a benchmark

The [v0.2.0 release notes](https://github.com/polishdataengineer/testenix/releases/tag/v0.2.0)
reported one final validation observation from a real 118-test pytest project: 3.120 seconds for
pytest, 2.870 seconds for native serial execution, and 2.423 seconds for native parallel execution.
Those values correspond to roughly 1.09× and 1.29× for that observation, not 3.15×. They had no
committed raw rounds, warm-up series, counterbalanced order, or publishable environment manifest,
so their role was outcome parity (118/118 in all modes), not performance marketing.

A small real suite can differ sharply from the 100,000-test no-op baseline. Interpreter spawn and
application import costs are a much larger fraction of its wall time; fixtures, mocks, files,
databases, and actual test bodies dominate framework overhead; and module affinity prevents one
large module from being split between workers. `workers=auto` is adaptive in current Testenix and
must be recorded as the worker count actually observed, not assumed to equal logical CPU count.
For a demonstrably independent large module, opt-in `--shard-modules` can create finer units after
static safety checks; it is not a safe default for arbitrary module state. Use `testenix tune` for
a local worker recommendation, then use the real-project manifest harness and publish five
counterbalanced rounds before drawing a project-specific conclusion.

### Worker-count sensitivity

An earlier exploratory run of 10,000 no-op tests across 16 modules produced these Testenix medians:

| Workers | Testenix median |
|---:|---:|
| 1 | 1.926 s |
| 4 | 1.483 s |
| 14 | 1.773 s |

Four workers were best for this short-test workload. This does not imply a universal four-worker
optimum: long CPU-bound tests can benefit from more processes. Adaptive worker selection should use
measured history rather than a fixed cap tuned to one machine.

### History and event-log cost

The original implementation coupled SQLite duration history to an event sink that opened, locked,
wrote, and closed its JSONL file for every event. A 10,000-test default-history run took 10.97
seconds. Keeping the descriptor open, avoiding internal fanout serialization, and storing one
self-contained attempt event instead of redundant post-hoc phase events reduced an equivalent run
to 1.98–2.30 seconds. Replay files still contain every test specification, complete attempt phases,
and final status.

## Profile and implemented optimizations

The first 10,000-test profile performed approximately 25.9 million Python calls. It spent large
fractions of coordinator time serializing the same 80,000 events more than once, reducing those
events again, resolving paths per test, and waiting for duplicated IPC payloads.

The optimized profile performed approximately 3.46 million calls. The main changes were:

1. Untimed synchronous tests reuse an executor thread instead of creating one OS thread per test.
   Timed tests retain a daemon-thread plus hard process deadline, so a stuck Python thread cannot
   block worker termination.
2. A successful worker streams each completed result for crash recovery and sends only a final ACK;
   it no longer sends the entire result tuple a second time.
3. Contract paths are computed once per module, source lines use `co_firstlineno`, and worker
   rediscovery builds an O(1) test-ID index instead of doing an O(m²) sequence of linear lookups.
4. The one-sink event path avoids JSON fanout. Duplicate event serialization is lazy and only runs
   when an event ID actually repeats.
5. Event IDs use the already unique `run_id:sequence` form instead of calling the OS random source
   tens of thousands of times.
6. Coordinator attempts are persisted as one complete replay event rather than a redundant
   started/three-phase/finished sequence emitted only after the attempt had already finished.
7. Unchanged selected specifications reuse discovered objects; selection events are omitted when
   every test is selected and no effective contract changed.
8. JSONL keeps one append descriptor for the run rather than performing open/close per event.
9. An explicit trusted collection manifest can remove the collection-side import from later
   unchanged runs. Roots, the complete file inventory, and every source SHA-256 are verified first;
   stale manifests fall back to supervised collection. Execution still imports assigned modules.

Correctness was retained throughout: the framework's full resilience suite passes after every
optimization, including worker crashes, timeout process-tree cleanup, collection crashes/hangs,
async task leaks, fixture teardown attribution, cancellation, retries, and event replay.

## Rust decision

Rust is not the next highest-value optimization. Python imports, Python test and fixture bodies,
and CPython object interaction remain Python work. PyO3 can release the interpreter only while
performing Rust-only work; it cannot make arbitrary Python callbacks execute in parallel under the
GIL.

The migration path does not change that decision. Parsing Python ASTs, hashing and copying files,
and publishing a new directory happen only when regenerating a suite, while most validation time
comes from executing the source and candidate Python tests. A Rust converter would not shorten a
1 ms Python test body or `unittest.TestCase.run()`. The empty-unittest result instead points to
profiling wrapper loading and result adaptation, then optimizing or caching those Python-level
boundaries without weakening SHA verification and fail-closed publication.

Direction-finding microbenchmarks on this machine showed:

| Candidate | Current signal | Rust/PyO3 result | Decision |
|---|---:|---:|---|
| LPT scheduling | ~2.1 ms for 1,000 synthetic units; real plan usually 16 units | bulk Rust loop ~17.5x faster | Absolute saving is below 2 ms; do not add native packaging for it |
| Event build + reduce | ~32 ms per 1,000 tests in a light microbenchmark | likely reducible in bulk | Revisit only if it exceeds 10% of final wall time |
| JSON event encoding | ~83 ms for ~8,000 detailed events | plausible 2–5x native gain | Relevant mainly to log-heavy mode; Python event compaction already recovered more |
| Pipe transport | 36.7 ms for 1,000 individual messages vs 12.2 ms as one batch | native framing could help | Batch/checkpoint in Python first |
| Sync invocation | 40–57 ms per 1,000 `to_thread` calls | Python body still crosses the GIL | Requires executor architecture, not Rust |
| Python list/record conversion | FFI conversion dominated the operation | PyO3 was slower than Python built-ins | Avoid fine-grained FFI calls |

If a later profile identifies a native-worthy data plane, the preferred design is an optional
PyO3 `abi3` extension with one bulk call per batch/run and a pure-Python fallback. Candidate scope:
framed IPC encoding/decoding or a bulk event reducer. Acceptance requires more than 10% end-to-end
improvement on Linux, macOS, and Windows with identical terminal results. A Rust sidecar or embedded
Python runtime is not justified: both retain Python interpreter startup/import costs while adding
another protocol and a much larger release matrix.

Relevant upstream constraints are documented in the
[PyO3 parallelism guide](https://pyo3.rs/main/parallelism),
[PyO3 performance guide](https://pyo3.rs/main/performance.html), and
[Maturin mixed-project guide](https://www.maturin.rs/project_layout.html).

## Next measurement gates

- publish the clean Testenix 0.3.0 dimension-sweep matrix for 100/500/1,000/3,000 tests,
  balanced/dominant/single-module layouts, 1/2/4/adaptive-auto workers, and both history modes;
- compare pytest-xdist `load`, `loadfile`, `loadscope`, and `worksteal` where each strategy is valid;
- collection, execution, IPC-byte, process-start, CPU, and aggregate-memory telemetry;
- 1,000/10,000/100,000 tests across 1, 16, 1,000, and 10,000 modules;
- synchronous and asynchronous fixtures, failures, captured output, timeouts, and retries;
- cold and warm SQLite history runs;
- real project suites and Linux/macOS/Windows CI runners;
- real migrated pytest and unittest suites, including sequential and established parallel source
  runners, multiple module layouts, and a break-even curve by median test duration;
- checkpoint-batched IPC followed by another profile;
- measure trusted-manifest hit and stale-fallback paths on import-heavy real projects, and consider
  a persistent collect-and-execute worker only if the remaining execution import is still material.

No universal “always faster than pytest” statement should be published until the current-version,
real-project, and cross-platform gates pass. The supported claim today is historical and narrower:
Testenix 0.1.0 was materially faster in the recorded native large passing-suite scenarios while
retaining supervised isolation and complete results. Testenix 0.3.0 has no checked-in speedup claim
yet.
