"""Safe, differential migration from common Python test frameworks.

Migration is deliberately copy-only.  Source files are fingerprinted, generated
artifacts are validated in disposable project shadows, and the destination is
published only after the source suite and both native execution modes agree.
"""

from __future__ import annotations

import ast
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from testenix.migration_models import (
    ConversionBundle,
    DiagnosticSeverity,
    GeneratedArtifact,
    MigrationDiagnostic,
    SourceFile,
    TestMapping,
)

MIGRATION_FORMAT = "testenix.migration-report"
MIGRATION_SCHEMA_VERSION = 1
Framework = Literal["auto", "pytest", "unittest"]
WorkerCount = int | Literal["auto"]


class MigrationStatus(StrEnum):
    """Terminal state of one migration transaction."""

    ANALYZED = "analyzed"
    VALIDATED = "validated"
    PUBLISHED = "published"
    UNSUPPORTED = "unsupported"
    VALIDATION_FAILED = "validation_failed"
    SAFETY_ERROR = "safety_error"


@dataclass(frozen=True, slots=True)
class MigrationOptions:
    """Validated input for :func:`migrate`."""

    framework: Framework
    sources: tuple[Path, ...]
    output: Path = Path("testenix_migrated")
    workers: WorkerCount = "auto"
    validation_timeout: float = 300.0
    dry_run: bool = False
    check_only: bool = False
    project_root: Path | None = None

    def __post_init__(self) -> None:
        if self.framework not in {"auto", "pytest", "unittest"}:
            raise ValueError("framework must be auto, pytest, or unittest")
        if not self.sources:
            raise ValueError("migration needs at least one source path")
        if self.dry_run and self.check_only:
            raise ValueError("dry_run and check_only are mutually exclusive")
        if self.workers != "auto":
            if isinstance(self.workers, bool) or not isinstance(self.workers, int):
                raise ValueError("workers must be 'auto' or an integer")
            if self.workers < 2:
                raise ValueError("migration workers must be at least 2 for a real parallel gate")
        if self.validation_timeout <= 0 or not math.isfinite(self.validation_timeout):
            raise ValueError("validation_timeout must be a finite number greater than zero")
        object.__setattr__(self, "sources", tuple(Path(path) for path in self.sources))
        object.__setattr__(self, "output", Path(self.output))
        if self.project_root is not None:
            object.__setattr__(self, "project_root", Path(self.project_root))

    @property
    def mode(self) -> str:
        if self.dry_run:
            return "dry-run"
        if self.check_only:
            return "check"
        return "publish"


@dataclass(frozen=True, slots=True)
class ValidationSummary:
    """Framework-neutral outcome counts from one isolated validation run."""

    runner: str
    tests: int
    passed: int
    failed: int
    errors: int
    skipped: int
    xfailed: int
    xpassed: int
    exit_code: int
    duration: float
    timed_out: bool = False
    detail: str | None = None
    outcomes: Mapping[str, str] = field(default_factory=dict)

    @property
    def gating(self) -> int:
        return self.failed + self.errors + self.xpassed

    def outcome_signature(self) -> tuple[int, int, int, int, int, int, int]:
        return (
            self.tests,
            self.passed,
            self.failed,
            self.errors,
            self.skipped,
            self.xfailed,
            self.xpassed,
        )


@dataclass(frozen=True, slots=True)
class MigrationReport:
    """Complete audit record for a migration attempt."""

    status: MigrationStatus
    mode: str
    framework: str
    project_root: str
    sources: tuple[str, ...]
    output: str
    source_hashes: Mapping[str, str]
    generated_files: tuple[str, ...]
    mappings: tuple[TestMapping, ...]
    diagnostics: tuple[MigrationDiagnostic, ...]
    baseline: ValidationSummary | None
    native_serial: ValidationSummary | None
    native_parallel: ValidationSummary | None
    originals_modified: bool
    published: bool
    message: str
    started_at: float
    finished_at: float

    @property
    def exit_code(self) -> int:
        if self.status in {
            MigrationStatus.ANALYZED,
            MigrationStatus.VALIDATED,
            MigrationStatus.PUBLISHED,
        }:
            return 0
        if self.status is MigrationStatus.VALIDATION_FAILED:
            return 1
        if self.status is MigrationStatus.SAFETY_ERROR:
            return 2
        return 4

    @property
    def converted_tests(self) -> int:
        return len(self.mappings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline": asdict(self.baseline) if self.baseline is not None else None,
            "converted_tests": self.converted_tests,
            "diagnostics": [
                {
                    "code": diagnostic.code,
                    "line": diagnostic.line,
                    "message": diagnostic.message,
                    "severity": diagnostic.severity.value,
                    "source": diagnostic.source,
                }
                for diagnostic in self.diagnostics
            ],
            "duration": max(0.0, self.finished_at - self.started_at),
            "exit_code": self.exit_code,
            "finished_at": self.finished_at,
            "format": MIGRATION_FORMAT,
            "framework": self.framework,
            "generated_files": list(self.generated_files),
            "mappings": [asdict(mapping) for mapping in self.mappings],
            "message": self.message,
            "mode": self.mode,
            "native_parallel": (
                asdict(self.native_parallel) if self.native_parallel is not None else None
            ),
            "native_serial": (
                asdict(self.native_serial) if self.native_serial is not None else None
            ),
            "originals_modified": self.originals_modified,
            "output": self.output,
            "project_root": self.project_root,
            "published": self.published,
            "schema_version": MIGRATION_SCHEMA_VERSION,
            "source_hashes": dict(sorted(self.source_hashes.items())),
            "sources": list(self.sources),
            "started_at": self.started_at,
            "status": self.status.value,
        }

    def to_json(self) -> str:
        return (
            json.dumps(
                self.to_dict(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        )


@dataclass(frozen=True, slots=True)
class _ProcessOutcome:
    returncode: int
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False


@dataclass(frozen=True, slots=True)
class _ConversionPlan:
    bundle: ConversionBundle
    resolved_framework: str


@dataclass(frozen=True, slots=True)
class _ReportContext:
    framework: str
    project_root: Path
    sources: tuple[str, ...]
    output: Path
    source_hashes: Mapping[str, str]
    generated_files: tuple[str, ...]
    mappings: tuple[TestMapping, ...]
    diagnostics: tuple[MigrationDiagnostic, ...]


def write_migration_report(report: MigrationReport, path: Path) -> None:
    """Publish a migration report atomically without replacing an existing path.

    Reports are audit evidence for a copy-only migration.  Treating their path
    as an ordinary overwrite target could accidentally replace an original
    test file, so publication uses a same-directory temporary file followed by
    an atomic create-only hard link.
    """

    from testenix.migration_fs import MigrationPaths, validate_migration_report_path

    migration_paths = MigrationPaths(
        project_root=Path(report.project_root),
        sources=tuple(Path(source) for source in report.sources),
        output=Path(report.output),
    )
    destination = validate_migration_report_path(migration_paths, path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination = validate_migration_report_path(migration_paths, destination)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            delete=False,
        ) as temporary:
            temporary.write(report.to_json())
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        try:
            os.link(temporary_name, destination, follow_symlinks=False)
        except FileExistsError as error:
            raise FileExistsError(
                f"migration report already exists and was not replaced: {destination}"
            ) from error
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def render_migration_summary(report: MigrationReport) -> str:
    """Render a compact, human-readable transaction result."""

    headline = {
        MigrationStatus.ANALYZED: "migration analysis passed",
        MigrationStatus.VALIDATED: "migration validation passed; output was not published",
        MigrationStatus.PUBLISHED: "migration validated and published",
        MigrationStatus.UNSUPPORTED: "migration stopped on unsupported source constructs",
        MigrationStatus.VALIDATION_FAILED: "migration validation failed; no output was published",
        MigrationStatus.SAFETY_ERROR: "migration rejected an unsafe path or concurrent change",
    }[report.status]
    inventory_label = {
        MigrationStatus.ANALYZED: "analyzed candidate",
        MigrationStatus.VALIDATED: "validated candidate",
        MigrationStatus.PUBLISHED: "converted",
        MigrationStatus.UNSUPPORTED: "statically convertible subset",
        MigrationStatus.VALIDATION_FAILED: "generated candidate",
        MigrationStatus.SAFETY_ERROR: "generated candidate",
    }[report.status]
    lines = [
        f"Testenix: {headline}",
        f"  {inventory_label}: {report.converted_tests} tests in "
        f"{len(report.generated_files)} files",
        f"  originals modified: {'yes' if report.originals_modified else 'no'}",
        f"  output: {report.output}",
        f"  detail: {report.message}",
    ]
    if report.baseline is not None:
        lines.append(_summary_line("source baseline", report.baseline))
    if report.native_serial is not None:
        lines.append(_summary_line("native serial", report.native_serial))
    if report.native_parallel is not None:
        lines.append(_summary_line("native parallel", report.native_parallel))
    lines.extend(_diagnostic_summary(report.diagnostics))
    return "\n".join(lines)


def _diagnostic_summary(
    diagnostics: Sequence[MigrationDiagnostic],
) -> tuple[str, ...]:
    """Group repeated console diagnostics without losing JSON report detail."""

    if not diagnostics:
        return ()

    counts = Counter(diagnostic.severity for diagnostic in diagnostics)
    groups: dict[tuple[DiagnosticSeverity, str], list[MigrationDiagnostic]] = {}
    for diagnostic in diagnostics:
        groups.setdefault((diagnostic.severity, diagnostic.code), []).append(diagnostic)

    error_count = counts[DiagnosticSeverity.ERROR]
    warning_count = counts[DiagnosticSeverity.WARNING]
    lines = [
        f"  diagnostics: {error_count} error(s), {warning_count} warning(s), {len(groups)} code(s)"
    ]
    severity_order = {
        DiagnosticSeverity.ERROR: 0,
        DiagnosticSeverity.WARNING: 1,
    }
    for (severity, code), members in sorted(
        groups.items(),
        key=lambda item: (severity_order[item[0][0]], item[0][1]),
    ):
        first = members[0]
        location = first.source
        if first.line is not None:
            location = f"{location}:{first.line}"
        if len(members) == 1:
            lines.append(f"  {severity.value.upper()} {code} {location}: {first.message}")
            continue
        source_count = len({diagnostic.source for diagnostic in members})
        lines.append(
            f"  {severity.value.upper()} {code}: {len(members)} occurrence(s) in "
            f"{source_count} file(s); first at {location}: {first.message}"
        )

    if len(diagnostics) > len(groups):
        lines.append("  diagnostic detail: --report-json FILE|- retains every line-addressed entry")
    return tuple(lines)


def _summary_line(label: str, summary: ValidationSummary) -> str:
    return (
        f"  {label}: {summary.tests} tests, {summary.passed} passed, "
        f"{summary.skipped} skipped, {summary.xfailed} xfailed, "
        f"{summary.gating} gating ({summary.duration:.3f}s)"
    )


def migrate(options: MigrationOptions) -> MigrationReport:
    """Analyze, validate, and optionally publish a native Testenix copy.

    No branch of this function writes to a source path.  A destination is only
    published by a no-overwrite rename after the source fingerprints have been
    checked for concurrent changes.
    """

    from testenix.migration_fs import (
        MigrationFilesystemError,
        PublishedOutputDurabilityError,
        SourceChangedError,
        atomic_publish,
        cleanup_publish_staging,
        copy_project_to_shadow,
        create_publish_staging,
        snapshot_source_files,
        validate_migration_paths,
        verify_source_snapshot,
        write_staged_artifacts,
    )

    started_at = time.time()
    project_root = (options.project_root or Path.cwd()).expanduser().resolve()
    try:
        paths = validate_migration_paths(
            project_root,
            options.sources,
            options.output,
        )
        source_files = snapshot_source_files(paths, include_all_python=True)
    except MigrationFilesystemError as error:
        context = _ReportContext(
            framework=options.framework,
            project_root=project_root,
            sources=tuple(str(path) for path in options.sources),
            output=options.output,
            source_hashes={},
            generated_files=(),
            mappings=(),
            diagnostics=(
                MigrationDiagnostic(
                    code="MIG001",
                    message=str(error),
                    source="<migration>",
                ),
            ),
        )
        return _terminal_report(
            options,
            started_at=started_at,
            status=MigrationStatus.SAFETY_ERROR,
            message=str(error),
            context=context,
        )

    plan = _convert_sources(
        options.framework,
        source_files,
        output_relative=paths.output.relative_to(paths.project_root),
    )
    bundle = plan.bundle
    if plan.resolved_framework in {"pytest", "mixed"}:
        from testenix.migration_pytest_config import pytest_asyncio_config_diagnostics

        # This is a static preflight over the exact source invocation. The serial and
        # parallel shadow runs below remain the authoritative behavioral gates.
        asyncio_config_diagnostics = pytest_asyncio_config_diagnostics(
            project_root=paths.project_root,
            source_paths=paths.sources,
            files=source_files,
        )
        if asyncio_config_diagnostics:
            bundle = _merge_bundles(
                bundle,
                ConversionBundle(diagnostics=asyncio_config_diagnostics),
            )
    source_hashes = {source.project_relative.as_posix(): source.sha256 for source in source_files}
    generated_files = tuple(artifact.relative_path.as_posix() for artifact in bundle.artifacts)
    diagnostics = bundle.diagnostics
    context = _ReportContext(
        framework=plan.resolved_framework,
        project_root=paths.project_root,
        sources=tuple(path.relative_to(paths.project_root).as_posix() for path in paths.sources),
        output=paths.output,
        source_hashes=source_hashes,
        generated_files=generated_files,
        mappings=bundle.mappings,
        diagnostics=diagnostics,
    )

    if bundle.blocking_diagnostics:
        return _verified_terminal_report(
            options,
            paths=paths,
            source_files=source_files,
            started_at=started_at,
            status=MigrationStatus.UNSUPPORTED,
            message=(
                f"{len(bundle.blocking_diagnostics)} unsupported construct(s); "
                "the destination was not created"
            ),
            context=context,
        )
    if not bundle.mappings:
        diagnostic = MigrationDiagnostic(
            code="MIG002",
            message="no convertible tests were found in the selected source paths",
            source="<migration>",
        )
        return _verified_terminal_report(
            options,
            paths=paths,
            source_files=source_files,
            started_at=started_at,
            status=MigrationStatus.UNSUPPORTED,
            message=diagnostic.message,
            context=replace(context, diagnostics=(*diagnostics, diagnostic)),
        )
    if options.dry_run:
        try:
            verify_source_snapshot(paths, source_files, include_all_python=True)
        except MigrationFilesystemError as error:
            return _source_changed_report(options, started_at, context, str(error))
        return _terminal_report(
            options,
            started_at=started_at,
            status=MigrationStatus.ANALYZED,
            message="all selected constructs are supported; no tests were run and no files written",
            context=context,
        )

    affinity_units = {mapping.target_file for mapping in bundle.mappings}
    if len(affinity_units) < 2:
        diagnostic = MigrationDiagnostic(
            code="MIG006",
            message=(
                "the parallel validation command is configured with at least two workers, "
                "but this converted suite has one module affinity unit and therefore "
                "executes on one worker"
            ),
            source="<migration>",
            severity=DiagnosticSeverity.WARNING,
        )
        diagnostics = (*diagnostics, diagnostic)
        context = replace(context, diagnostics=diagnostics)

    baseline: ValidationSummary | None = None
    native_serial: ValidationSummary | None = None
    native_parallel: ValidationSummary | None = None
    shadows: list[Path] = []
    try:
        baseline_shadow = copy_project_to_shadow(paths.project_root)
        shadows.append(baseline_shadow)
        baseline = _run_source_baseline(
            plan.resolved_framework,
            baseline_shadow,
            paths,
            bundle.mappings,
            options.validation_timeout,
        )
        baseline_problem = _baseline_problem(baseline, len(bundle.mappings))
        if baseline_problem is not None:
            return _verified_terminal_report(
                options,
                paths=paths,
                source_files=source_files,
                started_at=started_at,
                status=MigrationStatus.VALIDATION_FAILED,
                message=baseline_problem,
                context=context,
                baseline=baseline,
            )

        serial_shadow = copy_project_to_shadow(paths.project_root)
        shadows.append(serial_shadow)
        native_serial = _run_native_candidate(
            serial_shadow,
            paths,
            bundle.artifacts,
            bundle.mappings,
            workers=1,
            timeout=options.validation_timeout,
        )
        serial_problem = _candidate_problem("serial", baseline, native_serial)
        if serial_problem is not None:
            return _verified_terminal_report(
                options,
                paths=paths,
                source_files=source_files,
                started_at=started_at,
                status=MigrationStatus.VALIDATION_FAILED,
                message=serial_problem,
                context=context,
                baseline=baseline,
                native_serial=native_serial,
            )

        parallel_shadow = copy_project_to_shadow(paths.project_root)
        shadows.append(parallel_shadow)
        native_parallel = _run_native_candidate(
            parallel_shadow,
            paths,
            bundle.artifacts,
            bundle.mappings,
            workers=_parallel_validation_workers(options.workers),
            timeout=options.validation_timeout,
        )
        parallel_problem = _candidate_problem("parallel", baseline, native_parallel)
        if parallel_problem is None and (
            native_serial.outcome_signature() != native_parallel.outcome_signature()
        ):
            parallel_problem = "serial and parallel native outcomes differ"
        if parallel_problem is not None:
            return _verified_terminal_report(
                options,
                paths=paths,
                source_files=source_files,
                started_at=started_at,
                status=MigrationStatus.VALIDATION_FAILED,
                message=parallel_problem,
                context=context,
                baseline=baseline,
                native_serial=native_serial,
                native_parallel=native_parallel,
            )

        try:
            verify_source_snapshot(paths, source_files, include_all_python=True)
        except MigrationFilesystemError as error:
            return _source_changed_report(
                options,
                started_at,
                context,
                str(error),
                baseline=baseline,
                native_serial=native_serial,
                native_parallel=native_parallel,
            )

        if options.check_only:
            return _terminal_report(
                options,
                started_at=started_at,
                status=MigrationStatus.VALIDATED,
                message="source and native outcomes match; check mode left the destination absent",
                context=context,
                baseline=baseline,
                native_serial=native_serial,
                native_parallel=native_parallel,
            )

        staging_root = create_publish_staging(paths)
        publish_error: MigrationFilesystemError | OSError | None = None
        durability_warning: PublishedOutputDurabilityError | None = None
        cleanup_error: MigrationFilesystemError | OSError | None = None
        committed = False
        try:
            write_staged_artifacts(staging_root, bundle.artifacts)
            verify_source_snapshot(paths, source_files, include_all_python=True)
            atomic_publish(staging_root, paths)
            committed = True
        except PublishedOutputDurabilityError as error:
            committed = True
            durability_warning = error
        except (MigrationFilesystemError, OSError) as error:
            publish_error = error
        finally:
            try:
                cleanup_publish_staging(staging_root, paths)
            except (MigrationFilesystemError, OSError) as error:
                cleanup_error = error

        if publish_error is not None:
            message = str(publish_error)
            if cleanup_error is not None:
                message += f"; staging cleanup also failed: {cleanup_error}"
            if isinstance(publish_error, SourceChangedError):
                return _source_changed_report(
                    options,
                    started_at,
                    context,
                    message,
                    baseline=baseline,
                    native_serial=native_serial,
                    native_parallel=native_parallel,
                )
            return _verified_terminal_report(
                options,
                paths=paths,
                source_files=source_files,
                started_at=started_at,
                status=MigrationStatus.SAFETY_ERROR,
                message=f"{message}; the destination was not published",
                context=replace(
                    context,
                    diagnostics=(
                        *context.diagnostics,
                        MigrationDiagnostic(
                            code="MIG004",
                            message=message,
                            source="<migration>",
                        ),
                    ),
                ),
                baseline=baseline,
                native_serial=native_serial,
                native_parallel=native_parallel,
            )

        if not committed:
            raise RuntimeError("migration publication ended without a terminal state")
        if durability_warning is not None:
            context = replace(
                context,
                diagnostics=(
                    *context.diagnostics,
                    MigrationDiagnostic(
                        code="MIG004",
                        message=str(durability_warning),
                        source="<migration>",
                        severity=DiagnosticSeverity.WARNING,
                    ),
                ),
            )
        if cleanup_error is not None:
            cleanup_diagnostic = MigrationDiagnostic(
                code="MIG004",
                message=(
                    f"published output is complete, but staging cleanup failed: {cleanup_error}"
                ),
                source="<migration>",
                severity=DiagnosticSeverity.WARNING,
            )
            context = replace(
                context,
                diagnostics=(*context.diagnostics, cleanup_diagnostic),
            )

        publication_message = (
            "validated candidate was atomically published; original tests remain untouched"
        )
        if durability_warning is not None:
            publication_message += "; directory durability could not be confirmed"
        return _terminal_report(
            options,
            started_at=started_at,
            status=MigrationStatus.PUBLISHED,
            message=publication_message,
            context=context,
            baseline=baseline,
            native_serial=native_serial,
            native_parallel=native_parallel,
            published=True,
        )
    except MigrationFilesystemError as error:
        return _verified_terminal_report(
            options,
            paths=paths,
            source_files=source_files,
            started_at=started_at,
            status=MigrationStatus.SAFETY_ERROR,
            message=f"{error}; the destination was not published",
            context=replace(
                context,
                diagnostics=(
                    *context.diagnostics,
                    MigrationDiagnostic(
                        code="MIG004",
                        message=str(error),
                        source="<migration>",
                    ),
                ),
            ),
            baseline=baseline,
            native_serial=native_serial,
            native_parallel=native_parallel,
        )
    finally:
        for shadow in shadows:
            shutil.rmtree(shadow, ignore_errors=True)


def _terminal_report(
    options: MigrationOptions,
    *,
    started_at: float,
    status: MigrationStatus,
    message: str,
    context: _ReportContext,
    baseline: ValidationSummary | None = None,
    native_serial: ValidationSummary | None = None,
    native_parallel: ValidationSummary | None = None,
    originals_modified: bool = False,
    published: bool = False,
) -> MigrationReport:
    return MigrationReport(
        status=status,
        mode=options.mode,
        framework=context.framework,
        project_root=str(context.project_root),
        sources=context.sources,
        output=str(context.output),
        source_hashes=dict(context.source_hashes),
        generated_files=context.generated_files,
        mappings=context.mappings,
        diagnostics=context.diagnostics,
        baseline=baseline,
        native_serial=native_serial,
        native_parallel=native_parallel,
        originals_modified=originals_modified,
        published=published,
        message=message,
        started_at=started_at,
        finished_at=time.time(),
    )


def _source_changed_report(
    options: MigrationOptions,
    started_at: float,
    context: _ReportContext,
    message: str,
    *,
    baseline: ValidationSummary | None = None,
    native_serial: ValidationSummary | None = None,
    native_parallel: ValidationSummary | None = None,
) -> MigrationReport:
    diagnostic = MigrationDiagnostic(
        code="MIG003",
        message=message,
        source="<migration>",
    )
    return _terminal_report(
        options,
        started_at=started_at,
        status=MigrationStatus.SAFETY_ERROR,
        message=f"{message}; the destination was not published",
        context=replace(context, diagnostics=(*context.diagnostics, diagnostic)),
        baseline=baseline,
        native_serial=native_serial,
        native_parallel=native_parallel,
        originals_modified=True,
    )


def _verified_terminal_report(
    options: MigrationOptions,
    *,
    paths: Any,
    source_files: Sequence[SourceFile],
    started_at: float,
    status: MigrationStatus,
    message: str,
    context: _ReportContext,
    baseline: ValidationSummary | None = None,
    native_serial: ValidationSummary | None = None,
    native_parallel: ValidationSummary | None = None,
    published: bool = False,
) -> MigrationReport:
    """Return a terminal report only after rechecking immutable source inputs."""

    from testenix.migration_fs import MigrationFilesystemError, verify_source_snapshot

    try:
        verify_source_snapshot(paths, source_files, include_all_python=True)
    except MigrationFilesystemError as error:
        return _source_changed_report(
            options,
            started_at,
            context,
            str(error),
            baseline=baseline,
            native_serial=native_serial,
            native_parallel=native_parallel,
        )
    return _terminal_report(
        options,
        started_at=started_at,
        status=status,
        message=message,
        context=context,
        baseline=baseline,
        native_serial=native_serial,
        native_parallel=native_parallel,
        published=published,
    )


def _parallel_validation_workers(workers: WorkerCount) -> int:
    if workers == "auto":
        return max(2, os.cpu_count() or 1)
    return max(2, workers)


def _convert_sources(
    framework: Framework,
    files: Sequence[SourceFile],
    *,
    output_relative: Path,
) -> _ConversionPlan:
    from testenix.migration_pytest import convert_pytest_suite, detect_pytest_module
    from testenix.migration_unittest import convert_unittest_suite, detect_unittest_module

    conftest_files = tuple(file for file in files if file.path.name == "conftest.py")
    test_files = tuple(
        file
        for file in files
        if file.path.name != "conftest.py"
        and (_looks_like_test_module(file.path.name) or _contains_static_test_inventory(file))
    )
    test_file_paths = {file.path for file in test_files}
    support_files = tuple(
        file
        for file in files
        if file.path.name != "conftest.py" and file.path not in test_file_paths
    )
    passthrough = ConversionBundle(
        artifacts=tuple(
            GeneratedArtifact(
                relative_path=file.migration_relative,
                content=file.text,
                source_files=(file.project_relative.as_posix(),),
            )
            for file in support_files
        )
    )

    if framework == "pytest":
        converted = convert_pytest_suite(test_files, conftest_files)
        return _ConversionPlan(_merge_bundles(converted, passthrough), "pytest")
    if framework == "unittest":
        converted = convert_unittest_suite(
            test_files,
            output_relative=output_relative,
            manifest_files=files,
        )
        conftest_warning = _ignored_conftest_bundle(conftest_files)
        return _ConversionPlan(
            _merge_bundles(converted, passthrough, conftest_warning),
            "unittest",
        )

    pytest_files: list[SourceFile] = []
    unittest_files: list[SourceFile] = []
    auto_support_files: list[SourceFile] = []
    diagnostics: list[MigrationDiagnostic] = []
    for source in test_files:
        is_pytest = detect_pytest_module(source)
        is_unittest = detect_unittest_module(source)
        has_separate_pytest_inventory = is_unittest and _has_non_unittest_inventory(source)
        if is_pytest and is_unittest and has_separate_pytest_inventory:
            diagnostics.append(
                MigrationDiagnostic(
                    code="MIG101",
                    message=(
                        "one module mixes pytest-style functions and unittest.TestCase; "
                        "split it before automatic migration"
                    ),
                    source=source.project_relative.as_posix(),
                )
            )
        elif is_unittest:
            unittest_files.append(source)
        elif is_pytest:
            pytest_files.append(source)
        elif not _contains_static_test_inventory(source):
            auto_support_files.append(source)
        else:
            diagnostics.append(
                MigrationDiagnostic(
                    code="MIG102",
                    message="cannot identify a supported pytest or unittest test module",
                    source=source.project_relative.as_posix(),
                )
            )

    auto_passthrough = ConversionBundle(
        artifacts=tuple(
            GeneratedArtifact(
                relative_path=file.migration_relative,
                content=file.text,
                source_files=(file.project_relative.as_posix(),),
            )
            for file in auto_support_files
        )
    )
    bundles: list[ConversionBundle] = [passthrough, auto_passthrough]
    if pytest_files:
        bundles.append(convert_pytest_suite(tuple(pytest_files), conftest_files))
    elif conftest_files:
        bundles.append(_ignored_conftest_bundle(conftest_files))
    if unittest_files:
        bundles.append(
            convert_unittest_suite(
                tuple(unittest_files),
                output_relative=output_relative,
                manifest_files=files,
            )
        )
    if diagnostics:
        bundles.append(ConversionBundle(diagnostics=tuple(diagnostics)))
    resolved = (
        "mixed"
        if pytest_files and unittest_files
        else ("pytest" if pytest_files else "unittest" if unittest_files else "auto")
    )
    return _ConversionPlan(_merge_bundles(*bundles), resolved)


def _looks_like_test_module(name: str) -> bool:
    return name.endswith(".py") and (name.startswith("test") or name.endswith("_test.py"))


def _contains_static_test_inventory(source: SourceFile) -> bool:
    """Recognize explicit nonstandard filenames without importing their code."""

    try:
        tree = ast.parse(source.text, filename=source.project_relative.as_posix())
    except SyntaxError:
        return False
    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if statement.name.startswith("test"):
                return True
        elif isinstance(statement, ast.ClassDef) and any(
            isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
            and member.name.startswith("test")
            for member in statement.body
        ):
            return True
    return False


def _has_non_unittest_inventory(source: SourceFile) -> bool:
    """Distinguish a real mixed module from pytest seeing unittest method names."""

    try:
        tree = ast.parse(source.text, filename=source.project_relative.as_posix())
    except SyntaxError:
        return True
    unittest_modules = {"unittest"}
    unittest_bases = {"TestCase", "IsolatedAsyncioTestCase"}
    for statement in tree.body:
        if isinstance(statement, ast.Import):
            for imported in statement.names:
                if imported.name == "unittest":
                    unittest_modules.add(imported.asname or imported.name)
        elif isinstance(statement, ast.ImportFrom) and statement.module in {
            "unittest",
            "unittest.case",
            "unittest.async_case",
        }:
            for imported in statement.names:
                if imported.name in {"TestCase", "IsolatedAsyncioTestCase"}:
                    unittest_bases.add(imported.asname or imported.name)
    for statement in tree.body:
        if isinstance(
            statement, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) and statement.name.startswith("test_"):
            return True
        if not isinstance(statement, ast.ClassDef):
            continue
        has_tests = any(
            isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
            and member.name.startswith("test")
            for member in statement.body
        )
        if not has_tests:
            continue
        base_names = {ast.unparse(base) for base in statement.bases}
        direct_unittest = any(
            name in unittest_bases
            or any(
                name in {f"{module}.TestCase", f"{module}.IsolatedAsyncioTestCase"}
                for module in unittest_modules
            )
            for name in base_names
        )
        if not direct_unittest:
            return True
    return False


def _ignored_conftest_bundle(files: Sequence[SourceFile]) -> ConversionBundle:
    return ConversionBundle(
        diagnostics=tuple(
            MigrationDiagnostic(
                code="UNIT090",
                message="conftest.py is ignored by a unittest source run",
                source=file.project_relative.as_posix(),
                severity=DiagnosticSeverity.WARNING,
            )
            for file in files
        )
    )


def _merge_bundles(*bundles: ConversionBundle) -> ConversionBundle:
    artifacts: list[GeneratedArtifact] = []
    mappings: list[TestMapping] = []
    diagnostics: list[MigrationDiagnostic] = []
    artifact_paths: set[Path] = set()
    mapping_sources: set[tuple[str, str | None]] = set()
    for bundle in bundles:
        for artifact in bundle.artifacts:
            if artifact.relative_path in artifact_paths:
                diagnostics.append(
                    MigrationDiagnostic(
                        code="MIG103",
                        message=(
                            f"two generated artifacts target {artifact.relative_path.as_posix()}"
                        ),
                        source=", ".join(artifact.source_files) or "<migration>",
                    )
                )
                continue
            artifacts.append(artifact)
            artifact_paths.add(artifact.relative_path)
        for mapping in bundle.mappings:
            mapping_identity = (mapping.source_id, mapping.case_id)
            if mapping_identity in mapping_sources:
                diagnostics.append(
                    MigrationDiagnostic(
                        code="MIG104",
                        message=f"duplicate source test identity {mapping.source_id}",
                        source=mapping.source_id.split("::", 1)[0],
                    )
                )
                continue
            mappings.append(mapping)
            mapping_sources.add(mapping_identity)
        diagnostics.extend(bundle.diagnostics)
    return ConversionBundle(
        artifacts=tuple(sorted(artifacts, key=lambda item: item.relative_path.as_posix())),
        mappings=tuple(sorted(mappings, key=lambda item: item.source_id)),
        diagnostics=tuple(
            sorted(
                diagnostics,
                key=lambda item: (item.source, item.line or 0, item.code, item.message),
            )
        ),
    )


def _run_source_baseline(
    framework: str,
    shadow_root: Path,
    paths: Any,
    mappings: Sequence[TestMapping],
    timeout: float,
) -> ValidationSummary:
    shadow_sources = [
        str(shadow_root / source.relative_to(paths.project_root)) for source in paths.sources
    ]
    report_path = shadow_root / ".testenix-migration-baseline.json"
    if framework in {"pytest", "mixed"}:
        junit_path = shadow_root / ".testenix-migration-baseline.xml"
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--tb=short",
            f"--junitxml={junit_path}",
            *shadow_sources,
        ]
        outcome = _run_process(command, cwd=shadow_root, timeout=timeout)
        return _pytest_summary(junit_path, outcome, mappings)

    command = [
        sys.executable,
        "-m",
        "testenix._unittest_probe",
        "--output",
        str(report_path),
        *shadow_sources,
    ]
    outcome = _run_process(command, cwd=shadow_root, timeout=timeout)
    return _unittest_summary(report_path, outcome, mappings)


def _run_native_candidate(
    shadow_root: Path,
    paths: Any,
    artifacts: Sequence[GeneratedArtifact],
    mappings: Sequence[TestMapping],
    *,
    workers: WorkerCount,
    timeout: float,
) -> ValidationSummary:
    from testenix.migration_fs import write_staged_artifacts

    output_relative = paths.output.relative_to(paths.project_root)
    candidate_root = shadow_root / output_relative
    candidate_root.mkdir(parents=True, exist_ok=False)
    write_staged_artifacts(candidate_root, artifacts, private_shadow=True)
    report_path = shadow_root / f".testenix-migration-native-{workers}.json"
    command = [
        sys.executable,
        "-m",
        "testenix._migration_native_probe",
        "--output",
        str(report_path),
        "--workers",
        str(workers),
        str(candidate_root),
    ]
    outcome = _run_process(command, cwd=shadow_root, timeout=timeout)
    return _native_summary(
        report_path,
        outcome,
        runner=f"testenix-{workers}",
        mappings=mappings,
    )


def _baseline_problem(summary: ValidationSummary, expected_tests: int) -> str | None:
    if summary.timed_out:
        return f"source baseline timed out after {summary.duration:.3f}s"
    if summary.exit_code != 0 or summary.gating:
        detail = f": {summary.detail}" if summary.detail else ""
        return f"source baseline is not green (exit {summary.exit_code}){detail}"
    if summary.tests != expected_tests:
        return (
            f"converter mapped {expected_tests} tests but the source runner collected "
            f"{summary.tests}"
        )
    return None


def _candidate_problem(
    label: str,
    baseline: ValidationSummary,
    candidate: ValidationSummary,
) -> str | None:
    if candidate.timed_out:
        return f"native {label} validation timed out after {candidate.duration:.3f}s"
    if candidate.exit_code != 0 or candidate.gating:
        detail = f": {candidate.detail}" if candidate.detail else ""
        return f"native {label} candidate is not green (exit {candidate.exit_code}){detail}"
    if candidate.outcome_signature() != baseline.outcome_signature():
        return (
            f"native {label} outcome counts differ from the source baseline: "
            f"source={baseline.outcome_signature()}, native={candidate.outcome_signature()}"
        )
    if dict(candidate.outcomes) != dict(baseline.outcomes):
        source_outcomes = dict(baseline.outcomes)
        candidate_outcomes = dict(candidate.outcomes)
        differing = sorted(
            test_id
            for test_id in set(source_outcomes).union(candidate_outcomes)
            if source_outcomes.get(test_id) != candidate_outcomes.get(test_id)
        )
        preview = ", ".join(differing[:5])
        suffix = "…" if len(differing) > 5 else ""
        return f"native {label} per-test outcomes differ for: {preview}{suffix}"
    return None


def _run_process(command: Sequence[str], *, cwd: Path, timeout: float) -> _ProcessOutcome:
    environment = os.environ.copy()
    package_root = str(Path(__file__).resolve().parents[1])
    environment["PYTHONPATH"] = package_root
    environment["PWD"] = str(cwd)
    environment.pop("OLDPWD", None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["TESTENIX_MIGRATION_VALIDATION"] = "1"
    popen_options: dict[str, Any] = {}
    if os.name == "nt":
        popen_options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_options["start_new_session"] = True

    started = time.perf_counter()
    process = subprocess.Popen(
        tuple(command),
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **popen_options,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as timeout_error:
        _terminate_process_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=1.0)
        except subprocess.TimeoutExpired as drain_error:
            stdout = _timeout_text(drain_error.output or timeout_error.output)
            stderr = _timeout_text(drain_error.stderr or timeout_error.stderr)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            with suppress(OSError):
                process.kill()
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=0.5)
        return _ProcessOutcome(
            returncode=process.returncode if process.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
            duration=time.perf_counter() - started,
            timed_out=True,
        )
    except BaseException:
        _terminate_process_tree(process)
        process.communicate()
        raise
    return _ProcessOutcome(
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
        duration=time.perf_counter() - started,
    )


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        subprocess.run(
            ("taskkill", "/PID", str(process.pid), "/T", "/F"),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        return

    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        process.poll()
        try:
            os.killpg(process.pid, 0)
        except OSError:
            break
        time.sleep(0.02)
    with suppress(OSError):
        os.killpg(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        process.kill()


def _mapping_source_base(mapping: TestMapping) -> tuple[str, str]:
    source_id = mapping.source_id
    if mapping.case_id is not None and source_id.endswith("]"):
        source_id = source_id.rsplit("[", 1)[0]
    path, separator, function = source_id.partition("::")
    if not separator or not path or not function:
        raise ValueError(f"invalid source test identity: {mapping.source_id}")
    return path, function


def _pytest_case_status(case: ET.Element) -> str:
    if case.find("failure") is not None:
        return "fail"
    if case.find("error") is not None:
        return "error"
    skipped = case.find("skipped")
    if skipped is not None:
        kind = skipped.attrib.get("type", "").lower()
        message = skipped.attrib.get("message", "").lower()
        return "xfail" if "xfail" in kind or "expected failure" in message else "skip"
    return "pass"


def _pytest_class_matches(classname: str, source_path: str, function: str) -> bool:
    normalized = source_path.replace("\\", "/")
    module_name = normalized.removesuffix(".py").replace("/", ".")
    observed_module = classname
    if "." in function:
        class_name = function.rsplit(".", 1)[0]
        if not classname.endswith(f".{class_name}"):
            return False
        observed_module = classname[: -(len(class_name) + 1)]
    return (
        observed_module == module_name
        or observed_module.endswith(f".{module_name}")
        or module_name.endswith(f".{observed_module}")
    )


def _map_pytest_outcomes(
    root: ET.Element,
    mappings: Sequence[TestMapping],
) -> tuple[dict[str, str], str | None]:
    cases = list(root.findall(".//testcase"))
    grouped: dict[tuple[str, str], list[TestMapping]] = {}
    try:
        for mapping in mappings:
            grouped.setdefault(_mapping_source_base(mapping), []).append(mapping)
    except ValueError as error:
        return {}, f"cannot map pytest outcomes: {error}"

    outcomes: dict[str, str] = {}
    used_cases: set[int] = set()
    for (source_path, function), group in grouped.items():
        junit_function = function.rsplit(".", 1)[-1]
        candidate_indexes = [
            index
            for index, case in enumerate(cases)
            if index not in used_cases
            and _pytest_class_matches(case.attrib.get("classname", ""), source_path, function)
            and (
                case.attrib.get("name", "") == junit_function
                or case.attrib.get("name", "").startswith(f"{junit_function}[")
            )
        ]
        if len(candidate_indexes) != len(group):
            return (
                {},
                "cannot map pytest outcomes for "
                f"{source_path}::{function}: expected {len(group)}, found {len(candidate_indexes)}",
            )

        remaining = list(candidate_indexes)
        explicit = [
            mapping
            for mapping in group
            if mapping.case_id is not None and re.fullmatch(r"case-\d{4}", mapping.case_id) is None
        ]
        for mapping in explicit:
            matching = [
                index
                for index in remaining
                if cases[index].attrib.get("name", "") == f"{junit_function}[{mapping.case_id}]"
            ]
            if len(matching) != 1:
                return {}, f"cannot map pytest case outcome for {mapping.source_id}"
            selected = matching[0]
            remaining.remove(selected)
            used_cases.add(selected)
            outcomes[mapping.source_id] = _pytest_case_status(cases[selected])

        implicit = [mapping for mapping in group if mapping not in explicit]
        implicit.sort(key=lambda mapping: mapping.case_id or "")
        if len(remaining) != len(implicit):
            return {}, f"cannot map pytest implicit case outcomes for {source_path}::{function}"
        for mapping, selected in zip(implicit, remaining, strict=True):
            used_cases.add(selected)
            outcomes[mapping.source_id] = _pytest_case_status(cases[selected])

    if len(outcomes) != len(mappings) or len(used_cases) != len(cases):
        return {}, "pytest per-test outcome inventory does not match converter mappings"
    return outcomes, None


def _map_unittest_outcomes(
    raw_outcomes: Sequence[Any],
    mappings: Sequence[TestMapping],
) -> tuple[dict[str, str], str | None]:
    materialized: list[tuple[str, str]] = []
    try:
        for item in raw_outcomes:
            test_id = str(item["id"])
            status = str(item["status"])
            if status not in {"pass", "fail", "error", "skip", "xfail", "xpass"}:
                raise ValueError(f"unknown unittest status {status!r}")
            materialized.append((test_id, status))
    except (KeyError, TypeError, ValueError) as error:
        return {}, f"cannot map unittest outcomes: {error}"

    outcomes: dict[str, str] = {}
    used: set[int] = set()
    for mapping in mappings:
        try:
            source_path, tail = _mapping_source_base(mapping)
        except ValueError as error:
            return {}, f"cannot map unittest outcomes: {error}"
        source_module_parts = Path(source_path).with_suffix("").parts
        candidates: list[tuple[int, int]] = []
        for index, (test_id, _) in enumerate(materialized):
            if index in used or not test_id.endswith(f".{tail}"):
                continue
            module_id = test_id[: -(len(tail) + 1)]
            score = _unittest_module_match_score(module_id, source_module_parts)
            if score:
                candidates.append((score, index))
        if not candidates:
            return {}, f"cannot map unittest case outcome for {mapping.source_id}"
        best_score = max(score for score, _ in candidates)
        best = [index for score, index in candidates if score == best_score]
        if len(best) != 1:
            return {}, f"cannot map unittest case outcome for {mapping.source_id}"
        selected = best[0]
        used.add(selected)
        outcomes[mapping.source_id] = materialized[selected][1]
    if len(used) != len(materialized):
        return {}, "unittest per-test outcome inventory does not match converter mappings"
    return outcomes, None


def _unittest_module_match_score(module_id: str, source_parts: Sequence[str]) -> int:
    """Return the longest common dotted-module suffix length."""

    observed = tuple(part for part in module_id.split(".") if part)
    maximum = min(len(observed), len(source_parts))
    for length in range(maximum, 0, -1):
        if observed[-length:] == tuple(source_parts[-length:]):
            return length
    return 0


def _native_status(value: str) -> str:
    if value in {"pass", "cached_pass"}:
        return "pass"
    if value in {"fail", "flaky"}:
        return "fail"
    if value in {"skip", "xfail", "xpass"}:
        return value
    return "error"


def _map_native_outcomes(
    raw_tests: Sequence[Any],
    mappings: Sequence[TestMapping],
) -> tuple[dict[str, str], str | None]:
    outcomes: dict[str, str] = {}
    used: set[int] = set()
    for mapping in mappings:
        candidates: list[int] = []
        for index, item in enumerate(raw_tests):
            if index in used:
                continue
            try:
                test = item["test"]
                path = str(test["path"]).replace("\\", "/")
                function_name = str(test["function_name"])
                case_id = test.get("case_id")
            except (KeyError, TypeError, AttributeError) as error:
                return {}, f"cannot map native outcomes: {error}"
            if (
                function_name == mapping.target_function
                and (path == mapping.target_file or path.endswith(f"/{mapping.target_file}"))
                and case_id == mapping.case_id
            ):
                candidates.append(index)
        if len(candidates) != 1:
            return {}, f"cannot map native case outcome for {mapping.source_id}"
        selected = candidates[0]
        used.add(selected)
        outcomes[mapping.source_id] = _native_status(str(raw_tests[selected]["status"]))
    if len(used) != len(raw_tests):
        return {}, "native per-test outcome inventory does not match converter mappings"
    return outcomes, None


def _pytest_summary(
    path: Path,
    outcome: _ProcessOutcome,
    mappings: Sequence[TestMapping],
) -> ValidationSummary:
    if outcome.timed_out:
        return _failed_summary("pytest", outcome, "validation process timed out")
    try:
        root = ET.parse(path).getroot()
        attributes = root.attrib
        if "tests" not in attributes:
            suites = root.findall("./testsuite")
            tests = sum(int(suite.attrib.get("tests", "0")) for suite in suites)
            failures = sum(int(suite.attrib.get("failures", "0")) for suite in suites)
            errors = sum(int(suite.attrib.get("errors", "0")) for suite in suites)
            skipped = sum(int(suite.attrib.get("skipped", "0")) for suite in suites)
        else:
            tests = int(attributes.get("tests", "0"))
            failures = int(attributes.get("failures", "0"))
            errors = int(attributes.get("errors", "0"))
            skipped = int(attributes.get("skipped", "0"))
    except (OSError, ET.ParseError, ValueError) as error:
        return _failed_summary("pytest", outcome, f"cannot read pytest JUnit report: {error}")
    skipped_nodes = root.findall(".//testcase/skipped")
    xfailed = sum(
        "xfail" in skipped_node.attrib.get("type", "").lower()
        or "expected failure" in skipped_node.attrib.get("message", "").lower()
        for skipped_node in skipped_nodes
    )
    regular_skips = max(0, skipped - xfailed)
    mapped_outcomes, mapping_error = _map_pytest_outcomes(root, mappings)
    if mapping_error is not None:
        return _failed_summary("pytest", outcome, mapping_error)
    return ValidationSummary(
        runner="pytest",
        tests=tests,
        passed=max(0, tests - failures - errors - skipped),
        failed=failures,
        errors=errors,
        skipped=regular_skips,
        xfailed=xfailed,
        xpassed=0,
        exit_code=outcome.returncode,
        duration=outcome.duration,
        detail=_process_detail(outcome) if outcome.returncode != 0 else None,
        outcomes=mapped_outcomes,
    )


def _unittest_summary(
    path: Path,
    outcome: _ProcessOutcome,
    mappings: Sequence[TestMapping],
) -> ValidationSummary:
    if outcome.timed_out:
        return _failed_summary("unittest", outcome, "validation process timed out")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        tests = int(document["tests"])
        failures = int(document.get("failures", 0))
        errors = int(document.get("errors", 0))
        skipped = int(document.get("skipped", 0))
        expected = int(document.get("expected_failures", 0))
        unexpected = int(document.get("unexpected_successes", 0))
        raw_outcomes = document["outcomes"]
        if not isinstance(raw_outcomes, list):
            raise TypeError("outcomes is not a list")
    except (OSError, ValueError, KeyError, TypeError) as error:
        return _failed_summary("unittest", outcome, f"cannot read unittest report: {error}")
    mapped_outcomes, mapping_error = _map_unittest_outcomes(raw_outcomes, mappings)
    if mapping_error is not None:
        return _failed_summary("unittest", outcome, mapping_error)
    return ValidationSummary(
        runner="unittest",
        tests=tests,
        passed=max(0, tests - failures - errors - skipped - expected - unexpected),
        failed=failures,
        errors=errors,
        skipped=skipped,
        xfailed=expected,
        xpassed=unexpected,
        exit_code=outcome.returncode,
        duration=outcome.duration,
        detail=_process_detail(outcome) if outcome.returncode != 0 else None,
        outcomes=mapped_outcomes,
    )


def _native_summary(
    path: Path,
    outcome: _ProcessOutcome,
    *,
    runner: str,
    mappings: Sequence[TestMapping],
) -> ValidationSummary:
    if outcome.timed_out:
        return _failed_summary(runner, outcome, "validation process timed out")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        raw_tests = document["tests"]
        if not isinstance(raw_tests, list):
            raise TypeError("tests is not a list")
        statuses = Counter(str(item["status"]) for item in raw_tests)
        collection_errors = len(document.get("collection_issues", []))
    except (OSError, ValueError, KeyError, TypeError) as error:
        return _failed_summary(runner, outcome, f"cannot read native JSON report: {error}")
    mapped_outcomes, mapping_error = _map_native_outcomes(raw_tests, mappings)
    if mapping_error is not None:
        return _failed_summary(runner, outcome, mapping_error)
    failures = statuses["fail"] + statuses["flaky"]
    errors = collection_errors + sum(
        statuses[name]
        for name in (
            "error_setup",
            "error_teardown",
            "timeout",
            "crash",
            "infra_error",
            "cancelled",
            "not_run",
        )
    )
    return ValidationSummary(
        runner=runner,
        tests=len(raw_tests) + collection_errors,
        passed=statuses["pass"] + statuses["cached_pass"],
        failed=failures,
        errors=errors,
        skipped=statuses["skip"],
        xfailed=statuses["xfail"],
        xpassed=statuses["xpass"],
        exit_code=outcome.returncode,
        duration=outcome.duration,
        detail=_process_detail(outcome) if outcome.returncode != 0 else None,
        outcomes=mapped_outcomes,
    )


def _failed_summary(
    runner: str,
    outcome: _ProcessOutcome,
    detail: str,
) -> ValidationSummary:
    process_detail = _process_detail(outcome)
    return ValidationSummary(
        runner=runner,
        tests=0,
        passed=0,
        failed=0,
        errors=1,
        skipped=0,
        xfailed=0,
        xpassed=0,
        exit_code=outcome.returncode,
        duration=outcome.duration,
        timed_out=outcome.timed_out,
        detail=f"{detail}; {process_detail}" if process_detail else detail,
    )


def _process_detail(outcome: _ProcessOutcome) -> str | None:
    text = (outcome.stderr.strip() or outcome.stdout.strip()).replace("\x00", "")
    if not text:
        return None
    limit = 2_000
    return text if len(text) <= limit else f"…{text[-limit:]}"


__all__ = [
    "MIGRATION_FORMAT",
    "MIGRATION_SCHEMA_VERSION",
    "MigrationOptions",
    "MigrationReport",
    "MigrationStatus",
    "ValidationSummary",
    "migrate",
    "render_migration_summary",
    "write_migration_report",
]
