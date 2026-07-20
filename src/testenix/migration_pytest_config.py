"""Static pytest-asyncio configuration guard for safe source migration.

The converter can remove a bare ``@pytest.mark.asyncio`` marker only when the
source runner uses function-scoped, non-debug event loops.  This module mirrors
pytest's configuration-file discovery without importing pytest or executing a
project plugin.  Differential execution in disposable project shadows remains
the authoritative validation gate after this preflight analysis.
"""

from __future__ import annotations

import ast
import configparser
import importlib.metadata as importlib_metadata
import os
import re
import shlex
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from testenix.migration_models import MigrationDiagnostic, SourceFile

_PYTEST_8_CONFIG_NAMES = (
    "pytest.ini",
    ".pytest.ini",
    "pyproject.toml",
    "tox.ini",
    "setup.cfg",
)
_PYTEST_9_CONFIG_NAMES = (
    "pytest.toml",
    ".pytest.toml",
    *_PYTEST_8_CONFIG_NAMES,
)
_VALID_LOOP_SCOPES = frozenset({"function", "class", "module", "package", "session"})
_FALSE_VALUES = frozenset({"0", "f", "false", "n", "no", "off"})
_TRUE_VALUES = frozenset({"1", "on", "t", "true", "y", "yes"})
_ASYNCIO_SCOPE_OPTION = "asyncio_default_test_loop_scope"
_ASYNCIO_DEBUG_OPTION = "asyncio_debug"
_OPTIONS_WITH_SEPARATE_VALUE = frozenset(
    {
        "-k",
        "-m",
        "-n",
        "-p",
        "-r",
        "-W",
        "--assert",
        "--asyncio-mode",
        "--basetemp",
        "--cache-show",
        "--capture",
        "--code-highlight",
        "--color",
        "--confcutdir",
        "--debug",
        "--deselect",
        "--dist",
        "--doctest-glob",
        "--doctest-report",
        "--durations",
        "--durations-min",
        "--ignore",
        "--ignore-glob",
        "--import-mode",
        "--junit-prefix",
        "--junit-xml",
        "--junitxml",
        "--log-cli-date-format",
        "--log-cli-format",
        "--log-cli-level",
        "--log-date-format",
        "--log-file",
        "--log-file-date-format",
        "--log-file-format",
        "--log-file-level",
        "--log-file-mode",
        "--log-format",
        "--log-level",
        "--log-auto-indent",
        "--log-disable",
        "--maxfail",
        "--maxprocesses",
        "--max-warnings",
        "--max-worker-restart",
        "--numprocesses",
        "--pastebin",
        "--pdbcls",
        "--pythonwarnings",
        "--report-chars",
        "--rootdir",
        "--show-capture",
        "--tb",
        "--tx",
        "--verbosity",
        "--xmlpath",
    }
)


@dataclass(frozen=True, slots=True)
class _ResolvedConfig:
    path: Path
    values: Mapping[str, object]
    native_toml: bool = False


class _ConfigError(ValueError):
    def __init__(self, path: Path | None, message: str) -> None:
        super().__init__(message)
        self.path = path


@dataclass(frozen=True, slots=True)
class _ArgumentOverrides:
    values: Mapping[str, str]
    label: str
    path: Path | None
    asyncio_debug_flag: bool = False


@dataclass(frozen=True, slots=True)
class _Aliases:
    modules: Mapping[str, str]
    symbols: Mapping[str, str]

    @classmethod
    def from_tree(cls, tree: ast.Module) -> _Aliases:
        modules: dict[str, str] = {}
        symbols: dict[str, str] = {}
        for statement in tree.body:
            if isinstance(statement, ast.Import):
                for imported in statement.names:
                    if imported.name == "pytest":
                        modules[imported.asname or imported.name] = imported.name
            elif isinstance(statement, ast.ImportFrom) and statement.module == "pytest":
                for imported in statement.names:
                    if imported.name != "*":
                        symbols[imported.asname or imported.name] = f"pytest.{imported.name}"
        return cls(modules, symbols)

    def canonical(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return self.symbols.get(node.id) or self.modules.get(node.id)
        if isinstance(node, ast.Attribute):
            owner = self.canonical(node.value)
            return f"{owner}.{node.attr}" if owner is not None else None
        return None


def pytest_asyncio_config_diagnostics(
    *,
    project_root: Path,
    source_paths: Sequence[Path],
    files: Sequence[SourceFile],
    environ: Mapping[str, str] | None = None,
    pytest_major: int | None = None,
) -> tuple[MigrationDiagnostic, ...]:
    """Return blocking diagnostics for unsupported bare pytest-asyncio settings.

    Configuration is deliberately ignored for suites without a bare marker so
    an unrelated project-level pytest option cannot block a synchronous or
    unittest-only migration.
    """

    if not any(_contains_bare_asyncio_test(source) for source in files):
        return ()

    root = project_root.resolve()
    environment = os.environ if environ is None else environ
    try:
        environment_overrides = _environment_overrides(
            environment.get("PYTEST_ADDOPTS", ""),
            project_root=root,
        )
        major = _installed_pytest_major() if pytest_major is None else pytest_major
        config = _resolve_config(root, major)
        config_overrides = _config_addopts_overrides(config)
        scope_value, scope_origin, scope_native, scope_path = _effective_option(
            _ASYNCIO_SCOPE_OPTION,
            default="function",
            config=config,
            config_overrides=config_overrides,
            environment_overrides=environment_overrides,
        )
        debug_value, debug_origin, debug_native, debug_path = _effective_option(
            _ASYNCIO_DEBUG_OPTION,
            default="false",
            config=config,
            config_overrides=config_overrides,
            environment_overrides=environment_overrides,
        )
        scope = _parse_loop_scope(
            scope_value,
            native_toml=scope_native,
            error_path=scope_path,
        )
        debug = _parse_bool(
            debug_value,
            native_toml=debug_native,
            error_path=debug_path,
        )
    except _ConfigError as error:
        return (
            MigrationDiagnostic(
                code="PYT509_PYTEST_CONFIG",
                message=str(error),
                source=_diagnostic_source(root, error.path),
            ),
        )

    diagnostics: list[MigrationDiagnostic] = []
    if scope != "function":
        diagnostics.append(
            MigrationDiagnostic(
                code="PYT508_ASYNCIO_CONFIG",
                message=(
                    f"bare pytest.mark.asyncio resolves to {scope!r} event-loop scope via "
                    f"{scope_origin}; automatic migration preserves only function scope"
                ),
                source=_origin_source(root, scope_path, scope_origin),
            )
        )
    if debug:
        diagnostics.append(
            MigrationDiagnostic(
                code="PYT508_ASYNCIO_CONFIG",
                message=(
                    "pytest-asyncio debug mode is enabled via "
                    f"{debug_origin}; automatic migration does not preserve debug-loop semantics"
                ),
                source=_origin_source(root, debug_path, debug_origin),
            )
        )
    return tuple(diagnostics)


def _contains_bare_asyncio_test(source: SourceFile) -> bool:
    try:
        tree = ast.parse(source.text, filename=source.project_relative.as_posix())
    except SyntaxError:
        return False
    aliases = _Aliases.from_tree(tree)
    functions: list[ast.AsyncFunctionDef] = []
    for statement in tree.body:
        if isinstance(statement, ast.AsyncFunctionDef):
            functions.append(statement)
        elif isinstance(statement, ast.ClassDef):
            functions.extend(
                member for member in statement.body if isinstance(member, ast.AsyncFunctionDef)
            )
    return any(
        function.name.startswith("test")
        and any(
            not isinstance(decorator, ast.Call)
            and aliases.canonical(decorator) == "pytest.mark.asyncio"
            for decorator in function.decorator_list
        )
        for function in functions
    )


def _installed_pytest_major() -> int:
    """Return the installed major, defaulting to the oldest supported resolver."""

    try:
        raw_version = importlib_metadata.version("pytest")
    except importlib_metadata.PackageNotFoundError:
        return 8
    match = re.match(r"\s*(\d+)", raw_version)
    if match is None:
        return 8
    return 9 if int(match.group(1)) >= 9 else 8


def _resolve_config(
    project_root: Path,
    pytest_major: int,
) -> _ResolvedConfig | None:
    # The baseline runs with cwd=project_root and passes paths below that directory.
    # Pytest computes their common ancestor with the invocation directory first, so
    # its implicit lookup begins at project_root rather than in a nested test folder.
    names = _PYTEST_9_CONFIG_NAMES if pytest_major >= 9 else _PYTEST_8_CONFIG_NAMES
    for name in names:
        candidate = project_root / name
        if not candidate.is_file():
            continue
        loaded = _load_candidate(candidate, pytest_major)
        if loaded is not None:
            return loaded
    return None


def _load_candidate(path: Path, pytest_major: int) -> _ResolvedConfig | None:
    try:
        if path.suffix == ".ini":
            return _load_ini(path, pytest_major)
        if path.suffix == ".cfg":
            return _load_setup_cfg(path)
        if path.suffix == ".toml":
            return _load_toml(path, pytest_major)
    except (OSError, UnicodeError, configparser.Error, tomllib.TOMLDecodeError) as error:
        raise _ConfigError(
            path, f"cannot parse pytest configuration {path.name}: {error}"
        ) from error
    return None


def _read_ini(path: Path) -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser(interpolation=None)
    with path.open(encoding="utf-8") as source:
        parser.read_file(source)
    return parser


def _load_ini(path: Path, pytest_major: int) -> _ResolvedConfig | None:
    parser = _read_ini(path)
    if parser.has_section("pytest"):
        return _ResolvedConfig(path, dict(parser.items("pytest", raw=True)))
    if path.name == "pytest.ini" or (path.name == ".pytest.ini" and pytest_major >= 9):
        return _ResolvedConfig(path, {})
    return None


def _load_setup_cfg(path: Path) -> _ResolvedConfig | None:
    parser = _read_ini(path)
    if parser.has_section("tool:pytest"):
        return _ResolvedConfig(path, dict(parser.items("tool:pytest", raw=True)))
    if parser.has_section("pytest"):
        raise _ConfigError(
            path,
            "setup.cfg uses unsupported [pytest]; pytest requires [tool:pytest]",
        )
    return None


def _load_toml(path: Path, pytest_major: int) -> _ResolvedConfig | None:
    with path.open("rb") as source:
        document = tomllib.load(source)
    if path.name in {"pytest.toml", ".pytest.toml"}:
        if pytest_major < 9:
            return None
        values = document.get("pytest", {})
        if not isinstance(values, dict):
            raise _ConfigError(path, "[pytest] in pytest.toml must be a table")
        return _ResolvedConfig(path, values, native_toml=True)

    tool = document.get("tool", {})
    if not isinstance(tool, dict):
        return None
    pytest_table = tool.get("pytest", {})
    if not isinstance(pytest_table, dict):
        raise _ConfigError(path, "[tool.pytest] in pyproject.toml must be a table")
    ini_values = pytest_table.get("ini_options")
    if ini_values is not None and not isinstance(ini_values, dict):
        raise _ConfigError(path, "[tool.pytest.ini_options] must be a table")

    if pytest_major >= 9:
        native_values = {key: value for key, value in pytest_table.items() if key != "ini_options"}
        if native_values and ini_values:
            raise _ConfigError(
                path,
                "pyproject.toml cannot combine [tool.pytest] options with "
                "[tool.pytest.ini_options]",
            )
        if native_values:
            return _ResolvedConfig(path, native_values, native_toml=True)
    if ini_values is not None:
        values = {
            key: value if isinstance(value, list) else str(value)
            for key, value in ini_values.items()
        }
        return _ResolvedConfig(path, values)
    return None


def _config_addopts_overrides(config: _ResolvedConfig | None) -> _ArgumentOverrides:
    if config is None or "addopts" not in config.values:
        return _ArgumentOverrides({}, "pytest config addopts", None)
    raw = config.values["addopts"]
    label = f"{config.path.name} addopts"
    if config.native_toml:
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            raise _ConfigError(
                config.path,
                f"{label} must be a list of strings in native TOML",
            )
        tokens = raw
    elif isinstance(raw, str):
        try:
            tokens = shlex.split(raw)
        except ValueError as error:
            raise _ConfigError(config.path, f"cannot parse {label}: {error}") from error
    elif isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        tokens = raw
    else:
        raise _ConfigError(config.path, f"{label} must be a string or list of strings")
    return _argument_overrides(tokens, label=label, path=config.path)


def _environment_overrides(raw: str, *, project_root: Path) -> _ArgumentOverrides:
    if not raw.strip():
        return _ArgumentOverrides({}, "PYTEST_ADDOPTS", None)
    try:
        tokens = shlex.split(raw)
    except ValueError as error:
        raise _ConfigError(
            None,
            f"cannot parse PYTEST_ADDOPTS while checking pytest-asyncio settings: {error}",
        ) from error

    return _argument_overrides(
        tokens,
        label="PYTEST_ADDOPTS",
        path=None,
        positional_root=project_root,
    )


def _argument_overrides(
    tokens: Sequence[str],
    *,
    label: str,
    path: Path | None,
    positional_root: Path | None = None,
) -> _ArgumentOverrides:

    overrides: dict[str, str] = {}
    debug_flag = False
    index = 0
    positional_only = False
    while index < len(tokens):
        token = tokens[index]
        if token == "--" and not positional_only:
            positional_only = True
            index += 1
            continue
        if positional_only:
            if positional_root is not None:
                _validate_positional_path(token, positional_root)
            index += 1
            continue
        if (
            token in {"-c", "--config-file"}
            or token.startswith("--config-file=")
            or (token.startswith("-c") and token != "-c")
        ):
            raise _ConfigError(
                path,
                f"{label} selects an explicit config with -c/--config-file; "
                "the effective pytest-asyncio settings cannot be proven statically",
            )
        if token == "--asyncio-debug" or token.startswith("--asyncio-debug="):
            debug_flag = True

        override: str | None = None
        if token in {"-o", "--override-ini"}:
            if index + 1 >= len(tokens):
                raise _ConfigError(
                    path,
                    f"{label} contains -o/--override-ini without option=value",
                )
            index += 1
            override = tokens[index]
        elif token.startswith("--override-ini="):
            override = token.partition("=")[2]
        elif token.startswith("-o") and token != "-o":
            override = token[2:].removeprefix("=")
        if override is not None:
            if "=" not in override:
                raise _ConfigError(
                    path,
                    f"{label} contains malformed -o/--override-ini; expected option=value",
                )
            key, value = override.split("=", 1)
            if key in {_ASYNCIO_SCOPE_OPTION, _ASYNCIO_DEBUG_OPTION}:
                overrides[key] = value
        elif token in _OPTIONS_WITH_SEPARATE_VALUE and index + 1 < len(tokens):
            index += 1
        elif not token.startswith("-") and positional_root is not None:
            _validate_positional_path(token, positional_root)
        index += 1
    return _ArgumentOverrides(
        overrides,
        label,
        path,
        asyncio_debug_flag=debug_flag,
    )


def _validate_positional_path(token: str, project_root: Path) -> None:
    path_text = token.split("::", 1)[0]
    if not path_text:
        return
    candidate = Path(path_text).expanduser()
    if not candidate.is_absolute():
        candidate = project_root / candidate
    absolute = Path(os.path.abspath(candidate))
    try:
        exists = absolute.exists()
    except OSError:
        return
    if not exists:
        return
    if absolute == project_root or project_root in absolute.parents:
        return
    raise _ConfigError(
        None,
        f"PYTEST_ADDOPTS positional path {token!r} is outside the project root and "
        "can change pytest configuration discovery",
    )


def _effective_option(
    name: str,
    *,
    default: object,
    config: _ResolvedConfig | None,
    config_overrides: _ArgumentOverrides,
    environment_overrides: _ArgumentOverrides,
) -> tuple[object, str, bool, Path | None]:
    if name == _ASYNCIO_DEBUG_OPTION:
        if environment_overrides.asyncio_debug_flag:
            return (
                True,
                f"{environment_overrides.label} --asyncio-debug",
                True,
                environment_overrides.path,
            )
        if config_overrides.asyncio_debug_flag:
            return (
                True,
                f"{config_overrides.label} --asyncio-debug",
                True,
                config_overrides.path,
            )
    if name in environment_overrides.values:
        return (
            environment_overrides.values[name],
            f"{environment_overrides.label} -o {name}",
            False,
            environment_overrides.path,
        )
    if name in config_overrides.values:
        return (
            config_overrides.values[name],
            f"{config_overrides.label} -o {name}",
            False,
            config_overrides.path,
        )
    if config is not None and name in config.values:
        return config.values[name], config.path.name, config.native_toml, config.path
    return default, "pytest-asyncio default", False, None


def _parse_loop_scope(
    value: object,
    *,
    native_toml: bool,
    error_path: Path | None,
) -> str:
    if not isinstance(value, str):
        mode = "native TOML" if native_toml else "pytest configuration"
        raise _ConfigError(
            error_path,
            f"{_ASYNCIO_SCOPE_OPTION} must be a string in {mode}",
        )
    if value not in _VALID_LOOP_SCOPES:
        raise _ConfigError(
            error_path,
            f"invalid {_ASYNCIO_SCOPE_OPTION}={value!r}; expected one of "
            + ", ".join(sorted(_VALID_LOOP_SCOPES)),
        )
    return value


def _parse_bool(
    value: object,
    *,
    native_toml: bool,
    error_path: Path | None,
) -> bool:
    if native_toml:
        if isinstance(value, bool):
            return value
        raise _ConfigError(
            error_path,
            f"{_ASYNCIO_DEBUG_OPTION} must be a boolean in native TOML",
        )
    normalized = str(value).strip().lower()
    if normalized in _FALSE_VALUES:
        return False
    if normalized in _TRUE_VALUES:
        return True
    raise _ConfigError(error_path, f"invalid {_ASYNCIO_DEBUG_OPTION} value {value!r}")


def _diagnostic_source(project_root: Path, path: Path | None) -> str:
    if path is None:
        return "<PYTEST_ADDOPTS>"
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return str(path)


def _origin_source(
    project_root: Path,
    path: Path | None,
    origin: str,
) -> str:
    if path is not None:
        return _diagnostic_source(project_root, path)
    if origin.startswith("PYTEST_ADDOPTS"):
        return "<PYTEST_ADDOPTS>"
    return "<pytest-asyncio-default>"


__all__ = ["pytest_asyncio_config_diagnostics"]
