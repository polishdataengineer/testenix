from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

import testenix.migration_fs as migration_fs
from testenix.migration_fs import (
    AtomicPublishError,
    MigrationPaths,
    OutputExistsError,
    PublishedOutputDurabilityError,
    PublishStaging,
    SourceChangedError,
    SourcePathError,
    UnsafeMigrationPathError,
    atomic_publish,
    cleanup_publish_staging,
    copy_project_to_shadow,
    create_publish_staging,
    snapshot_source_files,
    source_snapshot_digest,
    validate_migration_paths,
    verify_source_snapshot,
    write_staged_artifacts,
)
from testenix.migration_models import GeneratedArtifact


def _project(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "project"
    tests = root / "tests"
    tests.mkdir(parents=True)
    (tests / "test_example.py").write_text(
        "def test_example():\n    assert True\n",
        encoding="utf-8",
    )
    return root, tests


def _paths(tmp_path: Path) -> MigrationPaths:
    root, _ = _project(tmp_path)
    return validate_migration_paths(root, ("tests",), "tests_testenix")


def _artifact(path: str | Path, content: str = "VALUE = 1\n") -> GeneratedArtifact:
    return GeneratedArtifact(Path(path), content, ("tests/test_example.py",))


def _symlink_or_skip(target: Path | str, link: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symbolic links are unavailable: {error}")


def _remove_retained_test_transaction(staging: PublishStaging, tmp_path: Path) -> None:
    """Remove a retained transaction only from pytest's disposable directory."""

    transaction = staging.parent.resolve(strict=True)
    sandbox = tmp_path.resolve(strict=True)
    try:
        transaction.relative_to(sandbox)
    except ValueError as error:
        raise AssertionError(f"refusing test cleanup outside {sandbox}: {transaction}") from error
    shutil.rmtree(transaction)


def _cleanup_staging_for_test(
    staging: PublishStaging,
    paths: MigrationPaths,
    tmp_path: Path,
) -> None:
    """Honor fail-closed retention, then clean only pytest-owned scratch data."""

    try:
        cleanup_publish_staging(staging, paths)
    except UnsafeMigrationPathError as error:
        if "automatic recursive staging cleanup is unavailable" not in str(error):
            raise
        _remove_retained_test_transaction(staging, tmp_path)


@pytest.fixture
def staged_payload(tmp_path: Path) -> Iterator[tuple[MigrationPaths, PublishStaging]]:
    paths = _paths(tmp_path)
    staging = create_publish_staging(paths, transaction_id="pytest-stage")
    try:
        yield paths, staging
    finally:
        _cleanup_staging_for_test(staging, paths, tmp_path)


def test_validate_returns_canonical_project_source_and_output(tmp_path: Path) -> None:
    root, tests = _project(tmp_path)

    paths = validate_migration_paths(root, (Path("tests"),), Path("tests_testenix"))

    assert paths.project_root == root.resolve()
    assert paths.sources == (tests.resolve(),)
    assert paths.output == root.resolve() / "tests_testenix"


def test_validate_rejects_symlink_project_root(tmp_path: Path) -> None:
    root, _ = _project(tmp_path)
    linked_root = tmp_path / "linked-project"
    _symlink_or_skip(root, linked_root, directory=True)

    with pytest.raises(UnsafeMigrationPathError, match="project root"):
        validate_migration_paths(linked_root, ("tests",), "native")


def test_validate_rejects_source_symlink_and_nested_symlink(tmp_path: Path) -> None:
    root, tests = _project(tmp_path)
    linked_source = root / "linked-tests"
    _symlink_or_skip(tests, linked_source, directory=True)

    with pytest.raises(UnsafeMigrationPathError, match="symlink"):
        validate_migration_paths(root, (linked_source,), "native")

    outside = tmp_path / "outside"
    outside.mkdir()
    _symlink_or_skip(outside, tests / "escape", directory=True)
    with pytest.raises(UnsafeMigrationPathError, match="symlink"):
        validate_migration_paths(root, ("tests",), "native")


def test_validate_rejects_symlinked_output_parent(tmp_path: Path) -> None:
    root, _ = _project(tmp_path)
    real_parent = root / "real-parent"
    real_parent.mkdir()
    linked_parent = root / "linked-parent"
    _symlink_or_skip(real_parent, linked_parent, directory=True)

    with pytest.raises(UnsafeMigrationPathError, match="symlink"):
        validate_migration_paths(root, ("tests",), linked_parent / "native")


@pytest.mark.parametrize("source", ("../outside", "/"))
def test_validate_rejects_sources_outside_project(tmp_path: Path, source: str) -> None:
    root, _ = _project(tmp_path)

    with pytest.raises(UnsafeMigrationPathError):
        validate_migration_paths(root, (source,), "native")


@pytest.mark.parametrize(
    "output",
    (
        "../native",
        ".",
        ".git/native",
        ".testenix/native",
        "tests/generated",
    ),
)
def test_validate_rejects_unsafe_output_locations(tmp_path: Path, output: str) -> None:
    root, _ = _project(tmp_path)
    (root / ".git").mkdir()
    (root / ".testenix").mkdir()

    with pytest.raises(UnsafeMigrationPathError):
        validate_migration_paths(root, ("tests",), output)


def test_validate_rejects_duplicate_and_overlapping_sources(tmp_path: Path) -> None:
    root, tests = _project(tmp_path)
    nested = tests / "unit"
    nested.mkdir()
    (nested / "test_unit.py").write_text("def test_unit(): pass\n", encoding="utf-8")

    with pytest.raises(SourcePathError, match="same path"):
        validate_migration_paths(root, ("tests", "tests"), "native")
    with pytest.raises(SourcePathError, match="overlap"):
        validate_migration_paths(root, ("tests", "tests/unit"), "native")


@pytest.mark.parametrize("kind", ("file", "directory"))
def test_validate_rejects_existing_output_without_modifying_it(
    tmp_path: Path,
    kind: str,
) -> None:
    root, _ = _project(tmp_path)
    output = root / "native"
    if kind == "directory":
        output.mkdir()
        sentinel = output / "sentinel"
    else:
        sentinel = output
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(OutputExistsError):
        validate_migration_paths(root, ("tests",), "native")

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_validate_rejects_broken_symlink_output(tmp_path: Path) -> None:
    root, _ = _project(tmp_path)
    output = root / "native"
    _symlink_or_skip(root / "missing-target", output)

    with pytest.raises(OutputExistsError):
        validate_migration_paths(root, ("tests",), "native")

    assert os.path.lexists(output)


def test_snapshot_default_selection_is_deterministic_and_excludes_noise(tmp_path: Path) -> None:
    root, tests = _project(tmp_path)
    (tests / "service_test.py").write_text("VALUE = 'suffix'\n", encoding="utf-8")
    (tests / "conftest.py").write_text("VALUE = 'fixture'\n", encoding="utf-8")
    (tests / "helper.py").write_text("VALUE = 'helper'\n", encoding="utf-8")
    for ignored in (".hidden", "__pycache__", ".pytest_cache", ".venv", "build", "output"):
        directory = tests / ignored
        directory.mkdir()
        (directory / "test_ignored.py").write_text("VALUE = 'ignored'\n", encoding="utf-8")
    paths = validate_migration_paths(root, ("tests",), "native")

    first = snapshot_source_files(paths)
    second = snapshot_source_files(paths)

    names = [source.migration_relative.as_posix() for source in first]
    assert names == ["conftest.py", "service_test.py", "test_example.py"]
    assert first == second
    assert all(source.project_relative.parts[0] == "tests" for source in first)
    assert source_snapshot_digest(first) == source_snapshot_digest(second)


def test_snapshot_include_all_python_adds_visible_helpers_only(tmp_path: Path) -> None:
    root, tests = _project(tmp_path)
    (tests / "helper.py").write_text("HELPER = True\n", encoding="utf-8")
    hidden = tests / ".hidden"
    hidden.mkdir()
    (hidden / "hidden_helper.py").write_text("HIDDEN = True\n", encoding="utf-8")
    paths = validate_migration_paths(root, ("tests",), "native")

    selected = snapshot_source_files(paths, include_all_python=True)

    assert [source.migration_relative.as_posix() for source in selected] == [
        "helper.py",
        "test_example.py",
    ]


def test_snapshot_multiple_sources_use_project_relative_migration_paths(tmp_path: Path) -> None:
    root, _ = _project(tmp_path)
    integration = root / "integration"
    integration.mkdir()
    (integration / "test_api.py").write_text("def test_api(): pass\n", encoding="utf-8")
    paths = validate_migration_paths(root, ("tests", "integration"), "native")

    selected = snapshot_source_files(paths)

    assert [source.migration_relative.as_posix() for source in selected] == [
        "integration/test_api.py",
        "tests/test_example.py",
    ]


def test_snapshot_honors_python_encoding_cookie(tmp_path: Path) -> None:
    root, tests = _project(tmp_path)
    encoded = tests / "test_latin1.py"
    encoded.write_bytes("# coding: latin-1\nVALUE = 'café'\n".encode("latin-1"))
    paths = validate_migration_paths(root, ("tests",), "native")

    selected = snapshot_source_files(paths)
    by_name = {source.path.name: source for source in selected}

    assert "café" in by_name["test_latin1.py"].text


def test_source_snapshot_digest_changes_with_content(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    before = snapshot_source_files(paths)
    before_digest = source_snapshot_digest(before)
    paths.sources[0].joinpath("test_example.py").write_text(
        "def test_example():\n    assert 2 + 2 == 4\n",
        encoding="utf-8",
    )

    after = snapshot_source_files(paths)

    assert source_snapshot_digest(after) != before_digest


@pytest.mark.parametrize("mutation", ("add", "remove", "change"))
def test_verify_source_snapshot_detects_all_source_drift(
    tmp_path: Path,
    mutation: str,
) -> None:
    paths = _paths(tmp_path)
    expected = snapshot_source_files(paths)
    source = paths.sources[0] / "test_example.py"
    if mutation == "add":
        paths.sources[0].joinpath("test_added.py").write_text(
            "def test_added(): pass\n", encoding="utf-8"
        )
    elif mutation == "remove":
        source.unlink()
    else:
        source.write_text("def test_example(): assert False\n", encoding="utf-8")

    with pytest.raises(SourceChangedError) as captured:
        verify_source_snapshot(paths, expected)
    if mutation == "add":
        assert "added=" in str(captured.value)
    elif mutation == "remove":
        assert "no selected test files" in str(captured.value)
    else:
        assert "changed=" in str(captured.value)


@pytest.mark.parametrize(
    "unsafe_path",
    (Path("/absolute.py"), Path("../escape.py"), Path("..\\escape.py")),
)
def test_staged_artifacts_reject_absolute_and_traversal_paths(
    staged_payload: tuple[MigrationPaths, PublishStaging],
    unsafe_path: Path,
) -> None:
    _, staging = staged_payload

    with pytest.raises(UnsafeMigrationPathError):
        write_staged_artifacts(staging, (_artifact(unsafe_path),))

    assert not any(staging.iterdir())


def test_staged_artifacts_reject_case_and_unicode_aliases(
    staged_payload: tuple[MigrationPaths, PublishStaging],
) -> None:
    _, staging = staged_payload

    with pytest.raises(UnsafeMigrationPathError, match="collision"):
        write_staged_artifacts(
            staging,
            (
                _artifact("Suite/Test_Value.py"),
                _artifact("suite/test_value.py"),
            ),
        )

    assert not any(staging.iterdir())


def test_staged_artifacts_reject_existing_entry(
    staged_payload: tuple[MigrationPaths, PublishStaging],
) -> None:
    _, staging = staged_payload
    existing = staging / "test_existing.py"
    existing.write_text("KEEP = True\n", encoding="utf-8")

    with pytest.raises(UnsafeMigrationPathError, match="must be empty"):
        write_staged_artifacts(staging, (_artifact("test_existing.py"),))

    assert existing.read_text(encoding="utf-8") == "KEEP = True\n"


def test_staged_artifacts_reject_symlink_parent_without_following_it(
    staged_payload: tuple[MigrationPaths, PublishStaging],
    tmp_path: Path,
) -> None:
    _, staging = staged_payload
    outside = tmp_path / "outside"
    outside.mkdir()
    _symlink_or_skip(outside, staging / "linked", directory=True)

    with pytest.raises(UnsafeMigrationPathError):
        write_staged_artifacts(staging, (_artifact("linked/escape.py"),))

    assert not (outside / "escape.py").exists()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX dir-fd operations")
def test_staged_artifact_parent_swap_cannot_escape_anchored_root(
    staged_payload: tuple[MigrationPaths, PublishStaging],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, staging = staged_payload
    outside = tmp_path / "outside-race"
    outside.mkdir()
    real_mkdir = os.mkdir
    swapped = False

    def racing_mkdir(
        path: str | bytes,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        real_mkdir(path, mode, dir_fd=dir_fd)
        if path == "raced" and dir_fd is not None and not swapped:
            swapped = True
            raced = staging / "raced"
            raced.rmdir()
            _symlink_or_skip(outside, raced, directory=True)

    monkeypatch.setattr(os, "mkdir", racing_mkdir)

    with pytest.raises(UnsafeMigrationPathError):
        write_staged_artifacts(staging, (_artifact("raced/escape.py"),))

    assert swapped
    assert not (outside / "escape.py").exists()


def test_staged_artifacts_write_nested_files_exclusively(
    staged_payload: tuple[MigrationPaths, PublishStaging],
) -> None:
    _, staging = staged_payload

    written = write_staged_artifacts(
        staging,
        (_artifact("nested/test_native.py", "def test_native(): pass\n"),),
    )

    assert written == (staging / "nested" / "test_native.py",)
    assert written[0].read_text(encoding="utf-8") == "def test_native(): pass\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-anchoring regression")
def test_staging_parent_swap_cannot_redirect_generated_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    outside = tmp_path / "outside-stage-race"
    outside.mkdir()
    migrations = paths.project_root / ".testenix" / "migrations"
    saved_migrations = paths.project_root / ".testenix" / "migrations-original"
    real_mkdir = os.mkdir
    swapped = False

    def racing_mkdir(
        path: str | bytes,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        real_mkdir(path, mode, dir_fd=dir_fd)
        if path == "create-race" and dir_fd is not None and not swapped:
            swapped = True
            migrations.rename(saved_migrations)
            _symlink_or_skip(outside, migrations, directory=True)

    monkeypatch.setattr(os, "mkdir", racing_mkdir)
    staging = create_publish_staging(paths, transaction_id="create-race")
    try:
        with pytest.raises(UnsafeMigrationPathError):
            write_staged_artifacts(staging, (_artifact("escape.py"),))
        assert not (outside / "create-race" / "payload" / "escape.py").exists()
    finally:
        if migrations.is_symlink():
            migrations.unlink()
        if saved_migrations.exists():
            saved_migrations.rename(migrations)
        cleanup_publish_staging(staging, paths)


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-anchoring regression")
def test_cleanup_transaction_swap_never_deletes_external_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    staging = create_publish_staging(paths, transaction_id="cleanup-race")
    write_staged_artifacts(staging, (_artifact("nested/generated.py"),))
    transaction = staging.parent
    moved_transaction = transaction.with_name("cleanup-race-moved")
    outside = tmp_path / "outside-cleanup-race"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    real_listdir = os.listdir
    swapped = False

    def racing_listdir(path: int | str | bytes | os.PathLike[str]) -> list[str]:
        nonlocal swapped
        if isinstance(path, int) and not swapped:
            swapped = True
            transaction.rename(moved_transaction)
            _symlink_or_skip(outside, transaction, directory=True)
        return real_listdir(path)

    monkeypatch.setattr(os, "listdir", racing_listdir)
    try:
        with pytest.raises(UnsafeMigrationPathError):
            cleanup_publish_staging(staging, paths)
        assert sentinel.read_text(encoding="utf-8") == "keep"
    finally:
        monkeypatch.setattr(os, "listdir", real_listdir)
        if transaction.is_symlink():
            transaction.unlink()
        if moved_transaction.exists():
            moved_transaction.rename(transaction)
        cleanup_publish_staging(staging, paths)


def test_shadow_copy_excludes_state_environments_caches_and_outputs(tmp_path: Path) -> None:
    root, _ = _project(tmp_path)
    excluded_names = (
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".testenix",
        "dist",
        "build",
        "output",
    )
    for name in excluded_names:
        directory = root / name
        directory.mkdir()
        (directory / "sentinel").write_text("excluded", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='sample'\n", encoding="utf-8")

    shadow = copy_project_to_shadow(root)
    try:
        assert (shadow / "tests" / "test_example.py").is_file()
        assert (shadow / "pyproject.toml").is_file()
        assert all(not (shadow / name).exists() for name in excluded_names)
    finally:
        shutil.rmtree(shadow)


def test_shadow_copy_rejects_visible_symlink_and_does_not_follow_it(tmp_path: Path) -> None:
    root, _ = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("outside", encoding="utf-8")
    _symlink_or_skip(outside, root / "linked", directory=True)

    with pytest.raises(UnsafeMigrationPathError, match="symlink"):
        copy_project_to_shadow(root)

    assert sentinel.read_text(encoding="utf-8") == "outside"


def test_atomic_publish_creates_complete_output_and_cleanup_removes_transaction(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    staging = create_publish_staging(paths, transaction_id="publish-success")
    transaction_root = staging.parent
    write_staged_artifacts(staging, (_artifact("test_native.py"),))

    try:
        try:
            output = atomic_publish(staging, paths)
        except AtomicPublishError as error:
            if "unsupported" in str(error) or "does not expose" in str(error):
                pytest.skip(str(error))
            raise
        assert output == paths.output
        assert (output / "test_native.py").read_text(encoding="utf-8") == "VALUE = 1\n"
        assert not staging.exists()
    finally:
        cleanup_publish_staging(staging, paths)

    assert not transaction_root.exists()
    assert (paths.sources[0] / "test_example.py").is_file()


def test_atomic_publish_race_never_replaces_existing_output(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    staging = create_publish_staging(paths, transaction_id="publish-race")
    write_staged_artifacts(staging, (_artifact("test_native.py"),))
    paths.output.mkdir()
    sentinel = paths.output / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")

    try:
        with pytest.raises(OutputExistsError):
            atomic_publish(staging, paths)
        assert sentinel.read_text(encoding="utf-8") == "keep"
        assert (staging / "test_native.py").is_file()
    finally:
        _cleanup_staging_for_test(staging, paths, tmp_path)


def test_cleanup_retains_nonempty_staging_without_safe_recursive_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(migration_fs, "_SECURE_DIR_FD_AVAILABLE", False)
    paths = _paths(tmp_path)
    staging = create_publish_staging(paths, transaction_id="retain-nonempty")
    transaction = staging.parent
    generated = staging / "nested" / "test_native.py"
    write_staged_artifacts(staging, (_artifact("nested/test_native.py"),))

    try:
        with pytest.raises(
            UnsafeMigrationPathError,
            match="automatic recursive staging cleanup is unavailable",
        ):
            cleanup_publish_staging(staging, paths)

        assert transaction.is_dir()
        assert generated.read_text(encoding="utf-8") == "VALUE = 1\n"
    finally:
        _remove_retained_test_transaction(staging, tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync regression")
def test_atomic_publish_reports_post_rename_durability_failure_truthfully(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    staging = create_publish_staging(paths, transaction_id="publish-fsync-warning")
    write_staged_artifacts(staging, (_artifact("test_native.py"),))
    real_fsync = os.fsync

    def failing_post_rename_fsync(descriptor: int) -> None:
        if paths.output.exists():
            raise OSError("simulated directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", failing_post_rename_fsync)
    try:
        with pytest.raises(PublishedOutputDurabilityError):
            atomic_publish(staging, paths)
        assert (paths.output / "test_native.py").is_file()
    finally:
        monkeypatch.setattr(os, "fsync", real_fsync)
        cleanup_publish_staging(staging, paths)


def test_cleanup_refuses_staging_outside_transaction_root(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    legitimate = create_publish_staging(paths, transaction_id="cleanup-guard")
    unrelated = paths.project_root / "unrelated" / "payload"
    unrelated.mkdir(parents=True)
    sentinel = unrelated / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")

    try:
        with pytest.raises(UnsafeMigrationPathError):
            cleanup_publish_staging(unrelated, paths)
    finally:
        cleanup_publish_staging(legitimate, paths)

    assert sentinel.read_text(encoding="utf-8") == "keep"
