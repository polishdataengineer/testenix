from __future__ import annotations

from dataclasses import replace

from testenix.contracts import (
    AttemptResult,
    CollectionIssue,
    Phase,
    PhaseResult,
    RunResult,
    Status,
)
from testenix.contracts import (
    TestResult as DomainTestResult,
)
from testenix.contracts import (
    TestSpec as DomainTestSpec,
)


def make_run(status: Status) -> RunResult:
    test = DomainTestSpec(
        id="tests/test_sample.py::works",
        path="tests/test_sample.py",
        module_name="test_sample",
        function_name="works",
        display_name="works",
    )
    phase = PhaseResult(phase=Phase.CALL, status=status, duration=0.01)
    attempt = AttemptResult(
        test_id=test.id,
        attempt=1,
        worker_id="worker-0",
        status=status,
        duration=0.01,
        phases=(phase,),
        started_at=1.0,
        finished_at=1.01,
    )
    result = DomainTestResult(test=test, status=status, attempts=(attempt,), duration=0.01)
    return RunResult(
        run_id="run-1",
        tests=(result,),
        collection_issues=(),
        started_at=1.0,
        finished_at=1.01,
    )


def test_successful_and_non_gating_statuses_exit_zero() -> None:
    for status in (Status.PASS, Status.SKIP, Status.XFAIL, Status.CACHED_PASS):
        assert make_run(status).exit_code == 0


def test_failures_and_flakiness_exit_non_zero() -> None:
    for status in (
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
    ):
        assert make_run(status).exit_code == 1


def test_collection_issue_has_distinct_exit_code() -> None:
    run = make_run(Status.PASS)
    issue = CollectionIssue(path="tests/broken.py", message="cannot import")

    assert replace(run, collection_issues=(issue,)).exit_code == 2
