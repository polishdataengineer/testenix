"""Configuration loading for the Testenix command-line interface.

Project configuration lives in ``[tool.testenix]`` in ``pyproject.toml``.  The
module intentionally has no dependency on the CLI, so the runner and library
users can load exactly the same configuration.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from math import isfinite
from pathlib import Path
from typing import Any, Literal

DEFAULT_HISTORY_PATH = Path(".testenix/history.sqlite3")


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

        object.__setattr__(self, "paths", _normalise_paths(self.paths))
        object.__setattr__(self, "tags", _normalise_tags(self.tags))
        for field_name in ("json_path", "junit_path", "history_path"):
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
        """Concrete local worker count for schedulers and process pools."""

        if self.workers == "auto":
            return max(1, os.cpu_count() or 1)
        return self.workers


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
    for name in ("json_path", "junit_path"):
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
