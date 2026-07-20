"""Private, quiet native runner used by the migration validator.

The public CLI intentionally prints every result.  Migration may validate tens of
thousands of generated wrappers, so this subprocess writes only the machine-readable
report requested by its parent.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from testenix.config import TestenixConfig
from testenix.reporters.json import JsonReporter
from testenix.runner import run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m testenix._migration_native_probe")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=_worker_count, required=True)
    parser.add_argument("paths", nargs="+")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    config = TestenixConfig(
        paths=tuple(arguments.paths),
        workers=arguments.workers,
        retries=0,
        history_path=None,
    )
    result = run(tuple(arguments.paths), config)
    JsonReporter(arguments.output).write(result)
    return result.exit_code


def _worker_count(value: str) -> int | str:
    if value == "auto":
        return value
    workers = int(value)
    if workers < 1:
        raise argparse.ArgumentTypeError("workers must be at least 1")
    return workers


if __name__ == "__main__":  # pragma: no cover - subprocess entry point
    raise SystemExit(main())
