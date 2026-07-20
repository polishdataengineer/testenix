# Benchmarking contract

Testenix must be compared with both sequential pytest and pytest-xdist. Comparing only with sequential
pytest would overstate the value of built-in parallelism.

The benchmark suite will contain:

- 100, 1,000, and 10,000 no-op tests to measure discovery and framework overhead;
- tests with deliberately uneven durations to measure scheduler tail latency;
- module- and session-scoped fixture suites to measure fixture reuse;
- synchronous and asynchronous tests;
- passing, failing, skipped, xfailed, flaky, timed-out, and crashing tests;
- a worker-crash scenario that verifies every selected test reaches a terminal state;
- cold and warm history runs.

For every scenario record wall-clock duration, collection time, execution time, peak memory,
worker utilization, number of process starts, result completeness, and output size. Performance
claims require at least five runs per configuration and must publish the environment fingerprint.

The current v0.1 harness automates wall-clock samples and the environment fingerprint across 16
generated modules for a four-worker run. It also validates the completed test count, rotates runner
order between measured rounds, supports an explicit module count, and records throughput, mean, and
standard deviation. Pytest plugin autoloading and its cache provider are disabled, pytest-xdist is
loaded explicitly, and every tool runs from the generated suite directory so repository-level
pytest configuration does not affect the comparison. New output also records the commit, dirty
state, lockfile hash, timestamp, and installed framework versions. The remaining telemetry above is
the acceptance contract for the next harness iteration, not data claimed by the checked-in baseline
files.

Correctness wins over speed: a run with a missing, duplicated, or incorrectly finalized result is
invalid and excluded from performance comparisons.

Run the reproducible local harness with:

```bash
uv run python benchmarks/run_benchmark.py --tests 1000 --workers 4 --repeats 5
uv run python benchmarks/run_benchmark.py --tests 1000 --workers 4 --repeats 5 --uneven
uv run python benchmarks/run_benchmark.py --tests 10000 --modules 1000 --workers 4 --repeats 5
```

Maintainers can run the same comparison from GitHub's **Benchmarks** workflow and download its raw
JSON artifact. Shared GitHub runners are appropriate for reproducibility checks, not for silently
replacing the approved marketing baseline: their timing variance is outside this project's control.

The checked-in baseline files are development evidence, not universal performance claims. See
`docs/performance-analysis.md` for the current large-suite results, optimization profile, memory
notes, and native-code decision. Real project suites and cross-platform repetitions remain required
before publishing broad comparative claims.

An approved public baseline must be committed through a reviewed pull request. Do not remove slow
but valid samples as outliers; invalid commands remain evidence and must be explained. The current
checked-in 10,000- and 100,000-test files each contain five measured rounds, one warm-up, and clean
commit provenance. They remain single-machine synthetic evidence, so broader claims still require
the real-project and cross-platform scenarios above.
