"""Local SQLite history used for duration estimates and status trends."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from testenix.contracts import RunResult, Status, TestResult

_INFRASTRUCTURE_STATUSES = frozenset({Status.INFRA_ERROR, Status.CRASH})


def _scheduler_duration(result: TestResult) -> float | None:
    """Return the latest real test-attempt duration suitable for scheduling.

    A run may contain a worker crash followed by a successful recovery, or a
    completed test attempt followed by infrastructure failure while retrying.
    Using ``TestResult.duration`` would charge every attempt to the next run and
    let a transient worker failure permanently distort LPT estimates.
    """

    ordered = sorted(result.attempts, key=lambda attempt: attempt.attempt)
    for attempt in reversed(ordered):
        if attempt.status in _INFRASTRUCTURE_STATUSES:
            continue
        duration = float(attempt.duration)
        return duration if math.isfinite(duration) and duration >= 0 else None
    return None


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """The latest status and accumulated duration estimate for a test."""

    test_id: str
    duration: float
    status: Status
    updated_at: float
    samples: int


class HistoryStore:
    """Coordinator-owned SQLite result history.

    ``record_run`` is idempotent for a given ``(run_id, test_id)`` pair, which
    makes it safe for both the runner and CLI hand-off code to persist a result.
    A store owns one connection and can be used as a context manager.
    """

    def __init__(self, path: str | Path = Path(".testenix/history.sqlite3")) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._connection = sqlite3.connect(str(self.path), timeout=10.0)
        self._connection.row_factory = sqlite3.Row
        self._initialise()

    def _initialise(self) -> None:
        with self._lock, self._connection:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    started_at REAL NOT NULL,
                    finished_at REAL NOT NULL,
                    exit_code INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS test_results (
                    run_id TEXT NOT NULL,
                    test_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    duration REAL NOT NULL,
                    finished_at REAL NOT NULL,
                    PRIMARY KEY (run_id, test_id),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS test_history (
                    test_id TEXT PRIMARY KEY,
                    mean_duration REAL NOT NULL,
                    last_duration REAL NOT NULL,
                    last_status TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    samples INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_test_results_test_finished
                    ON test_results(test_id, finished_at DESC);
                """
            )

    def record_run(self, run: RunResult) -> None:
        """Persist finalized outcomes and update duration estimates."""

        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO runs(run_id, started_at, finished_at, exit_code)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(run_id) DO NOTHING
                """,
                (run.run_id, run.started_at, run.finished_at, run.exit_code),
            )
            for result in run.tests:
                self._record_result(run.run_id, run.finished_at, result)

    # ``record`` is intentionally tiny but useful for embedders.
    record = record_run

    def _record_result(self, run_id: str, finished_at: float, result: TestResult) -> None:
        cursor = self._connection.execute(
            """
            INSERT INTO test_results(run_id, test_id, status, duration, finished_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(run_id, test_id) DO NOTHING
            """,
            (run_id, result.test.id, result.status.value, result.duration, finished_at),
        )
        if cursor.rowcount == 0:
            return
        scheduler_duration = _scheduler_duration(result)
        if scheduler_duration is None:
            # Preserve an existing row's latest outcome without changing its
            # duration estimate or sample count. With no prior real attempt,
            # test_results/recent_statuses remains the source of status history.
            self._connection.execute(
                """
                UPDATE test_history
                SET last_status = ?, updated_at = ?
                WHERE test_id = ?
                """,
                (result.status.value, finished_at, result.test.id),
            )
            return
        self._connection.execute(
            """
            INSERT INTO test_history(
                test_id, mean_duration, last_duration, last_status, updated_at, samples
            ) VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(test_id) DO UPDATE SET
                mean_duration = (
                    test_history.mean_duration * test_history.samples + excluded.last_duration
                ) / (test_history.samples + 1),
                last_duration = excluded.last_duration,
                last_status = excluded.last_status,
                updated_at = excluded.updated_at,
                samples = test_history.samples + 1
            """,
            (
                result.test.id,
                scheduler_duration,
                scheduler_duration,
                result.status.value,
                finished_at,
            ),
        )

    def get(self, test_id: str) -> HistoryEntry | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT test_id, mean_duration, last_status, updated_at, samples
                FROM test_history WHERE test_id = ?
                """,
                (test_id,),
            ).fetchone()
        return _entry_from_row(row) if row is not None else None

    def durations(self, test_ids: Iterable[str] | None = None) -> dict[str, float]:
        """Return deterministic mean-duration estimates, ordered by test id."""

        requested = None if test_ids is None else set(test_ids)
        with self._lock:
            rows = self._connection.execute(
                "SELECT test_id, mean_duration FROM test_history ORDER BY test_id"
            ).fetchall()
        return {
            str(row["test_id"]): float(row["mean_duration"])
            for row in rows
            if requested is None or row["test_id"] in requested
        }

    # Runner-facing name that communicates how the values are intended to be used.
    estimated_durations = durations

    def recent_statuses(self, test_id: str, limit: int = 20) -> tuple[Status, ...]:
        if limit < 1:
            return ()
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT status FROM test_results
                WHERE test_id = ?
                ORDER BY finished_at DESC, run_id DESC
                LIMIT ?
                """,
                (test_id, limit),
            ).fetchall()
        return tuple(Status(row["status"]) for row in rows)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> HistoryStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _entry_from_row(row: sqlite3.Row) -> HistoryEntry:
    return HistoryEntry(
        test_id=str(row["test_id"]),
        duration=float(row["mean_duration"]),
        status=Status(row["last_status"]),
        updated_at=float(row["updated_at"]),
        samples=int(row["samples"]),
    )
