"""Runtime helpers used by generated migration wrappers.

Pytest coroutine wrappers preserve the default function-scoped event-loop
lifecycle of ``pytest-asyncio``.  Unittest wrappers intentionally execute the
original ``unittest.TestCase`` instead of approximating its assertion and
per-test lifecycle semantics.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import re
import threading
import unittest
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from types import MappingProxyType, ModuleType, TracebackType
from typing import Any, ParamSpec, TypeAlias, TypeVar

from testenix.discovery import load_module

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_MODULE_CACHE: dict[tuple[str, str], ModuleType] = {}
_MODULE_CACHE_LOCK = threading.RLock()

ExceptionInfo: TypeAlias = tuple[type[BaseException], BaseException, TracebackType]
RawExceptionInfo: TypeAlias = ExceptionInfo | tuple[None, None, None]
_P = ParamSpec("_P")
_R = TypeVar("_R")


def isolated_pytest_asyncio(
    function: Callable[_P, Coroutine[Any, Any, _R]],
    /,
) -> Callable[_P, None]:
    """Run one migrated pytest coroutine in a fresh, function-scoped loop.

    The returned callable is deliberately synchronous.  The native executor
    therefore invokes it outside its orchestration loop, allowing ``Runner``
    to reproduce pytest-asyncio's default one-loop-per-test lifecycle without
    changing the behavior of native Testenix coroutine tests.
    """

    @wraps(function)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError(
                "a migrated pytest asyncio test cannot run inside an active event loop"
            )

        context = contextvars.copy_context()
        with asyncio.Runner(debug=False) as runner:
            runner.run(function(*args, **kwargs), context=context)

    return wrapper


@dataclass(frozen=True, slots=True)
class _ManifestCacheEntry:
    project_root: Path
    source_hashes: Mapping[Path, str]


_MANIFEST_CACHE: dict[tuple[str, str], _ManifestCacheEntry] = {}


class UnittestMigrationRuntimeError(RuntimeError):
    """Base error raised by a generated unittest migration wrapper."""


class UnittestSourceChangedError(UnittestMigrationRuntimeError):
    """The original test module no longer matches its migration manifest."""


class UnittestDynamicSkipError(AssertionError):
    """A runtime-only unittest skip escaped static migration analysis."""


class UnittestResultProtocolError(UnittestMigrationRuntimeError):
    """A TestCase did not publish exactly one recognizable terminal outcome."""


class _CapturingResult(unittest.TestResult):
    """A normal TestResult which also retains original exception objects."""

    def __init__(self) -> None:
        super().__init__()
        self.captured_failures: list[ExceptionInfo] = []
        self.captured_errors: list[ExceptionInfo] = []
        self.captured_expected_failures: list[ExceptionInfo] = []
        self.captured_skips: list[tuple[unittest.TestCase, str]] = []
        self.captured_unexpected_successes: list[unittest.TestCase] = []
        self.captured_successes: list[unittest.TestCase] = []

    def addFailure(  # noqa: N802 - unittest callback name
        self,
        test: unittest.TestCase,
        err: RawExceptionInfo,
    ) -> None:
        self.captured_failures.append(_require_exception_info(err))
        super().addFailure(test, err)

    def addError(  # noqa: N802 - unittest callback name
        self,
        test: unittest.TestCase,
        err: RawExceptionInfo,
    ) -> None:
        self.captured_errors.append(_require_exception_info(err))
        super().addError(test, err)

    def addExpectedFailure(  # noqa: N802 - unittest callback name
        self,
        test: unittest.TestCase,
        err: RawExceptionInfo,
    ) -> None:
        self.captured_expected_failures.append(_require_exception_info(err))
        super().addExpectedFailure(test, err)

    def addSkip(  # noqa: N802 - unittest callback name
        self,
        test: unittest.TestCase,
        reason: str,
    ) -> None:
        self.captured_skips.append((test, reason))
        super().addSkip(test, reason)

    def addUnexpectedSuccess(  # noqa: N802 - unittest callback name
        self,
        test: unittest.TestCase,
    ) -> None:
        self.captured_unexpected_successes.append(test)
        super().addUnexpectedSuccess(test)

    def addSuccess(  # noqa: N802 - unittest callback name
        self,
        test: unittest.TestCase,
    ) -> None:
        self.captured_successes.append(test)
        super().addSuccess(test)


def _require_exception_info(err: RawExceptionInfo) -> ExceptionInfo:
    if err[0] is None or err[1] is None or err[2] is None:
        raise UnittestResultProtocolError("unittest reported an empty exception tuple")
    return err


def _source_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_source(source_path: str | Path, expected_sha256: str) -> tuple[Path, str]:
    normalized_digest = expected_sha256.lower()
    if _SHA256_PATTERN.fullmatch(normalized_digest) is None:
        raise ValueError("expected_sha256 must be a 64-character hexadecimal SHA-256 digest")

    path = Path(source_path).expanduser().resolve()
    if not path.is_file():
        raise UnittestSourceChangedError(f"unittest source file does not exist: {path}")
    actual_digest = _source_digest(path)
    if actual_digest != normalized_digest:
        raise UnittestSourceChangedError(
            f"unittest source changed since migration: {path}; rerun 'testenix migrate'"
        )
    return path, normalized_digest


def _verified_manifest(
    manifest_path: str | Path,
    expected_sha256: str,
) -> tuple[Path, str, bytes]:
    """Verify a text manifest while treating JSON line endings as insignificant.

    Generated artifacts may pass through a Windows text writer or a Git checkout
    which converts LF to CRLF.  That does not change the JSON document, so only
    the manifest digest normalizes line endings.  Original Python sources remain
    byte-for-byte pinned by :func:`_verified_source`.  The canonical bytes are
    returned so the caller parses the verified snapshot without reopening the
    path.
    """

    normalized_digest = expected_sha256.lower()
    if _SHA256_PATTERN.fullmatch(normalized_digest) is None:
        raise ValueError("manifest_sha256 must be a 64-character hexadecimal SHA-256 digest")

    path = Path(manifest_path).expanduser().resolve()
    if not path.is_file():
        raise UnittestSourceChangedError(f"unittest source manifest does not exist: {path}")
    content = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    actual_digest = hashlib.sha256(content).hexdigest()
    if actual_digest != normalized_digest:
        raise UnittestSourceChangedError(
            f"unittest source manifest changed since migration: {path}; rerun 'testenix migrate'"
        )
    return path, normalized_digest, content


def resolve_unittest_source(
    wrapper_path: str | Path,
    source_relative_to_wrapper: str | Path,
    expected_sha256: str,
    *,
    project_relative_source: str | Path,
    manifest_relative_to_wrapper: str | Path,
    manifest_sha256: str,
) -> Path:
    """Locate an original and verify the complete migration source manifest."""

    wrapper = Path(wrapper_path).expanduser().resolve(strict=True)
    relative = Path(source_relative_to_wrapper)
    raw_relative = str(source_relative_to_wrapper)
    if (
        not raw_relative
        or "\x00" in raw_relative
        or relative.is_absolute()
        or relative.anchor
        or any(part in {"", "."} for part in relative.parts)
    ):
        raise ValueError("source_relative_to_wrapper must be a non-empty relative path")
    candidate = wrapper.parent.joinpath(*relative.parts)
    try:
        verified, normalized_source_digest = _verified_source(candidate, expected_sha256)
    except (OSError, RuntimeError) as error:
        raise UnittestSourceChangedError(
            "cannot locate the pinned unittest source through the generated wrapper; "
            "rerun 'testenix migrate'"
        ) from error
    project_relative = _safe_manifest_relative_path(
        project_relative_source,
        description="project_relative_source",
    )
    project_root = verified
    for _ in project_relative.parts:
        project_root = project_root.parent
    try:
        if project_root.joinpath(*project_relative.parts).resolve(strict=True) != verified:
            raise UnittestSourceChangedError(
                "the generated unittest source relation no longer reaches its manifest path"
            )
    except (OSError, RuntimeError) as error:
        raise UnittestSourceChangedError(
            "cannot validate the generated unittest project root; rerun 'testenix migrate'"
        ) from error

    manifest_relative = Path(manifest_relative_to_wrapper)
    raw_manifest_relative = str(manifest_relative_to_wrapper)
    if (
        not raw_manifest_relative
        or "\x00" in raw_manifest_relative
        or manifest_relative.is_absolute()
        or manifest_relative.anchor
        or any(part in {"", "."} for part in manifest_relative.parts)
    ):
        raise ValueError("manifest_relative_to_wrapper must be a non-empty relative path")
    manifest_path, normalized_manifest_digest, manifest_content = _verified_manifest(
        wrapper.parent.joinpath(*manifest_relative.parts),
        manifest_sha256,
    )
    cache_key = (str(manifest_path), normalized_manifest_digest)
    with _MODULE_CACHE_LOCK:
        cached_manifest = _MANIFEST_CACHE.get(cache_key)
        cache_hit = cached_manifest is not None
        if cached_manifest is None:
            source_hashes = _verify_source_manifest(
                manifest_path,
                manifest_content,
                project_root,
            )
            cached_manifest = _ManifestCacheEntry(project_root, source_hashes)
            _MANIFEST_CACHE[cache_key] = cached_manifest
        elif cached_manifest.project_root != project_root:
            raise UnittestSourceChangedError("unittest source manifest resolved ambiguously")
    if cache_hit:
        # Filesystem integrity cannot be cached: helpers may change while this
        # process remains alive.  This scan happens once per generated source
        # module import, not once per converted unittest method.
        _verify_manifest_source_hashes(
            cached_manifest.source_hashes,
            cached_manifest.project_root,
        )
    _verify_manifest_membership(
        cached_manifest.source_hashes,
        project_relative,
        normalized_source_digest,
    )
    return verified


def _safe_manifest_relative_path(value: str | Path, *, description: str) -> Path:
    raw_value = str(value)
    relative = Path(value)
    if (
        not raw_value
        or "\x00" in raw_value
        or relative.is_absolute()
        or relative.anchor
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise UnittestSourceChangedError(f"{description} is not a safe project-relative path")
    return relative


def _verify_source_manifest(
    manifest_path: Path,
    manifest_content: bytes,
    project_root: Path,
) -> Mapping[Path, str]:
    try:
        document = json.loads(manifest_content)
        if (
            document.get("format") != "testenix.unittest-source-manifest"
            or document.get("schema_version") != 1
        ):
            raise ValueError("unsupported source manifest format")
        raw_hashes = document["source_hashes"]
        if not isinstance(raw_hashes, dict) or not raw_hashes:
            raise ValueError("source_hashes must be a non-empty object")
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise UnittestSourceChangedError(
            f"cannot read unittest source manifest {manifest_path}: {error}"
        ) from error

    source_hashes: dict[Path, str] = {}
    for raw_path, raw_digest in raw_hashes.items():
        if not isinstance(raw_path, str) or not isinstance(raw_digest, str):
            raise UnittestSourceChangedError("unittest source manifest entries must be strings")
        relative = _safe_manifest_relative_path(raw_path, description="manifest source path")
        digest = raw_digest.lower()
        if _SHA256_PATTERN.fullmatch(digest) is None:
            raise UnittestSourceChangedError(
                f"unittest source manifest has an invalid digest for {raw_path}"
            )
        source_hashes[relative] = digest

    immutable_hashes = MappingProxyType(source_hashes)
    _verify_manifest_source_hashes(immutable_hashes, project_root)
    return immutable_hashes


def _verify_manifest_source_hashes(
    source_hashes: Mapping[Path, str],
    project_root: Path,
) -> None:
    for relative, digest in source_hashes.items():
        candidate, _ = _verified_source(project_root.joinpath(*relative.parts), digest)
        try:
            candidate.relative_to(project_root)
        except ValueError as error:
            raise UnittestSourceChangedError(
                f"unittest source path escapes the project root: {relative.as_posix()}"
            ) from error


def _verify_manifest_membership(
    source_hashes: Mapping[Path, str],
    requested_source: Path,
    expected_source_sha256: str,
) -> None:
    if source_hashes.get(requested_source) != expected_source_sha256:
        raise UnittestSourceChangedError(
            f"unittest source manifest does not pin {requested_source.as_posix()}"
        )


def load_unittest_case(
    source_path: str | Path,
    class_name: str,
    expected_sha256: str,
) -> type[unittest.TestCase]:
    """Load one original TestCase after verifying its pinned source digest.

    The file is hashed before every cache lookup.  A newly imported module is
    hashed again after execution so a concurrent edit cannot populate the
    cache under a stale manifest key.
    """

    with _MODULE_CACHE_LOCK:
        path, digest = _verified_source(source_path, expected_sha256)
        cache_key = (str(path), digest)
        module = _MODULE_CACHE.get(cache_key)
        if module is None:
            module = load_module(path)
            _verified_source(path, digest)
            _MODULE_CACHE[cache_key] = module

        try:
            candidate = getattr(module, class_name)
        except AttributeError as error:
            raise UnittestSourceChangedError(
                f"unittest class {class_name!r} is no longer defined in {path}; "
                "rerun 'testenix migrate'"
            ) from error
        if not isinstance(candidate, type) or not issubclass(candidate, unittest.TestCase):
            raise UnittestSourceChangedError(
                f"{class_name!r} in {path} is no longer a unittest.TestCase; "
                "rerun 'testenix migrate'"
            )
        return candidate


def unittest_case_is_skipped(
    test_case: type[unittest.TestCase],
    method_name: str,
) -> bool:
    """Return the static skip state interpreted by ``TestCase.run``."""

    method = getattr(test_case, method_name)
    return bool(
        getattr(test_case, "__unittest_skip__", False)
        or getattr(method, "__unittest_skip__", False)
    )


def unittest_case_skip_reason(
    test_case: type[unittest.TestCase],
    method_name: str,
) -> str:
    """Return a non-empty static skip reason for the Testenix decorator."""

    method = getattr(test_case, method_name)
    reason = getattr(test_case, "__unittest_skip_why__", "") or getattr(
        method,
        "__unittest_skip_why__",
        "",
    )
    return str(reason).strip() or "skipped by unittest"


def unittest_case_expects_failure(
    test_case: type[unittest.TestCase],
    method_name: str,
) -> bool:
    """Return whether unittest marks the class or method as expected failure."""

    method = getattr(test_case, method_name)
    return bool(
        getattr(test_case, "__unittest_expecting_failure__", False)
        or getattr(method, "__unittest_expecting_failure__", False)
    )


def _raise_exceptions(label: str, captured: list[ExceptionInfo]) -> None:
    materialized = [error.with_traceback(traceback) for _, error, traceback in captured]
    if len(materialized) == 1:
        raise materialized[0]
    if materialized:
        raise BaseExceptionGroup(label, materialized)


def run_unittest_case(
    test_case: type[unittest.TestCase],
    method_name: str,
) -> None:
    """Execute one original method and expose its outcome to native Testenix.

    Static skips never reach this function because the generated wrapper has a
    native ``@skip`` marker.  Expected failures are re-raised while the wrapper
    carries ``@xfail``; an unexpected success returns normally and therefore
    becomes Testenix ``XPASS``.
    """

    try:
        instance = test_case(methodName=method_name)
    except (TypeError, ValueError) as error:
        raise UnittestResultProtocolError(
            f"cannot instantiate {test_case.__qualname__}.{method_name}: {error}"
        ) from error

    result = _CapturingResult()
    instance.run(result)
    if result.testsRun != 1:
        raise UnittestResultProtocolError(
            f"{test_case.__qualname__}.{method_name} reported {result.testsRun} tests; expected 1"
        )

    # Errors are checked before assertion failures to retain unittest's most
    # severe information if teardown or cleanup produced an additional error.
    combined_failures = [*result.captured_errors, *result.captured_failures]
    _raise_exceptions(
        f"unittest errors in {test_case.__qualname__}.{method_name}",
        combined_failures,
    )

    if result.captured_skips:
        reasons = "; ".join(dict.fromkeys(reason for _, reason in result.captured_skips))
        raise UnittestDynamicSkipError(
            f"dynamic unittest skip is not safely migratable: {reasons or 'no reason supplied'}"
        )

    if result.captured_expected_failures:
        _raise_exceptions(
            f"expected unittest failure in {test_case.__qualname__}.{method_name}",
            result.captured_expected_failures,
        )

    if result.captured_unexpected_successes:
        return
    if len(result.captured_successes) != 1:
        raise UnittestResultProtocolError(
            f"{test_case.__qualname__}.{method_name} produced no recognized terminal outcome"
        )


__all__ = [
    "UnittestDynamicSkipError",
    "UnittestMigrationRuntimeError",
    "UnittestResultProtocolError",
    "UnittestSourceChangedError",
    "isolated_pytest_asyncio",
    "load_unittest_case",
    "resolve_unittest_source",
    "run_unittest_case",
    "unittest_case_expects_failure",
    "unittest_case_is_skipped",
    "unittest_case_skip_reason",
]
