"""Testenix public authoring and execution API."""

from importlib.metadata import PackageNotFoundError, version

from testenix.api import CaseDefinition, case, cases, fixture, skip, test, xfail
from testenix.config import TestenixConfig
from testenix.contracts import Event, RunResult, Scope, Status, TestResult, TestSpec
from testenix.discovery import CollectionResult, discover
from testenix.events import EventSink
from testenix.migration_service import (
    MigrationOptions,
    MigrationReport,
    MigrationStatus,
    ValidationSummary,
    migrate,
)
from testenix.runner import collect_trusted_manifest, run, run_async
from testenix.sharding import (
    CollectionManifestError,
    ShardingPolicy,
    TrustedCollectionManifest,
    deserialize_trusted_collection_manifest,
    serialize_trusted_collection_manifest,
)

__all__ = [
    "CaseDefinition",
    "CollectionResult",
    "CollectionManifestError",
    "Event",
    "EventSink",
    "MigrationOptions",
    "MigrationReport",
    "MigrationStatus",
    "TestenixConfig",
    "RunResult",
    "ShardingPolicy",
    "Scope",
    "Status",
    "TestResult",
    "TestSpec",
    "TrustedCollectionManifest",
    "ValidationSummary",
    "case",
    "cases",
    "collect_trusted_manifest",
    "deserialize_trusted_collection_manifest",
    "discover",
    "fixture",
    "migrate",
    "run",
    "run_async",
    "serialize_trusted_collection_manifest",
    "skip",
    "test",
    "xfail",
]
try:
    __version__ = version("testenix")
except PackageNotFoundError:  # Source checkout without installed metadata.
    __version__ = "0.2.1"
