"""Serializable contracts shared by migration analyzers and orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class DiagnosticSeverity(StrEnum):
    """Whether a migration diagnostic blocks publication."""

    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class SourceFile:
    """One immutable source file selected for analysis."""

    path: Path
    project_relative: Path
    migration_relative: Path
    sha256: str
    text: str


@dataclass(frozen=True, slots=True)
class MigrationDiagnostic:
    """Stable, user-facing explanation of a conversion decision."""

    code: str
    message: str
    source: str
    line: int | None = None
    severity: DiagnosticSeverity = DiagnosticSeverity.ERROR


@dataclass(frozen=True, slots=True)
class TestMapping:
    """Static mapping from one foreign test case to a generated native test."""

    source_id: str
    target_file: str
    target_function: str
    case_id: str | None = None


@dataclass(frozen=True, slots=True)
class GeneratedArtifact:
    """A complete generated file ready to be written into staging."""

    relative_path: Path
    content: str
    source_files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ConversionBundle:
    """Deterministic output of one framework converter."""

    artifacts: tuple[GeneratedArtifact, ...] = ()
    mappings: tuple[TestMapping, ...] = ()
    diagnostics: tuple[MigrationDiagnostic, ...] = ()

    @property
    def blocking_diagnostics(self) -> tuple[MigrationDiagnostic, ...]:
        return tuple(
            diagnostic
            for diagnostic in self.diagnostics
            if diagnostic.severity is DiagnosticSeverity.ERROR
        )


__all__ = [
    "ConversionBundle",
    "DiagnosticSeverity",
    "GeneratedArtifact",
    "MigrationDiagnostic",
    "SourceFile",
    "TestMapping",
]
