"""Testenix public authoring and execution API."""

from importlib.metadata import PackageNotFoundError, version

from testenix.api import CaseDefinition, case, cases, fixture, skip, test, xfail
from testenix.config import TestenixConfig
from testenix.contracts import Event, RunResult, Scope, Status, TestResult, TestSpec
from testenix.discovery import CollectionResult, discover
from testenix.events import EventSink
from testenix.runner import run, run_async

__all__ = [
    "CaseDefinition",
    "CollectionResult",
    "Event",
    "EventSink",
    "TestenixConfig",
    "RunResult",
    "Scope",
    "Status",
    "TestResult",
    "TestSpec",
    "case",
    "cases",
    "discover",
    "fixture",
    "run",
    "run_async",
    "skip",
    "test",
    "xfail",
]
try:
    __version__ = version("testenix")
except PackageNotFoundError:  # Source checkout without installed metadata.
    __version__ = "0.1.0"
