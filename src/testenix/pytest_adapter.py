"""Compatibility adapter for existing pytest suites.

The native Testenix engine deliberately does not import pytest.  This adapter
preserves pytest semantics by handing the current CLI process to pytest,
without translating tests, arguments, plugins, configuration, output, signals,
or exit status.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Callable, Sequence
from typing import NoReturn, cast

PYTEST_INSTALL_HINT = 'python -m pip install "testenix[pytest]"'


class PytestUnavailableError(RuntimeError):
    """Raised when the compatibility command cannot find pytest."""


class PytestInvocationError(RuntimeError):
    """Raised when pytest cannot take over the current CLI process."""


def run_pytest(arguments: Sequence[str] = ()) -> int:
    """Run pytest in the current CLI process using the same interpreter.

    POSIX supports a true process overlay.  Windows uses pytest's public console
    entry point in-process because its ``exec`` family does not provide the same
    replacement semantics.  Both paths give pytest ownership of terminal
    interaction, signals, output, and the final exit status.
    """

    if importlib.util.find_spec("pytest") is None:
        raise PytestUnavailableError(
            f"pytest is not installed in this Python environment; run: {PYTEST_INSTALL_HINT}"
        )

    if _is_windows():
        return _run_pytest_in_process(arguments)
    _exec_pytest(arguments)


def _is_windows() -> bool:
    return os.name == "nt"


def _run_pytest_in_process(arguments: Sequence[str]) -> int:
    try:
        pytest_module = importlib.import_module("pytest")
    except ImportError as error:
        raise PytestInvocationError(f"cannot import pytest: {error}") from error

    console_main = getattr(pytest_module, "console_main", None)
    if not callable(console_main):
        raise PytestInvocationError("installed pytest does not expose console_main")
    typed_console_main = cast(Callable[[], int], console_main)
    original_argv = sys.argv
    sys.argv = [original_argv[0], *arguments]
    try:
        return int(typed_console_main())
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as error:
        raise PytestInvocationError(f"cannot run pytest: {error}") from error
    finally:
        sys.argv = original_argv


def _exec_pytest(arguments: Sequence[str]) -> NoReturn:
    command = (sys.executable, "-m", "pytest", *tuple(arguments))
    try:
        os.execv(sys.executable, command)
    except OSError as error:
        raise PytestInvocationError(f"cannot start pytest: {error}") from error
    raise AssertionError("os.execv returned unexpectedly")  # pragma: no cover


__all__ = [
    "PYTEST_INSTALL_HINT",
    "PytestInvocationError",
    "PytestUnavailableError",
    "run_pytest",
]
