# Contributing

Testenix is in an early design phase. Changes should preserve the dependency rules in
`docs/architecture.md` and include tests for every externally visible behavior.

## Local checks

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

## Compatibility promises

- Public authoring helpers exported from `testenix` require an explicit deprecation cycle before
  removal after the 0.x API stabilizes.
- Event records carry a schema version. Readers must reject unsupported future schemas instead of
  silently guessing.
- Reporters consume domain results; they must not parse console output.
- Retries append attempts and never mutate or discard an earlier attempt.

## Pull requests

Keep changes focused, document user-facing behavior, and add an entry under `Unreleased` in the
changelog. Performance changes should include the relevant scenario from `docs/benchmarking.md`.
