# Performance analysis

## Executive summary

The optimized v0.1 runner is faster than pytest and pytest-xdist in the checked-in synthetic
large-suite scenarios. The largest recorded comparison is 100,000 passing tests across 16 modules:
Testenix completed the suite in a median 11.957 seconds, pytest in 33.957 seconds, and pytest-xdist
in 44.322 seconds. Every measured command had to report the expected test count or the harness
rejected the sample. This 100,000-test result is preliminary because it has only three rounds, no
warmup, and a wide Testenix range; it does not yet satisfy the project's five-run publication
contract.

This is evidence for the tested workload and machine, not a universal claim about every Python
project. Import-heavy suites, fixture-heavy suites, slow tests, failure output, different operating
systems, and real repositories still need independent measurements.

## Environment and method

- Apple M4 Pro, 14 logical CPUs, 24 GiB RAM;
- macOS 26.5.1 arm64;
- CPython 3.11.14;
- four workers unless stated otherwise;
- generated test modules accepted by both Testenix and pytest;
- full process wall-clock time, including discovery, execution, aggregation, and console render;
- deterministic rotated execution order instead of always running Testenix last;
- test-count validation from each runner's final output;
- `--no-history` for the primary runner-overhead comparison.

The harness records every sample, median, mean, standard deviation, environment fingerprint, and
median throughput. Five repetitions are used for 10,000-test claims and three counterbalanced
repetitions for the more expensive 100,000-test comparison.

The checked-in baseline JSON files retain the provisional `PTF` identifier used when the raw
measurements were recorded. `PTF` and `Testenix` refer to the same pre-release v0.1 runtime; the
project was renamed before its first public release. Future benchmark runs use `Testenix`.

## Results

| Scenario | pytest | pytest-xdist | Testenix | Testenix advantage |
|---|---:|---:|---:|---:|
| 1,000 no-op tests, 16 modules | 0.479 s | 0.922 s | 0.435 s | 1.10x vs pytest |
| 10,000 no-op tests, 16 modules | 3.525 s | 4.523 s | 1.436 s | 2.45x vs pytest |
| 10,000 uneven tests, 16 modules | 3.990 s | 4.173 s | 1.687 s | 2.36x vs pytest |
| 10,000 no-op tests, 1,000 modules | 4.279 s | 5.175 s | 2.089 s | 2.05x vs pytest |
| 100,000 no-op tests, 16 modules | 33.957 s | 44.322 s | 11.957 s | 2.84x vs pytest |

The 100,000-test median throughputs were 8,363 tests/s for Testenix, 2,945 tests/s for pytest, and
2,256 tests/s for pytest-xdist. Testenix showed higher positional variance (10.53–16.70 seconds) than the
other runners, so the raw samples and standard deviation remain part of the checked-in baseline.

For 100,000 tests, the coordinator's measured maximum resident set was approximately 513 MiB after
the final manifest/event optimization. Sequential pytest measured approximately 520 MiB on the same
generated suite. These macOS `time` figures are process maxima, not aggregate memory across every
xdist/Testenix child process.

### Worker-count sensitivity

For 10,000 no-op tests across 16 modules, Testenix medians were:

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

Correctness was retained throughout: the framework's full resilience suite passes after every
optimization, including worker crashes, timeout process-tree cleanup, collection crashes/hangs,
async task leaks, fixture teardown attribution, cancellation, retries, and event replay.

## Rust decision

Rust is not the next highest-value optimization. Python imports, Python test and fixture bodies,
and CPython object interaction remain Python work. PyO3 can release the interpreter only while
performing Rust-only work; it cannot make arbitrary Python callbacks execute in parallel under the
GIL.

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

- collection, execution, IPC-byte, process-start, CPU, and aggregate-memory telemetry;
- 1,000/10,000/100,000 tests across 1, 16, 1,000, and 10,000 modules;
- synchronous and asynchronous fixtures, failures, captured output, timeouts, and retries;
- cold and warm SQLite history runs;
- real project suites and Linux/macOS/Windows CI runners;
- checkpoint-batched IPC followed by another profile;
- persistent workers that collect and execute without importing every module twice.

No universal “always faster than pytest” statement should be published until the real-project and
cross-platform gates pass. The supported claim today is narrower: Testenix is materially faster in the
measured large passing-suite scenarios while retaining supervised isolation and complete results.
