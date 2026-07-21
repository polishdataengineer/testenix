---
description: Safely convert supported pytest and unittest suites to native Testenix without replacing the originals.
---

# Safe migration to native Testenix

An existing suite can use Testenix in three ways:

| Goal | Command | Execution engine | Original suite |
| --- | --- | --- | --- |
| Preserve all pytest behavior | `testenix pytest -q tests` | pytest | Runs directly |
| Check whether native conversion is supported | `testenix migrate auto tests --dry-run` | no tests run | Read-only |
| Create a validated native copy | `testenix migrate auto tests -o tests_testenix` | Testenix after conversion | Kept and used as the baseline |

Migration is conservative. It stops on a construct whose behavior cannot be proven equivalent
instead of producing a plausible-looking, incomplete suite.

## First migration

For pytest, install the optional dependency because validation executes the real source suite:

```console
$ python -m pip install "testenix[pytest]"
$ testenix migrate pytest tests --dry-run
$ testenix migrate pytest tests --check --workers 2 \
    --report-json reports/migration-check.json
$ testenix migrate pytest tests --output tests_testenix \
    --report-json reports/migration.json
$ testenix run tests_testenix
```

For a standard-library unittest suite, no extra package is needed:

```console
$ testenix migrate unittest tests --output tests_testenix
```

Use `auto` when pytest-style modules and unittest modules live in the same selected directory.
They must be separate modules; a file that mixes both test models is rejected.

The destination defaults to `testenix_migrated`. It must be a new directory inside the project,
with an existing real parent. There is deliberately no `--force` and no in-place mode.
An integer `--workers` value must be at least 2 so the parallel gate cannot silently repeat the
serial command. During `--check` or publication, a one-module candidate still has one schedulable
affinity unit, which is disclosed as a `MIG006` warning; spread tests across independent modules to
exercise multiple workers. The warning is not shown for `--dry-run` or an already-blocked migration
because no parallel candidate is run in either case. An audit-report path must also be new, inside
the project, and disjoint from every selected source and the output suite. Testenix never replaces
an existing report.

## Transaction and rollback contract

Testenix uses copy-and-validate rather than edit-and-undo:

```text
preflight paths
  -> snapshot source names + SHA-256
  -> convert in memory
  -> run original suite in disposable project copy
  -> run generated suite with one Testenix worker in a fresh copy
  -> run generated suite in parallel in another fresh copy
  -> compare inventory, totals, and every mapped test outcome
  -> recheck every source hash
  -> atomically publish a new directory without replacement
```

If parsing, conversion, the source baseline, either native run, parity comparison, a source hash,
or publication fails before the atomic rename, the requested output directory remains absent. A
failure to write the optional report after a successful rename instead leaves the validated output
published, prints an explicit warning, and returns the successful migration status. Testenix never writes to,
renames, deletes, or replaces a selected source file. There are therefore no old files to restore:
the old suite was never changed.

`--dry-run` stops after static analysis and a final source-hash check. `--check` performs the full
three-run validation but does not publish the destination. The default performs the same checks
and then uses an operating-system no-replace rename. If another process creates the destination
during validation, publication fails closed.

POSIX staging creation, artifact writes, publication, and cleanup are anchored to open directory
descriptors. On a platform without safe descriptor-relative recursive deletion, a non-empty
failed staging transaction is retained under `.testenix/migrations` for inspection instead of
being removed with a path-based recursive walk.

Validation runs with the shadow as both the current directory and `PWD`, so ordinary relative-path
writes land in the disposable copy. A shadow is not an operating-system security sandbox: a test
can still write through a hard-coded absolute path or another environment variable, and network,
database, cloud, and subprocess side effects remain external. Selected Python sources are rehashed
before every terminal result, so detected drift stops publication, but Testenix never tries to undo
an independent user or test-process edit. Use test credentials and `--dry-run` first for suites
with external effects.

## Pytest conversion contract

The converter subset introduced in v0.2 supports the behavior below:

- module-level pytest-default `test*` functions and normal Python `assert` statements;
- simple `Test*` classes with a fresh zero-argument instance per test method, including ordinary
  helper methods; inheritance, class decorators, custom construction, and pytest class lifecycle
  hooks remain outside the safe subset;
- bare `@pytest.mark.asyncio` on `async def` tests. The generated synchronous wrapper runs every
  test or parametrized case in a fresh, closed `asyncio.Runner`, matching pytest-asyncio's default
  function-scoped loop isolation;
- one static `pytest.mark.parametrize` with static names, rows, IDs, and unmarked
  `pytest.param(..., id=...)` values;
- local fixtures using function or module scope, including a statically boolean `autouse=True`;
- simple fixtures from an adjacent `conftest.py` in the same directory;
- native `tmp_path`, which supplies a fresh `pathlib.Path` and removes its temporary directory at
  test teardown;
- native `monkeypatch.setattr` in object/attribute and dotted-import forms, plus `setenv` and
  idempotent `undo`; successful changes are restored in LIFO order during test teardown.
  `monkeypatch` may also flow through statically resolved module-local helpers when every use can
  be proven to stay inside this supported subset;
- static `pytest.mark.skip` and `pytest.mark.skipif`;
- plain argument-free custom markers, converted to Testenix tags;
- pytest runtime helpers `approx`, `deprecated_call`, `fail`, `raises`, and `warns`. Generated
  modules using these helpers still require pytest at runtime.

It blocks, with a file and line diagnostic:

- complex pytest test classes, xfail, runtime skip/xfail/importorskip/exit, and xunit lifecycle
  hooks;
- built-in fixtures other than `tmp_path` and `monkeypatch`, such as `capsys`, `caplog`, and
  `request`; monkeypatch operations outside the documented native subset, imported or dynamically
  rebound helpers, and values that escape static analysis are also unsupported;
- dynamically configured autouse fixtures, parametrized fixtures, fixture overrides, and inherited
  ancestor-`conftest` fixtures;
- session-scoped fixtures, because pytest creates one per run while Testenix session scope is
  currently worker-local;
- stacked, dynamic, indirect, scoped, or per-case-marked parametrization;
- `usefixtures`, module-level `pytestmark`, hook functions, plugin registration, and semantic
  plugin markers such as anyio, configured asyncio, timeout, order, repeat, or flaky;
- unmarked async tests, async fixtures, custom `event_loop_policy`, non-function asyncio loop
  scopes, and enabled asyncio debug mode. Testenix checks the effective pytest configuration and
  relevant `PYTEST_ADDOPTS` overrides before accepting bare asyncio markers;
- decorators and required parameters whose execution meaning cannot be established statically.

Any converted pytest file whose name is not already `test_*.py` is renamed in the generated copy
to a native-discoverable name. Explicit nonstandard files such as `specs.py` are supported when
they contain static test inventory. Supporting Python modules and package `__init__.py` files under
the selected source directories are copied to preserve common relative imports. Missing non-Python
assets or path-sensitive `__file__` behavior will make candidate validation fail rather than
publish a bad copy.

Pytest plugins are intentionally not emulated. Keep using `testenix pytest` for modules that need
them and migrate supported modules incrementally.

## Unittest conversion contract

Rewriting `self.assertEqual`, lifecycle methods, cleanup stacks, and mock decorators would be
fragile. Instead, Testenix generates one native function wrapper per test method. The wrapper:

1. resolves the original by the exact wrapper-to-project relative path, independent of `cwd`;
2. verifies a manifest containing every selected Python source and SHA-256;
3. loads the original class by exact path;
4. executes one method through the standard `TestCase.run(TestResult)` protocol;
5. exposes its pass, failure, skip, expected-failure, or unexpected-success outcome to Testenix.

This preserves direct subclasses of `unittest.TestCase` and
`unittest.IsolatedAsyncioTestCase`, including per-test `setUp`/`tearDown`, async lifecycle,
`addCleanup`, `assert*`, context-manager assertions, and `unittest.mock.patch`. Static skips map to
Testenix `SKIP`; `expectedFailure` maps to `XFAIL`; an unexpected success remains the gating
`XPASS` outcome.

The generated unittest suite deliberately depends on the unchanged originals. Do not delete or
move them, and do not move or rename the published generated directory: wrappers encode the exact
relative relationship between both trees. If either path or an original file changes, rerun
migration; a changed source makes native collection fail with an explicit diagnostic.

The converter blocks `subTest`, runtime `skipTest`/`SkipTest`, class/module lifecycle and cleanup,
custom `run` or loader hooks, `load_tests`, mixins or indirect inheritance, `FunctionTestCase`,
dynamic/DDT/parameterized method generation, metaclasses, and ambiguous decorators.

## What validation compares

The source suite must be green. Testenix records and compares:

- the exact collected test count against the converter's source-to-target mapping;
- passed, failed, error, skipped, expected-failure, and unexpected-success totals;
- the outcome of every source test against its exact generated target mapping;
- native serial and native parallel outcome signatures;
- every selected Python source path and SHA-256 immediately before publication.

A pre-existing source failure is not treated as proof of equivalent conversion, even when the
candidate happens to fail too. Fix the baseline or use `--dry-run` to inspect static support.

The JSON audit report uses the versioned `testenix.migration-report` format. It includes source
hashes, every source-to-target mapping, per-test outcomes, generated files, line diagnostics,
timings and summaries for all validation runs, publication status, and an `originals_modified`
flag. It is false for a successful transaction and true when a terminal source recheck detects
drift; the flag reports observed state and does not claim that Testenix caused an independent edit.
The console groups repeated diagnostics by code and shows the first location, so large suites do
not produce hundreds of near-identical lines. `--report-json FILE` and `--report-json -` always
retain every individual source- and line-addressed diagnostic. On a blocked transaction, the
console calls any safe in-memory result a *statically convertible subset* rather than implying that
those tests were published.

## Performance with thousands of migrated tests

Migration unlocks the native scheduler, process supervision, async engine, retries, history, and
Testenix reports. It does not guarantee that every converted suite is faster. By default Testenix
keeps a normal module as one affinity unit so module-scoped fixtures are not duplicated.
Consequently, 3,000 tests in one source module still form one schedulable unit; 3,000 tests spread
across enough independent modules can use multiple workers. A project may later opt eligible native
modules into `--shard-modules`, but only after validating the generated suite and the documented
static-analysis trust boundary. Run `testenix tune` on the published native copy before fixing its
CI worker count.

The checked-in migration baseline used an Apple M4 Pro, CPython 3.11, 3,000 generated tests in 64
modules, four native workers, one warm-up, and five measured rounds:

| Source runner | Test body | Source median | Native median | Native result | One-time migration |
| --- | --- | ---: | ---: | ---: | ---: |
| sequential pytest | no-op | 1.539 s | 0.521 s | 2.96x faster | 5.940 s |
| sequential unittest outcome probe | no-op | 0.161 s | 1.192 s | 7.40x slower | 6.742 s |
| sequential unittest outcome probe | 1 ms sleep | 4.066 s | 2.577 s | 1.58x faster | 17.251 s |

The migration column is the complete copy, source-baseline, serial-candidate,
parallel-candidate, integrity-check, and publication transaction. It is paid when regenerating a
suite and is deliberately excluded from the recurring execution medians. The unittest adapter
loads the unchanged source and translates `TestCase.run()` results for every native wrapper. That
fixed cost dominates an empty method; once the generated methods contain 1 ms of synthetic work,
parallel execution across 64 modules outweighs it in this scenario.

These source measurements use sequential pytest and a sequential Testenix probe built on the
stdlib unittest loader/result protocol. The probe serializes per-test outcomes for the parity gate,
so its small audit overhead is included. They are not comparisons
against pytest-xdist or a third-party parallel unittest runner. They are synthetic timing evidence,
not a promise for a real project: imports, fixtures, I/O, test-duration distribution, module
affinity, worker count, operating system, and CPU can all reverse the result. See the
[generated benchmark page](../benchmarks/results.md) for raw samples and variance, or inspect the
checked-in [pytest](https://github.com/polishdataengineer/testenix/blob/main/benchmarks/migration_baseline_pytest_3000.json),
[unittest no-op](https://github.com/polishdataengineer/testenix/blob/main/benchmarks/migration_baseline_unittest_3000.json),
and [unittest 1 ms](https://github.com/polishdataengineer/testenix/blob/main/benchmarks/migration_baseline_unittest_3000_delay_1ms.json)
JSON files directly.

Benchmark the generated directory only after parity validation:

```console
$ python benchmarks/run_migration_benchmark.py \
    --framework pytest --tests 3000 --modules 64 --workers 4 \
    --warmups 1 --repeats 5 --output migration-pytest-3000.json
$ python benchmarks/run_migration_benchmark.py \
    --framework unittest --tests 3000 --modules 64 --workers 4 \
    --warmups 1 --repeats 5 --output migration-unittest-3000.json
$ python benchmarks/run_migration_benchmark.py \
    --framework unittest --tests 3000 --modules 64 --workers 4 --delay-ms 1 \
    --warmups 1 --repeats 5 --output migration-unittest-3000-delay-1ms.json
```

The harness checks the exact count and source hashes before accepting any timing sample. Publish
the raw JSON, environment, module count, worker count, variance, and commands with a result. Do not
apply the repository's synthetic native benchmark ratio to a real migrated project without
measuring it.

## CI rollout

A low-risk rollout keeps both suites for several releases and runs the exact newly published copy:

```yaml
- name: Generate and validate a fresh native copy
  run: |
    testenix migrate auto tests --output tests_testenix_ci --workers 2 \
      --report-json migration-report.json

- name: Existing source suite
  run: testenix pytest -q tests

- name: Run the exact validated copy
  run: testenix run tests_testenix_ci --no-history
```

The output and report paths must be absent at job start, which is natural on a fresh CI checkout.
`--check` validates an ephemeral candidate; it does not prove that a separately committed
`tests_testenix` directory has identical bytes. Regenerate and publish a fresh CI output as above,
or add a project-specific content-manifest check for a committed copy. Regenerate whenever a pinned
unittest source changes.

## Exit codes

| Code | Migration meaning |
| ---: | --- |
| `0` | Analysis, check, or publication completed successfully. |
| `1` | Source or candidate execution failed, timed out, or produced different outcomes. |
| `2` | Unsafe/invalid path, existing output, source drift, or command usage error. |
| `3` | Internal process or pre-publication report failure. A report-only failure after publication warns and preserves exit `0`. |
| `4` | At least one construct is unsupported by the conservative converter. |
| `130` | Interrupted by the user. |

Diagnostics beginning with `PYT` describe pytest source, `UNIT` describes unittest source, and
`MIG` describes cross-framework inventory or transaction safety.
