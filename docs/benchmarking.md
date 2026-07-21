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

The checked-in headline files were recorded with Testenix 0.1.0 across 16 generated modules, four
workers, and `--no-history`. They automate subprocess wall-clock samples and the environment
fingerprint, validate the completed test count, rotate runner order between measured rounds, and
record throughput, mean, standard deviation, provenance, and raw samples. Pytest plugin autoloading
and its cache provider are disabled, pytest-xdist 3.8 is loaded explicitly with its default `load`
distribution, and every tool runs from the generated suite directory so repository-level pytest
configuration does not affect the comparison. Those historical records do not measure Testenix
0.2.1.

Schema-version 2 harness output additionally records the requested and resolved worker counts,
balanced/dominant/single-module test distributions, default-history versus `--no-history`, the
pytest-xdist distribution strategy, and captured stdout/stderr byte counts. Collection/execution
splits, peak memory, utilization, process counts, and the remaining telemetry above are still the
acceptance contract for a later harness iteration, not data claimed by the historical baseline.

Correctness wins over speed: a run with a missing, duplicated, or incorrectly finalized result is
invalid and excluded from performance comparisons.

## Evidence levels

1. **Historical synthetic baseline.** The committed `3.15×` figure is Testenix 0.1.0 on 100,000
   generated no-op tests, 16 modules, four workers, disabled history, and one M4 Pro. It remains
   useful provenance but must not be labelled as current-version performance.
2. **Current-version scaling matrix.** `run_scaling_matrix.py` requires the installed Testenix
   version to match `pyproject.toml` and refuses a dirty worktree by default. The dimension-sweep
   design covers 100, 500, 1,000, and 3,000 tests; 1, 2, 4, and `auto` workers;
   balanced/dominant/single-module layouts; and default history versus `--no-history`. The reference
   configuration changes one axis at a time. `--full-cross-product` is available when every
   combination is worth the substantially larger runtime. `auto` remains an adaptive Testenix
   request; the harness records the worker count observed in each Testenix sample. For the xdist
   side of that row, `auto` resolves separately to Python's logical CPU count and is labelled as
   such.
3. **Real-project evidence.** `run_project_benchmark.py` executes argument arrays from a local JSON
   manifest without a shell. Its result excludes source, stdout/stderr, environment values,
   absolute project paths, and Git remotes. Publication requires a successful migration report:
   the harness verifies its exact per-test inventory and outcomes, complete current Python-file
   inventories and hashes under directory source roots, the complete generated Python inventory,
   and that canonical `python -m pytest` / `python -m testenix run` commands point at the report's
   source and output roots. It records only aggregate timings/output sizes, digests, redacted Git
   state, and optional content fingerprints.

The 118-test project mentioned in the v0.2.0 release was a differential semantic-validation gate.
Its three timings were single observations, not a committed multi-round benchmark, and therefore do
not establish a real-project speedup.

## Project-local tuning is not a published benchmark

`testenix tune` and its `testenix benchmark` alias answer a narrow operational question: which
native worker count is fastest for this selected project suite on this machine now? They use fresh
CLI processes, disable history, validate a green one-worker inventory, alternate candidate order,
require native candidate inventory/outcome parity, and recommend the smallest worker count within
a narrow tolerance of the best measured median. This is appropriate for writing a local
`[tool.testenix].workers` value.

The optional `--pytest-source` measurement is orientation for a corresponding source suite. A
tuning report alone is not sufficient for marketing because a migrated native path and its pytest
source may differ in collection, wrappers, output, or configuration. Publishing a ratio still
requires all of this contract: equivalent inventories and outcomes, full-process wall time,
counterbalanced order, at least one warm-up and five measured rounds, command/version/environment
provenance, output sizes, and an explanation of history, sharding, manifest, and plugin settings.

For every published Testenix result, record both the requested and observed worker count. `auto` is
adaptive and must never be relabelled as the logical CPU count. Also record whether
`--shard-modules` was enabled and whether a trusted collection manifest was accepted or fell back
to supervised collection; either choice changes the amount and shape of schedulable work.

## Pytest compatibility bridge

Measurements of `testenix pytest` must be reported separately from native `testenix run`
measurements. The compatibility command hands the current interpreter to pytest through a POSIX
process overlay or pytest's in-process console entry point on Windows; it does not execute tests
through the Testenix engine.

A compatibility-overhead comparison must use the same interpreter, working directory,
environment, pytest configuration, plugins, and arguments for both `python -m pytest ...` and
`testenix pytest ...`. Any difference measures adapter overhead only and must not be presented as a
Testenix execution speedup. Native comparisons continue to use `testenix run` and must validate
that both runners execute the same tests and produce equivalent outcomes.

Run one reproducible synthetic scenario with:

```bash
uv run --no-editable python benchmarks/run_benchmark.py \
  --tests 1000 --modules 16 --workers 4 --repeats 5 --warmups 1 \
  --module-layout balanced --history-mode disabled --xdist-strategy load
```

Generate the current-version dimension sweeps from a clean checkout with:

```bash
uv run --no-editable python benchmarks/run_scaling_matrix.py \
  --output benchmarks/scaling_matrix_0_2_1.json
```

For an unpublished smoke test only, add `--quick --allow-dirty`. A publishable matrix must retain
five rounds, one warm-up, clean provenance, and the complete requested coverage.

Measure a real project without committing its code or private paths:

```bash
cp benchmarks/real_project_manifest.example.json /tmp/testenix-project-benchmark.json
# Edit expected counts, migration-report path, fingerprints, and runner commands.
uv run --no-editable python benchmarks/run_project_benchmark.py \
  --project /absolute/path/to/project \
  --manifest /tmp/testenix-project-benchmark.json \
  --output /tmp/testenix-project-result.json
```

Keep private manifests and results outside the repository until their labels, commit policy, and
fingerprints have been reviewed for publication. Environment override **values** are never written
to the result; only their key names are retained. Commands are recorded for reproducibility, so
secrets should stay in the environment. If an unavoidable command argument is sensitive, list its
zero-based index in that runner's `redact_arguments` field.

Without `migration_report`, the harness can still produce a private diagnostic, but it always sets
`publication_eligible` to `false`. A publishable run also requires the canonical module
entrypoints, a clean project, at least one warm-up and five measured rounds, explicit Testenix
workers and history mode, and installed-distribution identities for both pytest and Testenix. The
pytest version is probed inside the same project environment used by the timed commands. In each
runner command, place options before `--` and the exact benchmark suite roots after it. The harness
uses that delimiter to distinguish positional targets from values of options such as `-k` or
`--tag` and requires those targets to match the migration report exactly.

Maintainers can run the same comparison from GitHub's **Benchmarks** workflow and download its raw
JSON artifact. Shared GitHub runners are appropriate for reproducibility checks, not for silently
replacing the approved marketing baseline: their timing variance is outside this project's control.

The checked-in baseline files are historical development evidence, not universal or current-version
performance claims. See `docs/performance-analysis.md` for the large-suite results, optimization
profile, memory notes, and native-code decision. A clean Testenix 0.2.1 scaling matrix, publishable
real-project suites, alternative pytest-xdist strategies, and cross-platform repetitions remain
required before publishing broad comparative claims.

An approved public baseline must be committed through a reviewed pull request. Do not remove slow
but valid samples as outliers; invalid commands remain evidence and must be explained. The current
checked-in 10,000- and 100,000-test files each contain five measured rounds, one warm-up, and clean
commit provenance. They remain Testenix 0.1.0 single-machine synthetic evidence, so broader claims
still require the current-version, real-project, and cross-platform scenarios above.
