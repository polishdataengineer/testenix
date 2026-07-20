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

## 0.2 — fast feedback

- Dynamic micro-shards and work stealing.
- `--last-failed`, watch mode, and failure fingerprints.
- Test-impact analysis in shadow mode with an explanation for every selection decision.
- Stable assertion-diff protocol and improved plain-assert diagnostics.

## 0.3 — adoption

- Pytest hook adapter translating collection and outcomes into Testenix events and `RunResult`.
- Expand migration beyond the v0.1 static subset only when new transformations have differential
  semantics tests on real projects.
- Versioned reporter and selector plugin interfaces.
- IDE protocol and machine-readable collection manifest.

## Later

- Remote workers with leases and heartbeats.
- Resource-capacity scheduling and explicit test affinity.
- Opt-in caching for tests that declare all external inputs.
- Flakiness history, quarantine ownership, and expiration policy.

Result caching and impact selection will not become build-gating defaults until their false-negative
rate is measured continuously against full-suite runs.
