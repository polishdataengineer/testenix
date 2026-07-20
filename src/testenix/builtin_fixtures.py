"""Dependency-free built-in fixtures for the native Testenix runtime."""

from __future__ import annotations

import importlib
import inspect
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


class _NotSet:
    __slots__ = ()


_NOT_SET = _NotSet()


@dataclass(frozen=True, slots=True)
class _AttributeUndo:
    target: object
    name: str
    previous: object


@dataclass(frozen=True, slots=True)
class _EnvironmentUndo:
    name: str
    previous: object


_UndoAction = _AttributeUndo | _EnvironmentUndo


def _resolve_dotted_target(import_path: str) -> tuple[object, str]:
    """Resolve ``package.module.owner.attribute`` without masking import failures."""

    parts = import_path.split(".")
    if len(parts) < 2 or any(not part for part in parts):
        raise TypeError("dotted monkeypatch targets must contain a module and attribute")

    for module_length in range(len(parts) - 1, 0, -1):
        module_name = ".".join(parts[:module_length])
        try:
            owner: object = importlib.import_module(module_name)
        except ModuleNotFoundError as error:
            # Trying ``package.module.Class`` as a module is expected to fail.
            # A missing dependency raised *inside* an import must not be hidden.
            if error.name is not None and (
                module_name == error.name or module_name.startswith(f"{error.name}.")
            ):
                continue
            raise
        for component in parts[module_length:-1]:
            owner = getattr(owner, component)
        return owner, parts[-1]

    raise ModuleNotFoundError(f"cannot import an owner for monkeypatch target {import_path!r}")


class MonkeyPatch:
    """A small, dependency-free subset of pytest's reversible monkeypatch API.

    Testenix intentionally supports only attribute replacement and environment
    variables. Every successful fixture use calls :meth:`undo` during teardown,
    and manual calls to ``undo()`` are idempotent.
    """

    def __init__(self) -> None:
        self._undo_actions: list[_UndoAction] = []

    def setattr(
        self,
        target: object | str,
        name: object,
        value: object = _NOT_SET,
        raising: bool = True,
    ) -> None:
        """Set an object attribute or a dotted import path and remember its old value."""

        if not isinstance(raising, bool):
            raise TypeError("raising must be a boolean")
        if value is _NOT_SET:
            if not isinstance(target, str):
                raise TypeError("two-argument MonkeyPatch.setattr() requires a dotted import path")
            resolved_target, attribute_name = _resolve_dotted_target(target)
            replacement = name
        else:
            if not isinstance(name, str):
                raise TypeError("attribute name must be a string")
            resolved_target = target
            attribute_name = name
            replacement = value

        previous = getattr(resolved_target, attribute_name, _NOT_SET)
        if previous is _NOT_SET and raising:
            raise AttributeError(f"{resolved_target!r} has no attribute {attribute_name!r}")
        # Avoid binding descriptors while saving class attributes. This is the
        # value that must be restored to reproduce the original class body.
        if inspect.isclass(resolved_target):
            previous = vars(resolved_target).get(attribute_name, _NOT_SET)

        setattr(resolved_target, attribute_name, replacement)
        action = _AttributeUndo(resolved_target, attribute_name, previous)
        self._undo_actions.append(action)

    def setenv(self, name: str, value: object, prepend: str | None = None) -> None:
        """Set an environment variable and restore its previous value at teardown."""

        if not isinstance(name, str):
            raise TypeError("environment variable name must be a string")
        if prepend is not None and not isinstance(prepend, str):
            raise TypeError("prepend must be a string or None")
        rendered = str(value)
        if prepend is not None and name in os.environ:
            rendered = f"{rendered}{prepend}{os.environ[name]}"
        previous: object = os.environ.get(name, _NOT_SET)
        os.environ[name] = rendered
        self._undo_actions.append(_EnvironmentUndo(name, previous))

    def undo(self) -> None:
        """Rollback all recorded changes in LIFO order, attempting every action."""

        actions = self._undo_actions
        self._undo_actions = []
        failures: list[BaseException] = []
        for action in reversed(actions):
            try:
                if isinstance(action, _AttributeUndo):
                    if action.previous is _NOT_SET:
                        delattr(action.target, action.name)
                    else:
                        setattr(action.target, action.name, action.previous)
                elif action.previous is _NOT_SET:
                    os.environ.pop(action.name, None)
                else:
                    os.environ[action.name] = str(action.previous)
            except BaseException as error:
                failures.append(error)

        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup("multiple monkeypatch rollback actions failed", failures)


def _tmp_path_fixture() -> Iterator[Path]:
    temporary = tempfile.TemporaryDirectory(prefix="testenix-")
    try:
        yield Path(temporary.name)
    finally:
        # TemporaryDirectory has platform-specific handling for read-only paths
        # while still surfacing open-handle cleanup failures on Windows.
        temporary.cleanup()


def _monkeypatch_fixture() -> Iterator[MonkeyPatch]:
    patcher = MonkeyPatch()
    try:
        yield patcher
    finally:
        patcher.undo()


__all__ = ["MonkeyPatch"]
