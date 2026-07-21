"""Stable JSON serialization for Testenix run results."""

from __future__ import annotations

import json
import os
import tempfile
from enum import Enum
from pathlib import Path
from typing import Any

from testenix.contracts import EVENT_SCHEMA_VERSION, RunResult, TestResult

RESULT_FORMAT = "testenix.run-result"


class JsonReporter:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def render(self, run: RunResult) -> str:
        return (
            json.dumps(
                run_result_to_dict(run),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        )

    def write(self, run: RunResult) -> None:
        _atomic_write(self.path, self.render(run))

    report = write


def run_result_to_dict(run: RunResult) -> dict[str, Any]:
    return {
        "collection_issues": [
            {
                "message": issue.message,
                "path": issue.path,
                "traceback": issue.traceback,
            }
            for issue in sorted(run.collection_issues, key=lambda item: (item.path, item.message))
        ],
        "duration": max(0.0, run.finished_at - run.started_at),
        "exit_code": run.exit_code,
        "finished_at": run.finished_at,
        "format": RESULT_FORMAT,
        "run_id": run.run_id,
        "schema_version": EVENT_SCHEMA_VERSION,
        "shardable_paths": list(run.shardable_paths),
        "started_at": run.started_at,
        "tests": [_test_to_dict(result) for result in sorted(run.tests, key=_test_sort_key)],
        "workers_used": run.workers_used,
    }


def _test_to_dict(result: TestResult) -> dict[str, Any]:
    test = result.test
    return {
        "attempts": [
            {
                "attempt": attempt.attempt,
                "duration": attempt.duration,
                "finished_at": attempt.finished_at,
                "phases": [
                    {
                        "duration": phase.duration,
                        "exception_type": phase.exception_type,
                        "message": phase.message,
                        "phase": phase.phase.value,
                        "status": phase.status.value,
                        "stderr": phase.stderr,
                        "stdout": phase.stdout,
                        "traceback": phase.traceback,
                    }
                    for phase in attempt.phases
                ],
                "started_at": attempt.started_at,
                "status": attempt.status.value,
                "test_id": attempt.test_id,
                "worker_id": attempt.worker_id,
            }
            for attempt in sorted(result.attempts, key=lambda item: item.attempt)
        ],
        "duration": result.duration,
        "status": result.status.value,
        "test": {
            "case_id": test.case_id,
            "display_name": test.display_name,
            "function_name": test.function_name,
            "id": test.id,
            "module_name": test.module_name,
            "parameters": _json_value(test.parameters),
            "path": test.path,
            "skip_reason": test.skip_reason,
            "source_line": test.source_line,
            "tags": sorted(test.tags),
            "timeout": test.timeout,
            "xfail_reason": test.xfail_reason,
        },
    }


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_json_value(item) for item in value), key=str)
    return repr(value)


def _test_sort_key(result: TestResult) -> tuple[str, int, str]:
    line = result.test.source_line if result.test.source_line is not None else -1
    return (result.test.path, line, result.test.id)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary_name = temporary.name
        os.replace(temporary_name, path)
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
