# Changelog

All notable changes will be documented in this file. The format follows Keep a Changelog and the
project intends to use Semantic Versioning once its public API reaches stability.

## [Unreleased]

## [0.1.0] - 2026-07-20

### Added

- Native authoring API, fixture graph, cases, and sync/async execution.
- Versioned event stream and lossless run/test/attempt/phase result model.
- Deterministic local scheduling, process-worker supervision, and retries.
- Console, JSON, JUnit XML, and SQLite history adapters.
- `testenix` command-line interface and `pyproject.toml` configuration.
- Supervised collection, worker-ready handshakes, process-tree cleanup, and cancellable async runs.
- Module affinity, streamed partial-result recovery, and stable rediscovery locators for arbitrary
  case values.
- Strict mypy/pyright validation and adversarial tests for crashes, hangs, retries, and teardown
  ownership.
- Counterbalanced, count-validating 1k/10k/100k performance harness with configurable module count
  and scalable uneven workloads.
- A searchable Sphinx/Furo documentation site for GitHub Pages, generated Python API reference,
  and one-click page/full-document copying for LLM context.
- Deterministic `llms.txt` and `llms-full.txt` references plus generated benchmark tables and SVG.
- Manual GitHub benchmark workflow with downloadable raw JSON results.

### Changed

- Reused sync executor threads, removed duplicate final IPC payloads, indexed worker rediscovery,
  and eliminated per-test path/source inspection.
- Compacted coordinator events, made event IDs deterministic, retained one JSONL descriptor per
  run, and removed unchanged manifest/selection copies.
- Reduced the recorded 100k-test median to 8.038 seconds versus 25.333 seconds for pytest on the
  documented M4 Pro development baseline; comparative claims remain workload-specific.
- Isolated benchmark runs from repository pytest configuration, disabled pytest cache/plugin
  autoloading, and added commit, lockfile, version, and dirty-state provenance to new results.
- Hardened PyPI releases by requiring the release tag to reference a commit contained in `main`.
