"""JUnit XML output compatible with common CI systems."""

from __future__ import annotations

import os
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from testenix.contracts import PhaseResult, RunResult, Status, TestResult

_FAILURES = {Status.FAIL, Status.XPASS, Status.FLAKY}
_ERRORS = {
    Status.ERROR_SETUP,
    Status.ERROR_TEARDOWN,
    Status.TIMEOUT,
    Status.CRASH,
    Status.INFRA_ERROR,
}
_SKIPPED = {Status.SKIP, Status.XFAIL, Status.CANCELLED, Status.NOT_RUN}


class JUnitReporter:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def render(self, run: RunResult) -> str:
        root = _build_document(run)
        ET.indent(root, space="  ")
        return ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n"

    def write(self, run: RunResult) -> None:
        _atomic_write(self.path, self.render(run))

    report = write


def _build_document(run: RunResult) -> ET.Element:
    tests = sorted(run.tests, key=_test_sort_key)
    collection_errors = len(run.collection_issues)
    failures = sum(result.status in _FAILURES for result in tests)
    errors = sum(result.status in _ERRORS for result in tests) + collection_errors
    skipped = sum(result.status in _SKIPPED for result in tests)
    total = len(tests) + collection_errors
    run_duration = _duration(max(0.0, run.finished_at - run.started_at))

    root = ET.Element(
        "testsuites",
        {
            "name": "testenix",
            "tests": str(total),
            "failures": str(failures),
            "errors": str(errors),
            "skipped": str(skipped),
            "time": run_duration,
        },
    )
    suite = ET.SubElement(
        root,
        "testsuite",
        {
            "name": "testenix",
            "tests": str(total),
            "failures": str(failures),
            "errors": str(errors),
            "skipped": str(skipped),
            "time": run_duration,
        },
    )

    for issue in sorted(run.collection_issues, key=lambda item: (item.path, item.message)):
        case = ET.SubElement(
            suite,
            "testcase",
            {"classname": "testenix.collection", "name": issue.path, "time": "0.000000"},
        )
        error = ET.SubElement(case, "error", {"message": issue.message, "type": "collection"})
        error.text = issue.traceback or issue.message

    for result in tests:
        _add_test_case(suite, result)
    return root


def _add_test_case(suite: ET.Element, result: TestResult) -> None:
    test = result.test
    attributes = {
        "classname": test.module_name,
        "name": test.display_name,
        "file": test.path,
        "time": _duration(result.duration),
    }
    if test.source_line is not None:
        attributes["line"] = str(test.source_line)
    case = ET.SubElement(suite, "testcase", attributes)

    properties = ET.SubElement(case, "properties")
    ET.SubElement(properties, "property", {"name": "testenix.id", "value": test.id})
    ET.SubElement(properties, "property", {"name": "testenix.status", "value": result.status.value})
    ET.SubElement(
        properties,
        "property",
        {"name": "testenix.attempts", "value": str(len(result.attempts))},
    )

    phase = _problem_phase(result)
    message = phase.message if phase is not None and phase.message else result.status.value
    problem_type = (
        phase.exception_type if phase is not None and phase.exception_type else result.status.value
    )
    body = phase.traceback if phase is not None and phase.traceback else message

    if result.status in _FAILURES:
        failure = ET.SubElement(case, "failure", {"message": message, "type": problem_type})
        failure.text = body
    elif result.status in _ERRORS:
        error = ET.SubElement(case, "error", {"message": message, "type": problem_type})
        error.text = body
    elif result.status in _SKIPPED:
        reason = test.skip_reason or test.xfail_reason or message
        ET.SubElement(case, "skipped", {"message": reason, "type": result.status.value})

    stdout = _captured_output(result, "stdout")
    stderr = _captured_output(result, "stderr")
    if stdout:
        ET.SubElement(case, "system-out").text = stdout
    if stderr:
        ET.SubElement(case, "system-err").text = stderr


def _problem_phase(result: TestResult) -> PhaseResult | None:
    for attempt in reversed(result.attempts):
        for phase in reversed(attempt.phases):
            if phase.status not in {Status.PASS, Status.CACHED_PASS, Status.SKIP, Status.XFAIL}:
                return phase
    return None


def _captured_output(result: TestResult, field: str) -> str:
    chunks: list[str] = []
    for attempt in sorted(result.attempts, key=lambda item: item.attempt):
        for phase in attempt.phases:
            value = getattr(phase, field)
            if value:
                chunks.append(f"[attempt {attempt.attempt} {phase.phase.value}]\n{value.rstrip()}")
    return "\n".join(chunks)


def _duration(value: float) -> str:
    return f"{max(0.0, value):.6f}"


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
