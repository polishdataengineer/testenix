# Published benchmark results

These tables are generated from the raw JSON committed in `benchmarks/`. They are development
evidence for specific synthetic workloads, not a universal claim that Testenix is always faster
than pytest.

![Preliminary Testenix throughput ratios](../_static/benchmark-speedup.svg)

## Median wall-clock time

Lower time is better. A speedup of `{benchmarks[0].speedup_vs_pytest:.2f}×` means pytest's median
wall time was {benchmarks[0].speedup_vs_pytest:.2f} times the Testenix median for that exact
scenario.

| Scenario | Testenix | pytest | pytest-xdist | vs pytest | vs xdist |
| --- | ---: | ---: | ---: | ---: | ---: |
| 10,000 no-op tests / 16 modules | 1.436 s | 3.525 s | 4.523 s | 2.46× | 3.15× |
| 10,000 uneven-duration tests / 16 modules | 1.687 s | 3.990 s | 4.173 s | 2.36× | 2.47× |
| 100,000 no-op tests / 16 modules | 11.957 s | 33.957 s | 44.322 s | 2.84× | 3.71× |

<div class="benchmark-caveat">
The 100,000-test result has only 3 measured rounds and
0 warmups. It is published for transparency, but it does not yet satisfy the
project's five-run, one-warmup minimum for a broad promotional claim.
</div>

## Environment

- CPU: not recorded in this legacy baseline (14 logical CPUs)
- Machine: `arm64`
- Platform: `macOS-26.5.1-arm64-arm-64bit`
- Python: `3.11.14`
- Measurement: complete subprocess wall-clock time, including discovery, execution, aggregation,
  and console rendering
- Correctness gate: every command had to exit successfully and report the expected test count


The raw JSON files retain the pre-release `ptf` runner identifier because the measurements were
recorded before the project was named Testenix. The performance analysis documents that provenance;
future approved baselines must use the `testenix` identifier and record their commit SHA.


## Raw samples and variance

### 10,000 no-op tests / 16 modules

- Testenix range: 1.388 s–1.712 s
- Testenix standard deviation: 0.131 s
- Testenix raw samples: 1.430, 1.549, 1.712, 1.388, 1.436 seconds
- pytest range: 3.447 s–3.829 s
- pytest standard deviation: 0.151 s
- pytest raw samples: 3.525, 3.829, 3.447, 3.487, 3.591 seconds
- pytest-xdist range: 4.127 s–4.801 s
- pytest-xdist standard deviation: 0.265 s
- pytest-xdist raw samples: 4.127, 4.732, 4.459, 4.523, 4.801 seconds
- Measured rounds: 5; warmups: 1
- Workers: 4

- [Raw JSON](https://github.com/polishdataengineer/testenix/blob/main/benchmarks/baseline.json)

### 10,000 uneven-duration tests / 16 modules

- Testenix range: 1.631 s–1.892 s
- Testenix standard deviation: 0.106 s
- Testenix raw samples: 1.892, 1.647, 1.687, 1.759, 1.631 seconds
- pytest range: 3.927 s–4.605 s
- pytest standard deviation: 0.282 s
- pytest raw samples: 3.927, 4.016, 4.605, 3.977, 3.990 seconds
- pytest-xdist range: 3.913 s–4.447 s
- pytest-xdist standard deviation: 0.215 s
- pytest-xdist raw samples: 3.913, 4.447, 4.243, 3.972, 4.173 seconds
- Measured rounds: 5; warmups: 1
- Workers: 4

- [Raw JSON](https://github.com/polishdataengineer/testenix/blob/main/benchmarks/baseline_uneven.json)

### 100,000 no-op tests / 16 modules

- Testenix range: 10.532 s–16.703 s
- Testenix standard deviation: 3.231 s
- Testenix raw samples: 16.703, 11.957, 10.532 seconds
- pytest range: 33.753 s–35.346 s
- pytest standard deviation: 0.867 s
- pytest raw samples: 35.346, 33.753, 33.957 seconds
- pytest-xdist range: 43.554 s–45.751 s
- pytest-xdist standard deviation: 1.115 s
- pytest-xdist raw samples: 43.554, 45.751, 44.322 seconds
- Measured rounds: 3; warmups: 0
- Workers: 4

- [Raw JSON](https://github.com/polishdataengineer/testenix/blob/main/benchmarks/baseline_100k.json)

## Interpretation

The checked-in results show that Testenix has low per-test overhead for large generated suites and
that its built-in process model is competitive with both sequential pytest and pytest-xdist in
those scenarios.

They do **not** yet answer how Testenix performs for import-heavy applications, complex fixture
graphs, assertion failures, real repositories, or different operating systems. Pytest also has a
far larger plugin and tooling ecosystem. Read the
[full performance analysis](../performance-analysis.md) for profiling details, memory notes,
implemented optimizations, and the Rust/PyO3 decision.

## Reproduce

Run the same harness from a locked development environment:

```console
$ uv sync --locked --dev --no-editable
$ uv run python benchmarks/run_benchmark.py --tests 10000 --workers 4 --repeats 5
$ uv run python benchmarks/run_benchmark.py --tests 10000 --workers 4 --repeats 5 --uneven
$ uv run python benchmarks/run_benchmark.py --tests 100000 --workers 4 --repeats 5
```

Review the [benchmarking contract](../benchmarking.md) before comparing or publishing new data.
