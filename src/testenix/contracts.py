"""Stable domain contracts shared by the native engine and all adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

EVENT_SCHEMA_VERSION = 1


class Scope(StrEnum):
    """Lifetime of a fixture instance."""

    TEST = "test"
    MODULE = "module"
    SESSION = "session"


class Status(StrEnum):
    """A lossless outcome vocabulary for phases, attempts, and tests."""

    PASS = "pass"
    FAIL = "fail"
    ERROR_SETUP = "error_setup"
    ERROR_TEARDOWN = "error_teardown"
    SKIP = "skip"
    XFAIL = "xfail"
    XPASS = "xpass"
    TIMEOUT = "timeout"
    CRASH = "crash"
    INFRA_ERROR = "infra_error"
    CANCELLED = "cancelled"
    NOT_RUN = "not_run"
    FLAKY = "flaky"
    CACHED_PASS = "cached_pass"


class Phase(StrEnum):
    SETUP = "setup"
    CALL = "call"
    TEARDOWN = "teardown"


class EventType(StrEnum):
    RUN_STARTED = "run_started"
    COLLECTION_STARTED = "collection_started"
    TEST_DISCOVERED = "test_discovered"
    COLLECTION_ERROR = "collection_error"
    COLLECTION_FINISHED = "collection_finished"
    TEST_SELECTED = "test_selected"
    TEST_EXCLUDED = "test_excluded"
    UNIT_SCHEDULED = "unit_scheduled"
    ATTEMPT_STARTED = "attempt_started"
    PHASE_FINISHED = "phase_finished"
    ATTEMPT_FINISHED = "attempt_finished"
    WORKER_LOST = "worker_lost"
    TEST_FINALIZED = "test_finalized"
    RUN_FINISHED = "run_finished"


@dataclass(frozen=True, slots=True)
class TestSpec:
    """Serializable description of one concrete test case."""

    __test__ = False

    id: str
    path: str
    module_name: str
    function_name: str
    display_name: str
    parameters: dict[str, Any] = field(default_factory=dict)
    case_id: str | None = None
    tags: frozenset[str] = field(default_factory=frozenset)
    skip_reason: str | None = None
    xfail_reason: str | None = None
    timeout: float | None = None
    source_line: int | None = None


@dataclass(frozen=True, slots=True)
class CollectionIssue:
    path: str
    message: str
    traceback: str | None = None


@dataclass(frozen=True, slots=True)
class PhaseResult:
    phase: Phase
    status: Status
    duration: float
    message: str | None = None
    exception_type: str | None = None
    traceback: str | None = None
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True, slots=True)
class AttemptResult:
    test_id: str
    attempt: int
    worker_id: str
    status: Status
    duration: float
    phases: tuple[PhaseResult, ...]
    started_at: float
    finished_at: float


@dataclass(frozen=True, slots=True)
class TestResult:
    __test__ = False

    test: TestSpec
    status: Status
    attempts: tuple[AttemptResult, ...]
    duration: float


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    tests: tuple[TestResult, ...]
    collection_issues: tuple[CollectionIssue, ...]
    started_at: float
    finished_at: float

    @property
    def exit_code(self) -> int:
        failing = {
            Status.FAIL,
            Status.ERROR_SETUP,
            Status.ERROR_TEARDOWN,
            Status.XPASS,
            Status.TIMEOUT,
            Status.CRASH,
            Status.INFRA_ERROR,
            Status.CANCELLED,
            Status.NOT_RUN,
            Status.FLAKY,
        }
        if self.collection_issues:
            return 2
        return int(any(test.status in failing for test in self.tests))


@dataclass(frozen=True, slots=True)
class Event:
    """Append-only fact emitted while a run is executing."""

    event_id: str
    run_id: str
    event_type: EventType
    timestamp: float
    sequence: int
    schema_version: int = EVENT_SCHEMA_VERSION
    test_id: str | None = None
    attempt: int | None = None
    worker_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
