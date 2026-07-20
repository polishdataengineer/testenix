"""Filesystem safety primitives for transactional test-suite migration.

The migration service treats source tests as immutable input.  This module
validates that input, snapshots it without following symbolic links, prepares
disposable project copies, writes generated artifacts below a controlled
staging root, and publishes a complete directory with create-only semantics.

Publication intentionally supports only operating systems on which Testenix
can request an atomic *no-replace* rename.  Falling back to ``os.replace`` (or
to a check followed by a normal POSIX rename) would introduce a race capable
of overwriting an existing directory.
"""

from __future__ import annotations

import ctypes
import errno
import fnmatch
import hashlib
import io
import os
import re
import shutil
import stat
import sys
import tempfile
import tokenize
import unicodedata
import uuid
from collections.abc import Iterable, Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from testenix.migration_models import GeneratedArtifact, SourceFile

_PROTECTED_OUTPUT_PARTS = frozenset({".git", ".testenix"})
_SNAPSHOT_EXCLUDED_NAMES = frozenset(
    {
        ".cache",
        ".hypothesis",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "env",
        "node_modules",
        "output",
        "venv",
    }
)
_SHADOW_EXCLUDED_NAMES = _SNAPSHOT_EXCLUDED_NAMES | frozenset({".git", ".testenix"})
_TRANSACTION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_WINDOWS_REPARSE_POINT = 0x0400
_RENAME_NOREPLACE = 1
_RENAME_EXCL = 0x00000004
_SECURE_DIR_FD_AVAILABLE = (
    os.name == "posix"
    and os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
)


class MigrationFilesystemError(RuntimeError):
    """Base class for controlled migration filesystem failures."""


class UnsafeMigrationPathError(MigrationFilesystemError):
    """Raised when a path could escape or mutate protected project content."""


class SourcePathError(MigrationFilesystemError):
    """Raised when migration source input cannot be read safely."""


class SourceChangedError(MigrationFilesystemError):
    """Raised when source files changed after their immutable snapshot."""


class OutputExistsError(MigrationFilesystemError):
    """Raised when create-only publication encounters an existing target."""


class AtomicPublishError(MigrationFilesystemError):
    """Raised when a complete staging directory cannot be published atomically."""


class PublishedOutputDurabilityError(AtomicPublishError):
    """Raised after publication when directory durability could not be confirmed.

    The destination already exists when this exception is raised.  Callers must
    report a successful publication with a durability warning instead of
    claiming that the transaction rolled back.
    """


@dataclass(frozen=True, slots=True)
class MigrationPaths:
    """Canonical paths approved for one migration transaction."""

    project_root: Path
    sources: tuple[Path, ...]
    output: Path


@dataclass(frozen=True, slots=True)
class PublishStaging(os.PathLike[str]):
    """Capability for one staging payload and its captured filesystem identity."""

    path: Path
    project_root: Path
    transaction_id: str
    project_identity: tuple[int, int]
    transaction_identity: tuple[int, int]
    payload_identity: tuple[int, int]

    def __fspath__(self) -> str:
        return os.fspath(self.path)

    def __str__(self) -> str:
        return str(self.path)

    def __truediv__(self, value: str | Path) -> Path:
        return self.path / value

    @property
    def parent(self) -> Path:
        return self.path.parent

    def exists(self) -> bool:
        return self.path.exists()

    def iterdir(self) -> Iterator[Path]:
        return self.path.iterdir()


@dataclass(slots=True)
class _StagingDescriptors:
    project_fd: int
    migrations_fd: int
    transaction_fd: int
    payload_fd: int | None

    def close(self) -> None:
        for descriptor in (
            self.payload_fd,
            self.transaction_fd,
            self.migrations_fd,
            self.project_fd,
        ):
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)


def validate_migration_paths(
    project_root: str | Path,
    sources: Sequence[str | Path],
    output: str | Path,
) -> MigrationPaths:
    """Validate immutable sources and a new output location.

    Relative source and output paths are interpreted from ``project_root``.
    Sources must exist below the project, may not overlap one another, and may
    not contain symbolic links or Windows reparse points.  The output must have
    an existing real parent, must not exist in any form (including a broken
    symlink), and must neither overlap input nor target protected project paths.
    """

    raw_root = Path(project_root).expanduser()
    if _is_link_like(raw_root):
        raise UnsafeMigrationPathError(f"project root may not be a symlink: {raw_root}")
    try:
        root = raw_root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise UnsafeMigrationPathError(
            f"cannot resolve project root {raw_root}: {error}"
        ) from error
    if not root.is_dir():
        raise UnsafeMigrationPathError(f"project root is not a directory: {root}")
    if not sources:
        raise SourcePathError("at least one migration source is required")

    canonical_sources: list[Path] = []
    for raw_source in sources:
        source_path = _from_project(root, raw_source, purpose="source")
        if not os.path.lexists(source_path):
            raise SourcePathError(f"migration source does not exist: {source_path}")
        _assert_no_link_components(root, source_path, purpose="source")
        try:
            source = source_path.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise SourcePathError(
                f"cannot resolve migration source {source_path}: {error}"
            ) from error
        _assert_within(root, source, purpose="source")
        if not source.is_file() and not source.is_dir():
            raise SourcePathError(f"migration source is not a regular file or directory: {source}")
        _assert_source_tree_has_no_links(source)
        canonical_sources.append(source)

    ordered_sources = tuple(sorted(set(canonical_sources), key=lambda path: path.as_posix()))
    if len(ordered_sources) != len(canonical_sources):
        raise SourcePathError("migration sources contain the same path more than once")
    for index, source in enumerate(ordered_sources):
        for other in ordered_sources[index + 1 :]:
            if _paths_overlap(source, other):
                raise SourcePathError(f"migration sources may not overlap: {source} and {other}")

    output_path = _new_output_from_project(root, output)
    _assert_within(root, output_path, purpose="output")
    output_relative = output_path.relative_to(root)
    if not output_relative.parts:
        raise UnsafeMigrationPathError("the project root cannot be a migration output")
    if _PROTECTED_OUTPUT_PARTS.intersection(output_relative.parts):
        raise UnsafeMigrationPathError(
            f"migration output may not target .git or .testenix: {output_path}"
        )
    if os.path.lexists(output_path):
        raise OutputExistsError(f"migration output already exists: {output_path}")
    if not output_path.parent.exists() or not output_path.parent.is_dir():
        raise UnsafeMigrationPathError(
            f"migration output parent must be an existing directory: {output_path.parent}"
        )
    _assert_no_link_components(root, output_path.parent, purpose="output parent")
    for source in ordered_sources:
        if _paths_overlap(source, output_path):
            raise UnsafeMigrationPathError(
                f"migration output may not overlap source {source}: {output_path}"
            )

    return MigrationPaths(root, ordered_sources, output_path)


def validate_migration_report_path(
    paths: MigrationPaths,
    report_path: str | Path,
) -> Path:
    """Validate a new audit-report path disjoint from sources and output.

    A report is written after validation (and, in publish mode, after the
    destination rename), so allowing it below either test tree would mutate a
    suite whose contents were just proved.  Relative paths are interpreted
    from the validated project root and may not leave it or traverse links.
    """

    report = _from_project(paths.project_root, report_path, purpose="report")
    relative = report.relative_to(paths.project_root)
    if not relative.parts:
        raise UnsafeMigrationPathError("the project root cannot be a migration report")
    if _PROTECTED_OUTPUT_PARTS.intersection(relative.parts):
        raise UnsafeMigrationPathError(
            f"migration report may not target .git or .testenix: {report}"
        )
    _assert_no_link_components(paths.project_root, report.parent, purpose="report parent")
    if os.path.lexists(report):
        raise OutputExistsError(f"migration report already exists: {report}")
    for source in paths.sources:
        if _paths_overlap(source, report):
            raise UnsafeMigrationPathError(
                f"migration report may not overlap source {source}: {report}"
            )
    if _paths_overlap(paths.output, report):
        raise UnsafeMigrationPathError(
            f"migration report may not overlap output {paths.output}: {report}"
        )
    return report


def snapshot_source_files(
    paths: MigrationPaths,
    *,
    include_all_python: bool = False,
) -> tuple[SourceFile, ...]:
    """Read a deterministic, immutable snapshot of selected Python sources.

    By default, directories include ``test*.py``, ``*_test.py``, and
    ``conftest.py``.  Set ``include_all_python`` to include every visible Python
    module.  Hidden paths, virtual environments, build outputs, and caches are
    ignored.  Explicit and discovered symbolic links are always rejected.
    """

    selected: dict[Path, Path] = {}
    for source in paths.sources:
        for file_path in _iter_source_files(source, include_all_python=include_all_python):
            selected[file_path] = source
    if not selected:
        selection = "Python" if include_all_python else "test"
        raise SourcePathError(f"migration sources contain no selected {selection} files")

    multiple_sources = len(paths.sources) > 1
    result: list[SourceFile] = []
    for file_path in sorted(selected, key=lambda path: path.as_posix()):
        data = _read_regular_file(file_path)
        project_relative = file_path.relative_to(paths.project_root)
        source = selected[file_path]
        if multiple_sources:
            migration_relative = project_relative
        elif source.is_dir():
            migration_relative = file_path.relative_to(source)
        else:
            migration_relative = Path(file_path.name)
        result.append(
            SourceFile(
                path=file_path,
                project_relative=project_relative,
                migration_relative=migration_relative,
                sha256=hashlib.sha256(data).hexdigest(),
                text=_decode_python_source(file_path, data),
            )
        )
    return tuple(result)


def source_snapshot_digest(files: Sequence[SourceFile]) -> str:
    """Return a stable digest for file names and hashes in a source snapshot."""

    digest = hashlib.sha256()
    for source in sorted(files, key=lambda item: item.project_relative.as_posix()):
        digest.update(source.project_relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(source.sha256.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def verify_source_snapshot(
    paths: MigrationPaths,
    expected: Sequence[SourceFile],
    *,
    include_all_python: bool = False,
) -> None:
    """Rehash selected sources and reject additions, removals, or modifications."""

    try:
        current = snapshot_source_files(paths, include_all_python=include_all_python)
    except SourcePathError as error:
        raise SourceChangedError(f"cannot verify migration sources: {error}") from error
    expected_by_path = {item.project_relative: item.sha256 for item in expected}
    current_by_path = {item.project_relative: item.sha256 for item in current}
    if current_by_path == expected_by_path:
        return

    missing = sorted(set(expected_by_path) - set(current_by_path), key=lambda path: path.as_posix())
    added = sorted(set(current_by_path) - set(expected_by_path), key=lambda path: path.as_posix())
    changed = sorted(
        (
            path
            for path in set(expected_by_path).intersection(current_by_path)
            if expected_by_path[path] != current_by_path[path]
        ),
        key=lambda path: path.as_posix(),
    )
    details: list[str] = []
    if missing:
        details.append("removed=" + ",".join(path.as_posix() for path in missing))
    if added:
        details.append("added=" + ",".join(path.as_posix() for path in added))
    if changed:
        details.append("changed=" + ",".join(path.as_posix() for path in changed))
    raise SourceChangedError("migration sources changed after snapshot: " + "; ".join(details))


def copy_project_to_shadow(
    project_root: str | Path,
    *,
    temp_parent: str | Path | None = None,
) -> Path:
    """Copy a project into a private disposable directory without following links.

    The returned path is the root of the copied project.  The caller owns it and
    should remove it after validation.  Git data, environments, caches,
    Testenix state, and common build/output directories are excluded.
    """

    raw_root = Path(project_root).expanduser()
    if _is_link_like(raw_root):
        raise UnsafeMigrationPathError(f"project root may not be a symlink: {raw_root}")
    try:
        root = raw_root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise UnsafeMigrationPathError(
            f"cannot resolve project root {raw_root}: {error}"
        ) from error
    if not root.is_dir():
        raise UnsafeMigrationPathError(f"project root is not a directory: {root}")

    parent: str | None = None
    if temp_parent is not None:
        raw_parent = Path(temp_parent).expanduser()
        if _is_link_like(raw_parent):
            raise UnsafeMigrationPathError(f"shadow parent may not be a symlink: {raw_parent}")
        try:
            resolved_parent = raw_parent.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise UnsafeMigrationPathError(
                f"cannot resolve shadow parent {raw_parent}: {error}"
            ) from error
        if not resolved_parent.is_dir():
            raise UnsafeMigrationPathError(f"shadow parent is not a directory: {resolved_parent}")
        parent = str(resolved_parent)

    shadow = Path(tempfile.mkdtemp(prefix="testenix-migration-shadow-", dir=parent))
    try:
        _copy_directory_contents(root, shadow)
    except BaseException:
        shutil.rmtree(shadow, ignore_errors=True)
        raise
    return shadow


def create_publish_staging(
    paths: MigrationPaths,
    *,
    transaction_id: str | None = None,
) -> PublishStaging:
    """Create and return an empty same-filesystem payload directory.

    The directory is placed below the ignored ``.testenix/migrations`` state
    root so its later rename to ``paths.output`` stays on one filesystem.
    """

    identifier = transaction_id or uuid.uuid4().hex
    if not _TRANSACTION_ID.fullmatch(identifier):
        raise UnsafeMigrationPathError(f"invalid migration transaction id: {identifier!r}")
    if _secure_dir_fd_supported():
        return _create_publish_staging_at(paths, identifier)

    state_root = paths.project_root / ".testenix"
    migrations_root = state_root / "migrations"
    _ensure_real_directory(state_root, mode=0o700)
    _ensure_real_directory(migrations_root, mode=0o700)
    transaction_root = migrations_root / identifier
    try:
        transaction_root.mkdir(mode=0o700)
    except FileExistsError as error:
        raise UnsafeMigrationPathError(
            f"migration transaction already exists: {identifier}"
        ) from error
    payload = transaction_root / "payload"
    try:
        payload.mkdir(mode=0o700)
    except BaseException:
        transaction_root.rmdir()
        raise
    project_stat = paths.project_root.stat()
    transaction_stat = transaction_root.stat()
    payload_stat = payload.stat()
    return PublishStaging(
        path=payload,
        project_root=paths.project_root,
        transaction_id=identifier,
        project_identity=(project_stat.st_dev, project_stat.st_ino),
        transaction_identity=(transaction_stat.st_dev, transaction_stat.st_ino),
        payload_identity=(payload_stat.st_dev, payload_stat.st_ino),
    )


def _create_publish_staging_at(paths: MigrationPaths, identifier: str) -> PublishStaging:
    """Create staging with mkdirat/openat anchored at the validated project."""

    project_fd = _open_directory(paths.project_root)
    state_fd: int | None = None
    migrations_fd: int | None = None
    transaction_fd: int | None = None
    payload_fd: int | None = None
    transaction_created = False
    payload_created = False
    completed = False
    try:
        project_stat = os.fstat(project_fd)
        state_fd = _open_or_create_directory_at(project_fd, ".testenix", mode=0o700)
        migrations_fd = _open_or_create_directory_at(state_fd, "migrations", mode=0o700)
        try:
            os.mkdir(identifier, 0o700, dir_fd=migrations_fd)
            transaction_created = True
        except FileExistsError as error:
            raise UnsafeMigrationPathError(
                f"migration transaction already exists: {identifier}"
            ) from error
        transaction_fd = _open_directory_at(migrations_fd, identifier)
        transaction_stat = os.fstat(transaction_fd)
        os.mkdir("payload", 0o700, dir_fd=transaction_fd)
        payload_created = True
        payload_fd = _open_directory_at(transaction_fd, "payload")
        payload_stat = os.fstat(payload_fd)
        os.fsync(transaction_fd)
        os.fsync(migrations_fd)
        staging = PublishStaging(
            path=paths.project_root / ".testenix" / "migrations" / identifier / "payload",
            project_root=paths.project_root,
            transaction_id=identifier,
            project_identity=(project_stat.st_dev, project_stat.st_ino),
            transaction_identity=(transaction_stat.st_dev, transaction_stat.st_ino),
            payload_identity=(payload_stat.st_dev, payload_stat.st_ino),
        )
        completed = True
        return staging
    except MigrationFilesystemError:
        raise
    except OSError as error:
        raise UnsafeMigrationPathError(
            f"cannot create migration staging safely: {error}"
        ) from error
    finally:
        if payload_fd is not None:
            os.close(payload_fd)
        if transaction_fd is not None:
            os.close(transaction_fd)
        if migrations_fd is not None:
            if transaction_created and not completed:
                if payload_created:
                    with suppress(OSError):
                        cleanup_transaction_fd = _open_directory_at(migrations_fd, identifier)
                        try:
                            os.rmdir("payload", dir_fd=cleanup_transaction_fd)
                        finally:
                            os.close(cleanup_transaction_fd)
                with suppress(OSError):
                    os.rmdir(identifier, dir_fd=migrations_fd)
            os.close(migrations_fd)
        if state_fd is not None:
            os.close(state_fd)
        os.close(project_fd)


def write_staged_artifacts(
    staging_root: str | Path | PublishStaging,
    artifacts: Iterable[GeneratedArtifact],
    *,
    private_shadow: bool = False,
) -> tuple[Path, ...]:
    """Write generated artifacts beneath an existing, empty staging root.

    Every file is created exclusively.  Relative traversal, absolute paths,
    path aliases on case-insensitive filesystems, existing files, and symlinked
    parents are rejected instead of overwritten.
    """

    root = Path(staging_root)
    materialized = tuple(artifacts)
    destinations: list[tuple[Path, bytes]] = []
    aliases: dict[str, Path] = {}
    for artifact in materialized:
        relative = _validate_artifact_path(artifact.relative_path)
        alias = unicodedata.normalize("NFC", relative.as_posix()).casefold()
        if previous := aliases.get(alias):
            raise UnsafeMigrationPathError(
                f"generated artifact path collision: {previous} and {relative}"
            )
        aliases[alias] = relative
        destination = root.joinpath(*relative.parts)
        _assert_within(root, destination, purpose="generated artifact")
        destinations.append((relative, artifact.content.encode("utf-8")))

    written: list[Path] = []
    if _secure_dir_fd_supported() and not private_shadow:
        staging = _require_staging_capability(staging_root)
        descriptors = _open_staging_descriptors(staging, require_payload=True)
        root_fd = descriptors.payload_fd
        assert root_fd is not None
        try:
            if os.listdir(root_fd):
                raise UnsafeMigrationPathError(f"staging root must be empty: {root}")
            for relative, content in sorted(destinations, key=lambda item: item[0].as_posix()):
                _write_artifact_at(root_fd, relative, content)
                written.append(root.joinpath(*relative.parts))
            os.fsync(root_fd)
        finally:
            descriptors.close()
    else:
        if _is_link_like(root):
            raise UnsafeMigrationPathError(f"staging root may not be a symlink: {root}")
        try:
            root = root.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise UnsafeMigrationPathError(
                f"cannot resolve staging root {root}: {error}"
            ) from error
        if not root.is_dir():
            raise UnsafeMigrationPathError(f"staging root is not a directory: {root}")
        if any(root.iterdir()):
            raise UnsafeMigrationPathError(f"staging root must be empty: {root}")
        for relative, content in sorted(destinations, key=lambda item: item[0].as_posix()):
            destination = root.joinpath(*relative.parts)
            _ensure_artifact_parents(root, destination.parent)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_CLOEXEC", 0)
            try:
                descriptor = os.open(destination, flags, 0o644)
            except FileExistsError as error:
                raise UnsafeMigrationPathError(
                    f"generated artifact already exists: {destination}"
                ) from error
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as target:
                    target.write(content)
                    target.flush()
                    os.fsync(target.fileno())
            except BaseException:
                with suppress(OSError):
                    destination.unlink(missing_ok=True)
                raise
            written.append(destination)
    if not _secure_dir_fd_supported() or private_shadow:
        _fsync_directory(root)
    return tuple(written)


def atomic_publish(
    staging_root: str | Path | PublishStaging,
    paths: MigrationPaths,
) -> Path:
    """Atomically rename a complete staging payload to a new output path.

    This operation never replaces an existing filesystem entry.  Linux and
    macOS use their native exclusive rename variants; Windows' ``os.rename``
    already has create-only destination semantics.  Unsupported platforms fail
    closed rather than using a racy check-and-rename fallback.
    """

    if os.name == "nt":
        staging = _validated_staging_path(staging_root, paths)
        return _windows_atomic_publish(staging, paths)
    if not _secure_dir_fd_supported():
        raise AtomicPublishError(
            "secure anchored publication is unavailable on this platform; "
            "use --check until a no-symlink dir-fd backend is available"
        )

    staging_capability = _require_staging_capability(staging_root)
    descriptors = _open_staging_descriptors(staging_capability, require_payload=True)
    staging = staging_capability.path
    output = paths.output
    _assert_no_link_components(paths.project_root, output.parent, purpose="output parent")
    if os.path.lexists(output):
        raise OutputExistsError(f"migration output already exists: {output}")
    lock_digest = hashlib.sha256(output.name.encode("utf-8")).hexdigest()[:16]
    lock_name = f".{output.name[:32]}.{lock_digest}.testenix-publish.lock"
    lock_path = output.parent / lock_name
    lock_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    lock_flags |= getattr(os, "O_NOFOLLOW", 0)
    lock_flags |= getattr(os, "O_CLOEXEC", 0)
    output_parent_fd: int | None = None
    try:
        output_parent_fd = _open_relative_directory(
            descriptors.project_fd,
            output.parent.relative_to(paths.project_root).parts,
        )
        anchored_staging = os.stat(
            "payload",
            dir_fd=descriptors.transaction_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(anchored_staging.st_mode)
            or (
                anchored_staging.st_dev,
                anchored_staging.st_ino,
            )
            != staging_capability.payload_identity
        ):
            raise UnsafeMigrationPathError("migration staging changed before publication")
        if anchored_staging.st_dev != os.fstat(output_parent_fd).st_dev:
            raise AtomicPublishError("staging and output are not on the same filesystem")
        try:
            os.stat(output.name, dir_fd=output_parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise OutputExistsError(f"migration output already exists: {output}")

        try:
            lock_descriptor = os.open(
                lock_name,
                lock_flags,
                0o600,
                dir_fd=output_parent_fd,
            )
        except FileExistsError as error:
            raise AtomicPublishError(f"another publication owns lock {lock_path}") from error
        lock_stat = os.fstat(lock_descriptor)
        try:
            os.write(lock_descriptor, f"pid={os.getpid()}\n".encode("ascii"))
            os.fsync(lock_descriptor)
        finally:
            os.close(lock_descriptor)

        try:
            _rename_noreplace_at(
                descriptors.transaction_fd,
                "payload",
                output_parent_fd,
                output.name,
                output,
            )
            try:
                os.fsync(output_parent_fd)
            except OSError as error:
                raise PublishedOutputDurabilityError(
                    f"migration output was published at {output}, but directory durability "
                    f"could not be confirmed: {error}"
                ) from error
        finally:
            _unlink_owned_lock_at(output_parent_fd, lock_name, lock_stat)
        return output
    finally:
        if output_parent_fd is not None:
            os.close(output_parent_fd)
        descriptors.close()


def _windows_atomic_publish(staging: Path, paths: MigrationPaths) -> Path:
    """Use Windows' create-only same-volume directory rename semantics."""

    output = paths.output
    _assert_no_link_components(paths.project_root, staging, purpose="staging")
    _assert_no_link_components(paths.project_root, output.parent, purpose="output parent")
    if os.path.lexists(output):
        raise OutputExistsError(f"migration output already exists: {output}")
    if staging.stat().st_dev != output.parent.stat().st_dev:
        raise AtomicPublishError("staging and output are not on the same filesystem")
    try:
        os.rename(staging, output)
    except FileExistsError as error:
        raise OutputExistsError(f"migration output already exists: {output}") from error
    except OSError as error:
        if error.errno in {errno.EEXIST, errno.ENOTEMPTY}:
            raise OutputExistsError(f"migration output already exists: {output}") from error
        raise AtomicPublishError(f"cannot publish migration output: {error}") from error
    return output


def cleanup_publish_staging(
    staging_root: str | Path | PublishStaging,
    paths: MigrationPaths,
) -> None:
    """Remove only the transaction represented by a captured staging capability.

    POSIX deletion walks open directory descriptors and never follows a path
    after validation.  On platforms without that API, an empty transaction can
    be removed safely; a non-empty failed staging tree is deliberately retained
    for manual inspection instead of using a racy recursive path walk.
    """

    staging = _require_staging_capability(staging_root)
    if staging.project_root != paths.project_root:
        raise UnsafeMigrationPathError("staging capability belongs to another project")
    if _secure_dir_fd_supported():
        descriptors = _open_staging_descriptors(staging, require_payload=False)
        try:
            _remove_directory_contents_at(descriptors.transaction_fd)
            current = os.stat(
                staging.transaction_id,
                dir_fd=descriptors.migrations_fd,
                follow_symlinks=False,
            )
            if (current.st_dev, current.st_ino) != staging.transaction_identity:
                raise UnsafeMigrationPathError("migration transaction changed before final cleanup")
            os.rmdir(staging.transaction_id, dir_fd=descriptors.migrations_fd)
            os.fsync(descriptors.migrations_fd)
        except OSError as error:
            raise UnsafeMigrationPathError(
                f"cannot clean migration staging safely: {error}"
            ) from error
        finally:
            descriptors.close()
        return

    transaction_root = staging.path.parent
    if not os.path.lexists(transaction_root):
        return
    transaction_stat = transaction_root.lstat()
    if (
        _is_link_like(transaction_root, transaction_stat)
        or (
            transaction_stat.st_dev,
            transaction_stat.st_ino,
        )
        != staging.transaction_identity
    ):
        raise UnsafeMigrationPathError("migration transaction changed before cleanup")
    if os.path.lexists(staging.path):
        payload_stat = staging.path.lstat()
        if (
            _is_link_like(staging.path, payload_stat)
            or (
                payload_stat.st_dev,
                payload_stat.st_ino,
            )
            != staging.payload_identity
        ):
            raise UnsafeMigrationPathError("migration staging payload changed before cleanup")
        if any(staging.path.iterdir()):
            raise UnsafeMigrationPathError(
                "automatic recursive staging cleanup is unavailable on this platform; "
                f"retained {transaction_root}"
            )
        staging.path.rmdir()
    if any(transaction_root.iterdir()):
        raise UnsafeMigrationPathError(
            f"migration transaction contains unexpected entries: {transaction_root}"
        )
    transaction_root.rmdir()


def _from_project(root: Path, raw_value: str | Path, *, purpose: str) -> Path:
    raw_path = Path(raw_value).expanduser()
    if ".." in raw_path.parts:
        raise UnsafeMigrationPathError(f"{purpose} path may not contain '..': {raw_path}")
    candidate = raw_path if raw_path.is_absolute() else root / raw_path
    # Lexically normalize without following links.  Callers can then inspect
    # every user-supplied component before canonical resolution.
    lexical = Path(os.path.abspath(candidate))
    _assert_within(root, lexical, purpose=purpose)
    return lexical


def _new_output_from_project(root: Path, raw_value: str | Path) -> Path:
    """Resolve an output parent without following a possibly dangling leaf."""

    raw_path = Path(raw_value).expanduser()
    if ".." in raw_path.parts:
        raise UnsafeMigrationPathError(f"output path may not contain '..': {raw_path}")
    candidate = raw_path if raw_path.is_absolute() else root / raw_path
    lexical = Path(os.path.abspath(candidate))
    _assert_within(root, lexical, purpose="output")
    _assert_no_link_components(root, lexical.parent, purpose="output parent")
    try:
        parent = lexical.parent.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise UnsafeMigrationPathError(
            f"cannot resolve output parent {lexical.parent}: {error}"
        ) from error
    output = parent / lexical.name
    _assert_within(root, output, purpose="output")
    return output


def _assert_within(root: Path, candidate: Path, *, purpose: str) -> None:
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise UnsafeMigrationPathError(
            f"{purpose} path escapes project/staging root {root}: {candidate}"
        ) from error


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _is_link_like(path: Path, metadata: os.stat_result | None = None) -> bool:
    try:
        info = metadata if metadata is not None else path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & _WINDOWS_REPARSE_POINT)


def _assert_no_link_components(root: Path, candidate: Path, *, purpose: str) -> None:
    _assert_within(root, candidate, purpose=purpose)
    if _is_link_like(root):
        raise UnsafeMigrationPathError(f"project root may not be a symlink: {root}")
    cursor = root
    for component in candidate.relative_to(root).parts:
        cursor /= component
        if os.path.lexists(cursor) and _is_link_like(cursor):
            raise UnsafeMigrationPathError(f"{purpose} may not traverse a symlink: {cursor}")


def _assert_source_tree_has_no_links(source: Path) -> None:
    if source.is_file():
        if _is_link_like(source):
            raise UnsafeMigrationPathError(f"migration source may not be a symlink: {source}")
        return
    for current, directory_names, file_names in os.walk(source, followlinks=False):
        current_path = Path(current)
        for name in sorted((*directory_names, *file_names)):
            candidate = current_path / name
            try:
                metadata = candidate.lstat()
            except OSError as error:
                raise SourcePathError(
                    f"cannot inspect migration source {candidate}: {error}"
                ) from error
            if _is_link_like(candidate, metadata):
                raise UnsafeMigrationPathError(
                    f"migration source tree may not contain symlinks: {candidate}"
                )


def _iter_source_files(source: Path, *, include_all_python: bool) -> Iterator[Path]:
    if source.is_file():
        if source.suffix == ".py" and _selected_python_name(
            source.name, include_all_python=include_all_python
        ):
            yield source
        return
    yield from _walk_source_directory(source, include_all_python=include_all_python)


def _walk_source_directory(directory: Path, *, include_all_python: bool) -> Iterator[Path]:
    try:
        entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
    except OSError as error:
        raise SourcePathError(f"cannot enumerate migration source {directory}: {error}") from error
    for entry in entries:
        candidate = Path(entry.path)
        try:
            metadata = candidate.lstat()
        except OSError as error:
            raise SourcePathError(
                f"cannot inspect migration source {candidate}: {error}"
            ) from error
        if _is_link_like(candidate, metadata):
            raise UnsafeMigrationPathError(
                f"migration source tree may not contain symlinks: {candidate}"
            )
        if entry.name.startswith(".") or entry.name in _SNAPSHOT_EXCLUDED_NAMES:
            continue
        if stat.S_ISDIR(metadata.st_mode):
            yield from _walk_source_directory(candidate, include_all_python=include_all_python)
        elif (
            stat.S_ISREG(metadata.st_mode)
            and candidate.suffix == ".py"
            and _selected_python_name(candidate.name, include_all_python=include_all_python)
        ):
            yield candidate


def _selected_python_name(name: str, *, include_all_python: bool) -> bool:
    return include_all_python or (
        name == "conftest.py"
        or fnmatch.fnmatchcase(name, "test*.py")
        or fnmatch.fnmatchcase(name, "*_test.py")
    )


def _read_regular_file(path: Path) -> bytes:
    try:
        before = path.lstat()
    except OSError as error:
        raise SourcePathError(f"cannot inspect source file {path}: {error}") from error
    if _is_link_like(path, before) or not stat.S_ISREG(before.st_mode):
        raise SourcePathError(f"source is not a regular non-symlink file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise SourcePathError(f"cannot open source file {path}: {error}") from error
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise SourceChangedError(f"source file changed while opening: {path}")
        with os.fdopen(descriptor, "rb", closefd=True) as source:
            data = source.read()
            after = os.fstat(source.fileno())
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        raise
    if (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
        raise SourceChangedError(f"source file changed while reading: {path}")
    return data


def _decode_python_source(path: Path, data: bytes) -> str:
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
        return data.decode(encoding)
    except (LookupError, SyntaxError, UnicodeDecodeError) as error:
        raise SourcePathError(f"cannot decode Python source {path}: {error}") from error


def _copy_directory_contents(source: Path, destination: Path) -> None:
    try:
        entries = sorted(os.scandir(source), key=lambda entry: entry.name)
    except OSError as error:
        raise MigrationFilesystemError(
            f"cannot enumerate project directory {source}: {error}"
        ) from error
    for entry in entries:
        if entry.name in _SHADOW_EXCLUDED_NAMES:
            continue
        source_path = Path(entry.path)
        destination_path = destination / entry.name
        try:
            metadata = source_path.lstat()
        except OSError as error:
            raise MigrationFilesystemError(
                f"cannot inspect project path {source_path}: {error}"
            ) from error
        if _is_link_like(source_path, metadata):
            raise UnsafeMigrationPathError(
                f"shadow copy refuses project symlink or reparse point: {source_path}"
            )
        if stat.S_ISDIR(metadata.st_mode):
            # Populate with owner-write permission, then restore the source
            # access mode.  This also supports read-only source directories.
            destination_path.mkdir(mode=0o700)
            _copy_directory_contents(source_path, destination_path)
            destination_path.chmod(stat.S_IMODE(metadata.st_mode) & 0o777)
        elif stat.S_ISREG(metadata.st_mode):
            _copy_regular_file(source_path, destination_path, metadata)
        else:
            raise UnsafeMigrationPathError(
                f"shadow copy refuses non-regular project path: {source_path}"
            )


def _copy_regular_file(source: Path, destination: Path, metadata: os.stat_result) -> None:
    data = _read_regular_file(source)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(destination, flags, stat.S_IMODE(metadata.st_mode) & 0o777)
    with os.fdopen(descriptor, "wb", closefd=True) as target:
        target.write(data)


def _ensure_real_directory(path: Path, *, mode: int) -> None:
    try:
        path.mkdir(mode=mode)
    except FileExistsError:
        if _is_link_like(path) or not path.is_dir():
            raise UnsafeMigrationPathError(f"expected a real directory: {path}") from None


def _validate_artifact_path(raw_path: Path) -> Path:
    raw_text = str(raw_path)
    if not raw_text or "\x00" in raw_text:
        raise UnsafeMigrationPathError("generated artifact path is empty or contains NUL")
    # Check both separator conventions so an artifact remains safe if a report
    # created on one operating system is consumed on another.
    portable_parts = tuple(part for part in raw_text.replace("\\", "/").split("/") if part)
    if raw_path.is_absolute() or raw_path.anchor or re.match(r"^[A-Za-z]:", raw_text):
        raise UnsafeMigrationPathError(f"generated artifact path must be relative: {raw_path}")
    if not portable_parts or any(part in {".", ".."} for part in portable_parts):
        raise UnsafeMigrationPathError(f"unsafe generated artifact path: {raw_path}")
    return Path(*portable_parts)


def _ensure_artifact_parents(root: Path, parent: Path) -> None:
    relative = parent.relative_to(root)
    cursor = root
    for component in relative.parts:
        cursor /= component
        try:
            cursor.mkdir(mode=0o755)
        except FileExistsError:
            if _is_link_like(cursor) or not cursor.is_dir():
                raise UnsafeMigrationPathError(
                    f"generated artifact parent is not a real directory: {cursor}"
                ) from None


def _secure_dir_fd_supported() -> bool:
    return _SECURE_DIR_FD_AVAILABLE


def _open_directory_at(parent_fd: int, name: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    return os.open(name, flags, dir_fd=parent_fd)


def _open_or_create_directory_at(parent_fd: int, name: str, *, mode: int) -> int:
    with suppress(FileExistsError):
        os.mkdir(name, mode, dir_fd=parent_fd)
    try:
        return _open_directory_at(parent_fd, name)
    except OSError as error:
        raise UnsafeMigrationPathError(
            f"expected an anchored real directory: {name}: {error}"
        ) from error


def _open_relative_directory(root_fd: int, parts: Sequence[str]) -> int:
    """Open a directory through an anchored, no-symlink component walk."""

    descriptor = os.dup(root_fd)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        for component in parts:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _require_staging_capability(
    staging_root: str | Path | PublishStaging,
) -> PublishStaging:
    if not isinstance(staging_root, PublishStaging):
        raise UnsafeMigrationPathError(
            "secure staging operations require the capability returned by create_publish_staging()"
        )
    return staging_root


def _verify_descriptor_identity(
    descriptor: int,
    expected: tuple[int, int],
    *,
    purpose: str,
) -> None:
    actual = os.fstat(descriptor)
    if not stat.S_ISDIR(actual.st_mode) or (actual.st_dev, actual.st_ino) != expected:
        raise UnsafeMigrationPathError(f"{purpose} changed during migration")


def _open_staging_descriptors(
    staging: PublishStaging,
    *,
    require_payload: bool,
) -> _StagingDescriptors:
    project_fd: int | None = None
    migrations_fd: int | None = None
    transaction_fd: int | None = None
    payload_fd: int | None = None
    completed = False
    try:
        project_fd = _open_directory(staging.project_root)
        _verify_descriptor_identity(
            project_fd,
            staging.project_identity,
            purpose="migration project root",
        )
        migrations_fd = _open_relative_directory(project_fd, (".testenix", "migrations"))
        transaction_fd = _open_directory_at(migrations_fd, staging.transaction_id)
        _verify_descriptor_identity(
            transaction_fd,
            staging.transaction_identity,
            purpose="migration transaction",
        )
        try:
            payload_fd = _open_directory_at(transaction_fd, "payload")
        except FileNotFoundError:
            if require_payload:
                raise UnsafeMigrationPathError("migration staging payload disappeared") from None
        if payload_fd is not None:
            _verify_descriptor_identity(
                payload_fd,
                staging.payload_identity,
                purpose="migration staging payload",
            )
        descriptors = _StagingDescriptors(
            project_fd=project_fd,
            migrations_fd=migrations_fd,
            transaction_fd=transaction_fd,
            payload_fd=payload_fd,
        )
        completed = True
        return descriptors
    except MigrationFilesystemError:
        raise
    except OSError as error:
        raise UnsafeMigrationPathError(
            f"cannot reopen migration staging safely: {error}"
        ) from error
    finally:
        if not completed:
            if payload_fd is not None:
                os.close(payload_fd)
            if transaction_fd is not None:
                os.close(transaction_fd)
            if migrations_fd is not None:
                os.close(migrations_fd)
            if project_fd is not None:
                os.close(project_fd)


def _write_artifact_at(root_fd: int, relative: Path, content: bytes) -> None:
    parent_fd = os.dup(root_fd)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        for component in relative.parts[:-1]:
            with suppress(FileExistsError):
                os.mkdir(component, 0o755, dir_fd=parent_fd)
            try:
                next_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            except OSError as error:
                raise UnsafeMigrationPathError(
                    f"generated artifact parent is not a real directory: {relative.parent}"
                ) from error
            os.close(parent_fd)
            parent_fd = next_fd

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(relative.name, flags, 0o644, dir_fd=parent_fd)
        except FileExistsError as error:
            raise UnsafeMigrationPathError(
                f"generated artifact already exists: {relative}"
            ) from error
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as target:
                target.write(content)
                target.flush()
                os.fsync(target.fileno())
        except BaseException:
            with suppress(OSError):
                os.unlink(relative.name, dir_fd=parent_fd)
            raise
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _validated_staging_path(
    staging_root: str | Path | PublishStaging,
    paths: MigrationPaths,
) -> Path:
    raw_staging = Path(staging_root)
    if _is_link_like(raw_staging):
        raise UnsafeMigrationPathError(f"staging root may not be a symlink: {raw_staging}")
    try:
        staging = raw_staging.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise UnsafeMigrationPathError(
            f"cannot resolve staging root {raw_staging}: {error}"
        ) from error
    expected_parent = paths.project_root / ".testenix" / "migrations"
    try:
        relative = staging.relative_to(expected_parent)
    except ValueError as error:
        raise UnsafeMigrationPathError(
            f"staging root must be below {expected_parent}: {staging}"
        ) from error
    if len(relative.parts) != 2 or relative.parts[1] != "payload":
        raise UnsafeMigrationPathError(f"invalid migration staging layout: {staging}")
    if not _TRANSACTION_ID.fullmatch(relative.parts[0]):
        raise UnsafeMigrationPathError(f"invalid migration transaction id: {relative.parts[0]!r}")
    _assert_no_link_components(paths.project_root, staging, purpose="staging")
    if not staging.is_dir():
        raise UnsafeMigrationPathError(f"staging payload is not a directory: {staging}")
    return staging


def _rename_noreplace_at(
    old_fd: int,
    old_name: str,
    new_fd: int,
    new_name: str,
    destination: Path,
) -> None:
    try:
        if sys.platform.startswith("linux"):
            _linux_rename_noreplace(old_fd, old_name, new_fd, new_name, destination)
            return
        if sys.platform == "darwin":
            _darwin_rename_noreplace(old_fd, old_name, new_fd, new_name, destination)
            return
    except FileExistsError as error:
        raise OutputExistsError(f"migration output already exists: {destination}") from error
    except OSError as error:
        if error.errno in {errno.EEXIST, errno.ENOTEMPTY}:
            raise OutputExistsError(f"migration output already exists: {destination}") from error
        raise AtomicPublishError(f"cannot publish migration output: {error}") from error
    raise AtomicPublishError(
        f"atomic no-replace directory publication is unsupported on {sys.platform}"
    )


def _linux_rename_noreplace(
    old_fd: int,
    old_name: str,
    new_fd: int,
    new_name: str,
    destination: Path,
) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    function = getattr(library, "renameat2", None)
    if function is None:
        raise AtomicPublishError("libc does not expose renameat2(RENAME_NOREPLACE)")
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    result = function(
        old_fd,
        os.fsencode(old_name),
        new_fd,
        os.fsencode(new_name),
        _RENAME_NOREPLACE,
    )
    error_number = ctypes.get_errno() if result != 0 else 0
    if result != 0:
        raise OSError(error_number, os.strerror(error_number), str(destination))


def _darwin_rename_noreplace(
    old_fd: int,
    old_name: str,
    new_fd: int,
    new_name: str,
    destination: Path,
) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    function = getattr(library, "renameatx_np", None)
    if function is None:
        raise AtomicPublishError("libc does not expose renameatx_np(RENAME_EXCL)")
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    result = function(
        old_fd,
        os.fsencode(old_name),
        new_fd,
        os.fsencode(new_name),
        _RENAME_EXCL,
    )
    error_number = ctypes.get_errno() if result != 0 else 0
    if result != 0:
        raise OSError(error_number, os.strerror(error_number), str(destination))


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    return os.open(path, flags)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = _open_directory(path)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _unlink_owned_lock_at(parent_fd: int, name: str, expected: os.stat_result) -> None:
    try:
        actual = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino):
        with suppress(OSError):
            os.unlink(name, dir_fd=parent_fd)


def _remove_directory_contents_at(directory_fd: int) -> None:
    """Recursively unlink entries through an already-open directory descriptor."""

    for name in os.listdir(directory_fd):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = _open_directory_at(directory_fd, name)
            try:
                opened = os.fstat(child_fd)
                if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                    raise UnsafeMigrationPathError(
                        f"migration staging entry changed during cleanup: {name}"
                    )
                _remove_directory_contents_at(child_fd)
            finally:
                os.close(child_fd)
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise UnsafeMigrationPathError(
                    f"migration staging directory changed during cleanup: {name}"
                )
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


__all__ = [
    "AtomicPublishError",
    "MigrationFilesystemError",
    "MigrationPaths",
    "OutputExistsError",
    "PublishStaging",
    "PublishedOutputDurabilityError",
    "SourceChangedError",
    "SourcePathError",
    "UnsafeMigrationPathError",
    "atomic_publish",
    "cleanup_publish_staging",
    "copy_project_to_shadow",
    "create_publish_staging",
    "snapshot_source_files",
    "source_snapshot_digest",
    "validate_migration_paths",
    "validate_migration_report_path",
    "verify_source_snapshot",
    "write_staged_artifacts",
]
