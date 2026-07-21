# Roadmap

## 0.1 — native vertical slice

- Native function tests and explicit test descriptions.
- Typed fixture graph with test, module, and session scopes.
- Sync, coroutine, generator, and async-generator execution.
- Explicit cases, tags, skip, expected failure, and timeouts.
- Local process workers with deterministic LPT scheduling.
- Immutable attempts and lossless setup/call/teardown aggregation.
- Console, JSON, JUnit XML, JSONL events, and SQLite duration history.
- Optional `testenix pytest` compatibility bridge preserving the real pytest engine and plugins.
- Safe native migration for a conservative pytest subset and direct unittest TestCase classes,
  with source fingerprints, disposable validation copies, serial/parallel parity, and atomic
  create-only publication.

## 0.2 — real-world pytest migration

- Dependency-free native `tmp_path` and reversible `monkeypatch` fixtures for common
  `setattr`/`setenv` usage.
- Static autouse fixtures with native setup and teardown ownership.
- Bare pytest-asyncio coroutine markers translated to isolated fresh-loop wrappers that preserve
  the plugin's default function-scoped loop lifecycle.
- Fresh-instance wrappers for simple pytest classes, while complex lifecycle and inheritance stay
  fail-closed.
- Compact migration diagnostics on the console with complete line detail retained in JSON.
- Differential validation against a 118-test real-world suite before publication.

## 0.3 — fast feedback

- Adaptive worker selection based on real execution units, duration history, and process cost,
  plus an explicit project-local `tune`/`benchmark` command.
- Conservative opt-in intra-module sharding and a source-verified trusted collection manifest that
  can bypass duplicate collection imports without trusting stale source metadata.
- Dynamic micro-shards and work stealing.
- `--last-failed`, watch mode, and failure fingerprints.
- Test-impact analysis in shadow mode with an explanation for every selection decision.
- Stable assertion-diff protocol and improved plain-assert diagnostics.

## 0.4 — adoption

- Pytest hook adapter translating collection and outcomes into Testenix events and `RunResult`.
- Expand migration beyond the v0.2 static subset only when new transformations have differential
  semantics tests on real projects.
- Versioned reporter and selector plugin interfaces.
- IDE protocol built on the versioned machine-readable collection manifest.

## Later

- Remote workers with leases and heartbeats.
- Resource-capacity scheduling and explicit test affinity.
- Opt-in caching for tests that declare all external inputs.
- Flakiness history, quarantine ownership, and expiration policy.

Result caching and impact selection will not become build-gating defaults until their false-negative
rate is measured continuously against full-suite runs.
