# Published benchmark results

These tables are generated from the raw JSON committed in `benchmarks/`. They are development
evidence for specific synthetic workloads, not a universal claim that Testenix is always faster
than pytest. `Testenix` in these results means the native `testenix run` engine. The
`testenix pytest` compatibility bridge delegates to pytest and is not represented here.

## Testenix 0.3.0 scaling matrix

No current-version matrix is checked in yet. The historical results below must therefore not be
described as Testenix 0.3.0 performance. The new provenance-gated harness covers
100/500/1,000/3,000 tests, balanced/dominant/single-module layouts, 1/2/4/auto workers, and both
default history and `--no-history`, plus explicit safe-module sharding. Its default design uses
dimension sweeps; use
`--full-cross-product` only when the much larger run is intentional.

`auto` is passed literally to Testenix and remains adaptive; observed Testenix worker counts are
stored per sample. pytest-xdist resolves its side of an `auto` row separately to the machine's
logical CPU count.

```console
$ uv run --no-editable python benchmarks/run_scaling_matrix.py \
    --output benchmarks/scaling_matrix_0_3_0.json
```

The command refuses a dirty worktree or an installed Testenix version that differs from
`pyproject.toml`. `--allow-dirty` is available only for unpublished smoke runs. A matrix becomes
publishable here only after five measured rounds, one warm-up, clean commit provenance, and full
axis coverage pass the documentation generator's validation.


## Real-project harness

The 118-test project used during v0.2 migration validation was a semantic parity gate, not a
publishable benchmark: its release-note timings were single observations without a committed
multi-round record. Use the redaction-safe manifest harness for a real repository:

```console
$ cp benchmarks/real_project_manifest.example.json /tmp/testenix-project-benchmark.json
$ uv run --no-editable python benchmarks/run_project_benchmark.py \
    --project /absolute/path/to/project \
    --manifest /tmp/testenix-project-benchmark.json \
    --output /tmp/testenix-project-result.json
```

The manifest stores argument arrays, never shell fragments. The result omits stdout, stderr,
environment values, absolute project paths, and private source. It records only timings, aggregate
output sizes, optional tree fingerprints, and redacted Git provenance. A migrated-suite comparison
must point the manifest at a successful migration report to become publication-eligible. The
harness verifies the report's exact per-test inventory and outcomes, complete source and generated
Python-file inventories, current hashes, and binds canonical `python -m pytest` /
`python -m testenix run` commands to the report's source/output roots. Publishable source roots are
directories so support files such as `conftest.py` are covered. Without the report the result is
diagnostic-only. Commands are retained for
reproducibility. Publishable commands put options before `--` and exact suite targets after it, so
an option value cannot impersonate a migration root. Keep secrets in the environment or list
sensitive argument indexes in `redact_arguments`.


## Historical Testenix 0.1.0 synthetic baseline

The checked-in `3.15×` figure is a Testenix 0.1.0 result for 100,000 generated no-op
tests across 16 modules, four workers, disabled history (`--no-history`), and pytest-xdist's default
`load` strategy. It is retained as transparent historical evidence; it is not a measurement of
Testenix 0.3.0.

![Historical Testenix 0.1.0 throughput ratios](../_static/benchmark-speedup.svg)

### Median wall-clock time

Lower time is better. A speedup of `2.85×` means pytest's median
wall time was 2.85 times the Testenix median for that exact
scenario.

| Scenario | Testenix | pytest | pytest-xdist | vs pytest | vs xdist |
| --- | ---: | ---: | ---: | ---: | ---: |
| 10,000 no-op tests / 16 modules | 0.869 s | 2.477 s | 2.106 s | 2.85× | 2.42× |
| 10,000 uneven-duration tests / 16 modules | 1.345 s | 3.076 s | 2.138 s | 2.29× | 1.59× |
| 100,000 no-op tests / 16 modules | 8.038 s | 25.333 s | 21.300 s | 3.15× | 2.65× |

<div class="benchmark-caveat">
The 100,000-test result meets the project's local five-run, one-warmup minimum.
It remains a synthetic result from one machine, not a universal performance promise.
</div>

### Environment and controls

- CPU: Apple M4 Pro (14 logical CPUs)
- Machine: `arm64`
- Platform: `macOS-26.5.1-arm64-arm-64bit`
- Python: `3.11.14`
- Testenix: `0.1.0`
- Workers: four for Testenix and pytest-xdist
- Testenix history: disabled with `--no-history`
- pytest-xdist: version `3.8.0`,
  default `load` distribution
- Measurement: complete subprocess wall-clock time, including discovery, execution, aggregation,
  and console rendering
- Correctness gate: every command had to exit successfully and report the expected test count



### Raw samples and variance

### 10,000 no-op tests / 16 modules

- Testenix range: 0.857 s–0.892 s
- Testenix standard deviation: 0.013 s
- Testenix raw samples: 0.869, 0.857, 0.863, 0.892, 0.871 seconds
- pytest range: 2.447 s–2.522 s
- pytest standard deviation: 0.027 s
- pytest raw samples: 2.522, 2.477, 2.471, 2.447, 2.479 seconds
- pytest-xdist range: 2.075 s–2.267 s
- pytest-xdist standard deviation: 0.081 s
- pytest-xdist raw samples: 2.170, 2.267, 2.077, 2.075, 2.106 seconds
- Measured rounds: 5; warmups: 1
- Workers: 4
- Testenix history: disabled with `--no-history`
- pytest-xdist strategy: default `load`
- Recorded at: `2026-07-20T12:12:43.635798+00:00`
- Commit: `8f24f8a7bd72fa876988a8ce96364be97e35c2b6`
- Clean working tree at capture: yes
- [Raw JSON](https://github.com/polishdataengineer/testenix/blob/main/benchmarks/baseline.json)

### 10,000 uneven-duration tests / 16 modules

- Testenix range: 1.336 s–1.378 s
- Testenix standard deviation: 0.016 s
- Testenix raw samples: 1.336, 1.378, 1.342, 1.345, 1.356 seconds
- pytest range: 3.043 s–3.085 s
- pytest standard deviation: 0.016 s
- pytest raw samples: 3.070, 3.085, 3.043, 3.076, 3.077 seconds
- pytest-xdist range: 2.109 s–2.176 s
- pytest-xdist standard deviation: 0.025 s
- pytest-xdist raw samples: 2.146, 2.129, 2.109, 2.138, 2.176 seconds
- Measured rounds: 5; warmups: 1
- Workers: 4
- Testenix history: disabled with `--no-history`
- pytest-xdist strategy: default `load`
- Recorded at: `2026-07-20T12:13:53.369799+00:00`
- Commit: `18d9bba6cb5c8e39c2d5b211ee4384ae8f824524`
- Clean working tree at capture: yes
- [Raw JSON](https://github.com/polishdataengineer/testenix/blob/main/benchmarks/baseline_uneven.json)

### 100,000 no-op tests / 16 modules

- Testenix range: 7.912 s–8.096 s
- Testenix standard deviation: 0.075 s
- Testenix raw samples: 7.912, 8.096, 8.086, 8.038, 7.997 seconds
- pytest range: 25.188 s–27.005 s
- pytest standard deviation: 0.772 s
- pytest raw samples: 27.005, 25.333, 25.246, 25.188, 25.380 seconds
- pytest-xdist range: 21.120 s–22.216 s
- pytest-xdist standard deviation: 0.486 s
- pytest-xdist raw samples: 21.239, 21.120, 22.216, 21.300, 21.949 seconds
- Measured rounds: 5; warmups: 1
- Workers: 4
- Testenix history: disabled with `--no-history`
- pytest-xdist strategy: default `load`
- Recorded at: `2026-07-20T12:19:39.942492+00:00`
- Commit: `24b877c2f98420e91dcd2c8bcbc9417c7cf1ac96`
- Clean working tree at capture: yes
- [Raw JSON](https://github.com/polishdataengineer/testenix/blob/main/benchmarks/baseline_100k.json)

## Migrated-suite measurements

These separate measurements start with generated pytest or unittest sources, complete one safe
copy-and-validate migration, and then compare recurring source-suite runs with recurring native
Testenix runs. The migration transaction is a one-time cost shown separately; it is not included
in either execution median. These records came from the pre-v0.2 source commit linked below; its
distribution metadata still reported `0.1.0`. They are historical evidence, not measurements of
the current release.

| Source runner | Workload | Tests / modules | Source median | Native median | Native vs source | Migration transaction |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| pytest (sequential) | no-op | 3,000 / 64 | 1.539 s | 0.521 s | 2.96× faster | 5.940 s |
| unittest outcome probe (sequential) | no-op | 3,000 / 64 | 0.161 s | 1.192 s | 7.40× slower | 6.742 s |
| unittest outcome probe (sequential) | 1 ms body | 3,000 / 64 | 4.066 s | 2.577 s | 1.58× faster | 17.251 s |

The native side used four workers. The source pytest and unittest outcome-probe baselines were
sequential, so these rows do not compare Testenix with pytest-xdist or another parallel unittest
runner. The unittest probe uses the standard-library loader and result semantics, then serializes
per-test outcomes for parity checking; its timing therefore includes that small audit overhead.
The no-op unittest wrappers are 7.40× slower than the probe because wrapper, loading, and
result-adaptation costs dominate an empty body. With 1 ms of synthetic work per unittest method,
parallel native execution is 1.58× faster in this 64-module layout. Module count and duration are
therefore material, and none of these synthetic rows predicts a specific real project.

### Raw migration samples and variance

### pytest / no-op

- Source command: `python -m pytest -q -p no:cacheprovider tests`
- Native command: `python -m testenix run testenix_migrated --workers 4 --no-history`
- Source median: 1.539 s
- Source range: 1.519 s–1.573 s;
  standard deviation: 0.020 s
- Source raw samples: 1.547, 1.535, 1.539, 1.573, 1.519 seconds
- Native Testenix median: 0.521 s
- Native Testenix range: 0.496 s–0.570 s;
  standard deviation: 0.030 s
- Native Testenix raw samples: 0.532, 0.498, 0.570, 0.496, 0.521 seconds
- Native workers: 4
- Measured rounds: 5; warmups: 1
- One-time copy, validation, and publication transaction: 5.940 s
- Integrity gates: 3,000 converted tests, matching source/native outcomes,
  original SHA-256 values unchanged
- Recorded at: `2026-07-20T16:38:49.510465+00:00`
- Source commit: [`3a51a901d268b061e9a87168300b41f3a2714a84`](https://github.com/polishdataengineer/testenix/commit/3a51a901d268b061e9a87168300b41f3a2714a84); worktree clean
- Lock SHA-256: `8ef0a9258aa5196bf2891f9da9f66c29bcf4e9bf297d178f3d4939cad36130cf`
- Versions: pytest=9.1.1, python=3.11.14, testenix=0.1.0, unittest=stdlib-3.11.14
- Environment: cpu_count=14, cpu_model=Apple M4 Pro, machine=arm64, platform=macOS-26.5.1-arm64-arm-64bit, python_implementation=CPython, python_version=3.11.14
- [Raw JSON](https://github.com/polishdataengineer/testenix/blob/main/benchmarks/migration_baseline_pytest_3000.json)

### unittest / no-op

- Source command: `python -m testenix._unittest_probe --output <project>/.benchmark-unittest.json tests`
- Native command: `python -m testenix run testenix_migrated --workers 4 --no-history`
- Source median: 0.161 s
- Source range: 0.154 s–0.166 s;
  standard deviation: 0.004 s
- Source raw samples: 0.161, 0.160, 0.161, 0.154, 0.166 seconds
- Native Testenix median: 1.192 s
- Native Testenix range: 1.151 s–1.264 s;
  standard deviation: 0.051 s
- Native Testenix raw samples: 1.192, 1.171, 1.256, 1.151, 1.264 seconds
- Native workers: 4
- Measured rounds: 5; warmups: 1
- One-time copy, validation, and publication transaction: 6.742 s
- Integrity gates: 3,000 converted tests, matching source/native outcomes,
  original SHA-256 values unchanged
- Recorded at: `2026-07-20T16:39:05.030979+00:00`
- Source commit: [`3a51a901d268b061e9a87168300b41f3a2714a84`](https://github.com/polishdataengineer/testenix/commit/3a51a901d268b061e9a87168300b41f3a2714a84); worktree clean
- Lock SHA-256: `8ef0a9258aa5196bf2891f9da9f66c29bcf4e9bf297d178f3d4939cad36130cf`
- Versions: pytest=9.1.1, python=3.11.14, testenix=0.1.0, unittest=stdlib-3.11.14
- Environment: cpu_count=14, cpu_model=Apple M4 Pro, machine=arm64, platform=macOS-26.5.1-arm64-arm-64bit, python_implementation=CPython, python_version=3.11.14
- [Raw JSON](https://github.com/polishdataengineer/testenix/blob/main/benchmarks/migration_baseline_unittest_3000.json)

### unittest / 1 ms body

- Source command: `python -m testenix._unittest_probe --output <project>/.benchmark-unittest.json tests`
- Native command: `python -m testenix run testenix_migrated --workers 4 --no-history`
- Source median: 4.066 s
- Source range: 4.023 s–4.075 s;
  standard deviation: 0.021 s
- Source raw samples: 4.075, 4.063, 4.066, 4.023, 4.067 seconds
- Native Testenix median: 2.577 s
- Native Testenix range: 2.454 s–2.644 s;
  standard deviation: 0.071 s
- Native Testenix raw samples: 2.577, 2.601, 2.571, 2.454, 2.644 seconds
- Native workers: 4
- Measured rounds: 5; warmups: 1
- One-time copy, validation, and publication transaction: 17.251 s
- Integrity gates: 3,000 converted tests, matching source/native outcomes,
  original SHA-256 values unchanged
- Recorded at: `2026-07-20T16:40:02.708652+00:00`
- Source commit: [`3a51a901d268b061e9a87168300b41f3a2714a84`](https://github.com/polishdataengineer/testenix/commit/3a51a901d268b061e9a87168300b41f3a2714a84); worktree clean
- Lock SHA-256: `8ef0a9258aa5196bf2891f9da9f66c29bcf4e9bf297d178f3d4939cad36130cf`
- Versions: pytest=9.1.1, python=3.11.14, testenix=0.1.0, unittest=stdlib-3.11.14
- Environment: cpu_count=14, cpu_model=Apple M4 Pro, machine=arm64, platform=macOS-26.5.1-arm64-arm-64bit, python_implementation=CPython, python_version=3.11.14
- [Raw JSON](https://github.com/polishdataengineer/testenix/blob/main/benchmarks/migration_baseline_unittest_3000_delay_1ms.json)


## Interpretation

The historical checked-in results show that Testenix 0.1.0 had low per-test overhead for the large
generated suites above and was competitive with sequential pytest and pytest-xdist's default
`load` strategy in those scenarios. They are not evidence for the current release.

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
$ uv run python benchmarks/run_migration_benchmark.py --framework pytest --tests 3000 \
    --modules 64 --workers 4 --warmups 1 --repeats 5 \
    --output benchmarks/migration_baseline_pytest_3000.json
$ uv run python benchmarks/run_migration_benchmark.py --framework unittest --tests 3000 \
    --modules 64 --workers 4 --warmups 1 --repeats 5 \
    --output benchmarks/migration_baseline_unittest_3000.json
$ uv run python benchmarks/run_migration_benchmark.py --framework unittest --tests 3000 \
    --modules 64 --workers 4 --delay-ms 1 --warmups 1 --repeats 5 \
    --output benchmarks/migration_baseline_unittest_3000_delay_1ms.json
```

Review the [benchmarking contract](../benchmarking.md) before comparing or publishing new data.
