# Documentation for LLMs

Testenix publishes a clean, deterministic representation of its documentation for use as model
context. Every documentation page also includes **Copy this page** and
**Copy all docs for an LLM** controls above the article.

## Recommended inputs

| Resource | Use it when |
| --- | --- |
| [`llms.txt`](https://polishdataengineer.github.io/testenix/llms.txt) | A model should first discover the project and choose relevant pages. |
| [`llms-full.txt`](https://polishdataengineer.github.io/testenix/llms-full.txt) | You want one self-contained reference with guides, API, architecture, and benchmark context. |
| [Python API reference](reference/api.md) | The task is specifically about authoring or embedding Testenix. |
| [Pytest compatibility](guides/pytest-compatibility.md) | A model must choose between delegation and native migration. |
| [Benchmark results](benchmarks/results.md) | A model needs to evaluate or repeat performance claims. |

`llms.txt` follows the emerging llms.txt proposal, but it should be treated as a convenience
format rather than an official web standard.

## Suggested prompt

```text
Use the following Testenix documentation as the authoritative project reference.
Distinguish current documented behavior from roadmap items. Treat benchmark results as
workload-specific and preserve all documented limitations.

<paste llms-full.txt here>
```

## What the full reference contains

- installation and first-run instructions;
- the pytest compatibility bridge, capability matrix, and migration boundary;
- native tests, cases, tags, skips, expected failures, and fixtures;
- parallelism, timeouts, retries, crash recovery, reports, and history;
- CLI, configuration, and generated Python API reference;
- architecture and roadmap;
- benchmark results, raw-data links, methodology, and caveats.

## Source-of-truth policy

The text files are generated from the checked-in documentation and benchmark JSON. CI fails when a
generated file is stale, so the website, GitHub sources, and LLM reference cannot quietly drift
apart.

The public API page is built from the package installed in the documentation environment. When a
signature changes without its documentation being updated, the strict documentation build exposes
the mismatch in the Pull Request.
