"""Configuration loading for the Testenix command-line interface.

Project configuration lives in ``[tool.testenix]`` in ``pyproject.toml``.  The
module intentionally has no dependency on the CLI, so the runner and library
users can load exactly the same configuration.
"""

from __future__ import annotations

import copy
import os
import re
import stat
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from contextlib import suppress
from dataclasses import dataclass, replace
from math import isfinite
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from testenix.contracts import TestSpec
    from testenix.tuning import SpawnMethod

DEFAULT_HISTORY_PATH = Path(".testenix/history.sqlite3")


class _ExpectedSourceUnset:
    """Sentinel distinguishing an absent file from an omitted snapshot guard."""


_EXPECTED_SOURCE_UNSET = _ExpectedSourceUnset()


class ConfigError(ValueError):
    """Raised when Testenix configuration is malformed."""


@dataclass(frozen=True, slots=True)
class TestenixConfig:
    """Validated execution and reporting settings.

    Paths are kept relative to the process working directory.  This mirrors
    normal command-line path handling and makes a config object safe to pass to
    a worker process without retaining hidden project state.
    """

    __test__ = False

    paths: tuple[str, ...] = ("tests",)
    workers: int | Literal["auto"] = "auto"
    retries: int = 0
    timeout: float | None = None
    tags: tuple[str, ...] = ()
    json_path: Path | None = None
    junit_path: Path | None = None
    history_path: Path | None = DEFAULT_HISTORY_PATH
    shard_modules: bool = False
    manifest_path: Path | None = None

    def __post_init__(self) -> None:
        if self.workers != "auto":
            if isinstance(self.workers, bool) or not isinstance(self.workers, int):
                raise ConfigError("workers must be 'auto' or an integer")
            if self.workers < 1:
                raise ConfigError("workers must be at least 1")
        if isinstance(self.retries, bool) or not isinstance(self.retries, int):
            raise ConfigError("retries must be an integer")
        if self.retries < 0:
            raise ConfigError("retries cannot be negative")
        if self.timeout is not None:
            if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)):
                raise ConfigError("timeout must be a number")
            timeout = float(self.timeout)
            if not isfinite(timeout) or timeout <= 0:
                raise ConfigError("timeout must be a finite number greater than zero")
            object.__setattr__(self, "timeout", timeout)
        if not isinstance(self.shard_modules, bool):
            raise ConfigError("shard_modules must be a boolean")

        object.__setattr__(self, "paths", _normalise_paths(self.paths))
        object.__setattr__(self, "tags", _normalise_tags(self.tags))
        for field_name in ("json_path", "junit_path", "history_path", "manifest_path"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, Path):
                object.__setattr__(self, field_name, Path(value))

    def with_overrides(self, **values: Any) -> TestenixConfig:
        """Return a validated copy containing command-line overrides."""

        known = set(self.__dataclass_fields__)
        unknown = sorted(set(values) - known)
        if unknown:
            raise ConfigError(f"unknown Testenix option(s): {', '.join(unknown)}")
        return replace(self, **values)

    @property
    def resolved_workers(self) -> int:
        """Pre-discovery worker capacity retained for compatibility.

        Native execution should prefer :meth:`resolve_workers`, which can see
        the scheduler's real module/timeout execution units.  Explicit integer
        configuration has identical behavior through both APIs.
        """

        if self.workers == "auto":
            return max(1, os.cpu_count() or 1)
        return self.workers

    def resolve_workers(
        self,
        selected_specs: Sequence[TestSpec],
        durations: Mapping[str, float],
        *,
        spawn_method: SpawnMethod = "spawn",
        shardable_paths: AbstractSet[str] = frozenset(),
    ) -> int:
        """Resolve an adaptive count after discovery and history lookup."""

        from testenix.tuning import resolve_adaptive_workers

        return resolve_adaptive_workers(
            self,
            selected_specs,
            durations,
            spawn_method=spawn_method,
            shardable_paths=shardable_paths,
        )


# A concise alias is convenient for embedders and keeps the public API flexible.
Config = TestenixConfig


def load_config(path: str | Path | None = None) -> TestenixConfig:
    """Load ``[tool.testenix]`` from a pyproject file.

    If *path* is omitted and the current directory has no ``pyproject.toml``,
    defaults are returned.  An explicitly requested missing file is an error.
    """

    explicit_path = path is not None
    config_path = Path(path) if path is not None else Path("pyproject.toml")
    if not config_path.exists():
        if explicit_path:
            raise ConfigError(f"configuration file does not exist: {config_path}")
        return TestenixConfig()

    try:
        with config_path.open("rb") as source:
            document = tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"cannot read {config_path}: {error}") from error

    tool = document.get("tool", {})
    if not isinstance(tool, Mapping):
        raise ConfigError("[tool] must be a TOML table")
    raw = tool.get("testenix", {})
    if not isinstance(raw, Mapping):
        raise ConfigError("[tool.testenix] must be a TOML table")
    return config_from_mapping(raw)


def config_from_mapping(raw: Mapping[str, Any]) -> TestenixConfig:
    """Build a config from an already decoded ``[tool.testenix]`` mapping."""

    aliases = {
        "json": "json_path",
        "junit": "junit_path",
        "history": "history_path",
        "manifest": "manifest_path",
    }
    allowed = set(TestenixConfig.__dataclass_fields__) | set(aliases)
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"unknown option(s) in [tool.testenix]: {', '.join(unknown)}")

    values: dict[str, Any] = {}
    for name, value in raw.items():
        target = aliases.get(name, name)
        if target in values:
            raise ConfigError(f"option {target!r} is configured more than once")
        values[target] = value

    if "tags" in values:
        values["tags"] = _normalise_tags(values["tags"])
    if "paths" in values:
        values["paths"] = _normalise_paths(values["paths"])
    for name in ("json_path", "junit_path", "manifest_path"):
        if name in values:
            values[name] = _optional_path(values[name], name)
    if "history_path" in values:
        history = values["history_path"]
        if history is False:
            values["history_path"] = None
        else:
            values["history_path"] = _optional_path(history, "history")

    try:
        return TestenixConfig(**values)
    except TypeError as error:
        raise ConfigError(f"invalid [tool.testenix] configuration: {error}") from error


def write_worker_recommendation(
    path: str | Path,
    workers: int,
    *,
    expected_source: bytes | None | _ExpectedSourceUnset = _EXPECTED_SOURCE_UNSET,
) -> bool:
    """Atomically persist an explicit worker recommendation in ``pyproject.toml``.

    This operation is intentionally separate from loading and tuning.  Callers
    must expose an explicit user action (the CLI uses ``testenix tune --write``)
    before invoking it.  ``True`` means bytes changed; an already matching
    configuration returns ``False``.  When *expected_source* is supplied, it
    acts as an optimistic byte-drift guard: ``None`` means the file must still
    be absent, while bytes must still match exactly. The transformed content is
    checked again immediately before the final atomic replacement.
    """

    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ConfigError("workers must be a positive integer")
    config_path = Path(path)
    if config_path.is_symlink():
        raise ConfigError(f"refusing to replace symbolic link: {config_path}")
    try:
        raw_source = config_path.read_bytes() if config_path.exists() else None
        if expected_source is not _EXPECTED_SOURCE_UNSET and raw_source != expected_source:
            raise ConfigError("configuration changed while tuning; recommendation was not written")
        source = (raw_source or b"").decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigError(f"cannot read {config_path}: {error}") from error

    remainder = source.replace("\r\n", "")
    if "\r" in remainder or ("\r\n" in source and "\n" in remainder):
        raise ConfigError(f"cannot update {config_path}: mixed or unsupported line endings")
    newline = "\r\n" if "\r\n" in source else "\n"
    normalised = source.replace("\r\n", "\n")
    try:
        before = tomllib.loads(normalised) if normalised else {}
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"cannot update {config_path}: {error}") from error

    updated_normalised = _set_workers_in_toml(normalised, workers)
    try:
        parsed = tomllib.loads(updated_normalised)
        expected = copy.deepcopy(before)
        tool = expected.setdefault("tool", {})
        if not isinstance(tool, dict):
            raise ConfigError("[tool] must be a TOML table")
        configured = tool.setdefault("testenix", {})
        if not isinstance(configured, dict):
            raise ConfigError("[tool.testenix] must be a TOML table")
        configured["workers"] = workers
        if parsed != expected:
            raise ConfigError(
                "unsupported TOML layout: refusing an update that would change other values"
            )
        loaded = config_from_mapping(configured)
        if loaded.workers != workers:
            raise ConfigError("worker recommendation was not applied")
    except (tomllib.TOMLDecodeError, ConfigError, AttributeError) as error:
        raise ConfigError(f"cannot update {config_path}: {error}") from error
    updated = updated_normalised if newline == "\n" else updated_normalised.replace("\n", "\r\n")
    if source == updated:
        current_source = config_path.read_bytes() if config_path.exists() else None
        if current_source != raw_source:
            raise ConfigError(
                "configuration changed while preparing the update; recommendation was not written"
            )
        return False
    # Always protect the read/transform/write cycle, even for library callers
    # which did not provide a longer-lived tuning snapshot.
    _atomic_write_text(config_path, updated, expected_source=raw_source)
    return True


def _set_workers_in_toml(source: str, workers: int) -> str:
    table_pattern = re.compile(r"(?m)^[ \t]*\[tool\.testenix\][ \t]*(?:#.*)?$")
    next_table_pattern = re.compile(r"(?m)^[ \t]*\[")
    workers_pattern = re.compile(
        r"(?m)^(?P<indent>[ \t]*)workers[ \t]*=[^#\n]*(?P<comment>[ \t]*#.*)?$"
    )
    table = table_pattern.search(source)
    if table is None:
        separator = "" if not source else ("" if source.endswith("\n\n") else "\n")
        return f"{source}{separator}[tool.testenix]\nworkers = {workers}\n"

    section_start = table.end()
    following = next_table_pattern.search(source, section_start)
    section_end = len(source) if following is None else following.start()
    section = source[section_start:section_end]
    existing = workers_pattern.search(section)
    if existing is not None:
        comment = existing.group("comment") or ""
        if comment:
            comment = f" {comment.lstrip()}"
        replacement = f"{existing.group('indent')}workers = {workers}{comment}"
        updated_section = section[: existing.start()] + replacement + section[existing.end() :]
    else:
        updated_section = f"\nworkers = {workers}" + section
    return source[:section_start] + updated_section + source[section_end:]


def _atomic_write_text(path: Path, content: str, *, expected_source: bytes | None) -> None:
    if path.is_symlink():
        raise ConfigError(f"refusing to replace symbolic link: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.testenix-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path = Path(temporary_name)
        if existing_mode is not None:
            temporary_path.chmod(existing_mode)
        if path.is_symlink():
            raise ConfigError(f"refusing to replace symbolic link: {path}")
        current_source = path.read_bytes() if path.exists() else None
        if current_source != expected_source:
            raise ConfigError(
                "configuration changed while preparing the update; recommendation was not written"
            )
        os.replace(temporary_path, path)
        temporary_name = None
    except OSError as error:
        raise ConfigError(f"cannot write {path}: {error}") from error
    finally:
        if temporary_name is not None:
            with suppress(FileNotFoundError):
                Path(temporary_name).unlink()


def _normalise_tags(value: str | Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, str):
        candidates = value.split(",")
    elif isinstance(value, Sequence):
        candidates = []
        for item in value:
            if not isinstance(item, str):
                raise ConfigError("tags must contain only strings")
            candidates.extend(item.split(","))
    else:
        raise ConfigError("tags must be a string or a list of strings")

    tags = {candidate.strip() for candidate in candidates if candidate.strip()}
    return tuple(sorted(tags))


def _normalise_paths(value: str | Sequence[str]) -> tuple[str, ...]:
    candidates: Sequence[str]
    if isinstance(value, str):
        candidates = (value,)
    elif isinstance(value, Sequence):
        candidates = tuple(value)
    else:
        raise ConfigError("paths must be a string or a list of strings")

    paths: list[str] = []
    for item in candidates:
        if not isinstance(item, str):
            raise ConfigError("paths must contain only strings")
        item = item.strip()
        if not item:
            raise ConfigError("paths cannot contain an empty path")
        paths.append(item)
    if not paths:
        raise ConfigError("paths must contain at least one test path")
    return tuple(paths)


def _optional_path(value: Any, option: str) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, (str, Path)):
        raise ConfigError(f"{option} must be a filesystem path")
    if not str(value).strip():
        raise ConfigError(f"{option} cannot be empty")
    return Path(value)
