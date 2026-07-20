"""Filesystem discovery for native Testenix tests."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import re
import sys
import threading
import traceback
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, overload

from testenix.api import (
    CASES_METADATA_ATTR,
    SKIP_REASON_ATTR,
    XFAIL_REASON_ATTR,
    CaseDefinition,
    TestMetadata,
    get_cases,
    get_fixture_metadata,
    get_test_metadata,
)
from testenix.contracts import CollectionIssue, TestSpec
from testenix.fixtures import FixtureDefinition, FixtureRegistrationError, FixtureRegistry

_IMPORT_LOCK = threading.RLock()

_IGNORED_DIRECTORIES = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
    "site-packages",
    "venv",
}


@dataclass(frozen=True, slots=True)
class CollectedTest:
    """A serializable spec paired with its in-process implementation."""

    spec: TestSpec
    function: Any
    registry: FixtureRegistry

    @property
    def id(self) -> str:
        return self.spec.id


@dataclass(frozen=True, slots=True)
class CollectionResult(Sequence[TestSpec]):
    """Complete collection output, including non-fatal authoring issues."""

    items: tuple[CollectedTest, ...]
    registry: FixtureRegistry
    issues: tuple[CollectionIssue, ...] = ()

    @property
    def tests(self) -> tuple[TestSpec, ...]:
        return tuple(item.spec for item in self.items)

    @property
    def fixtures(self) -> tuple[FixtureDefinition, ...]:
        return self.registry.definitions

    def by_id(self, test_id: str) -> CollectedTest:
        try:
            return next(item for item in self.items if item.spec.id == test_id)
        except StopIteration as error:
            raise KeyError(test_id) from error

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self) -> Iterator[TestSpec]:
        return iter(self.tests)

    @overload
    def __getitem__(self, index: int) -> TestSpec: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[TestSpec, ...]: ...

    def __getitem__(self, index: int | slice) -> TestSpec | tuple[TestSpec, ...]:
        return self.tests[index]


def _is_ignored(path: Path) -> bool:
    return any(part in _IGNORED_DIRECTORIES or part.startswith(".") for part in path.parts)


def _expand_paths(
    paths: str | Path | Iterable[str | Path],
) -> tuple[list[Path], list[CollectionIssue]]:
    supplied = (paths,) if isinstance(paths, (str, Path)) else tuple(paths)
    files: set[Path] = set()
    issues: list[CollectionIssue] = []
    for raw_path in supplied:
        path = Path(raw_path).expanduser()
        if not path.exists():
            issues.append(CollectionIssue(str(path), "collection path does not exist"))
            continue
        if path.is_file():
            if path.suffix == ".py":
                files.add(path.resolve())
            else:
                issues.append(CollectionIssue(str(path), "collection file is not a Python module"))
            continue
        for candidate in path.rglob("test_*.py"):
            try:
                relative = candidate.relative_to(path)
            except ValueError:
                relative = candidate
            if candidate.is_file() and not _is_ignored(relative):
                files.add(candidate.resolve())
    return sorted(files, key=lambda value: value.as_posix()), issues


def _module_identity(path: Path) -> tuple[str, Path]:
    package_parts: list[str] = []
    cursor = path.parent
    while (cursor / "__init__.py").is_file():
        package_parts.append(cursor.name)
        cursor = cursor.parent
    if package_parts:
        parts = [*reversed(package_parts), path.stem]
        return ".".join(parts), cursor
    digest = hashlib.sha1(str(path).encode(), usedforsecurity=False).hexdigest()[:12]
    safe_stem = re.sub(r"\W+", "_", path.stem)
    return f"_testenix_collected_{safe_stem}_{digest}", path.parent


@contextmanager
def _temporary_sys_path(path: Path) -> Iterator[None]:
    value = str(path)
    inserted = value not in sys.path
    if inserted:
        sys.path.insert(0, value)
    try:
        yield
    finally:
        if inserted:
            with suppress(ValueError):
                sys.path.remove(value)


def load_module(path: str | Path) -> ModuleType:
    """Load a test module from its exact path under a deterministic name."""

    file_path = Path(path).resolve()
    module_name, import_root = _module_identity(file_path)
    specification = importlib.util.spec_from_file_location(module_name, file_path)
    if specification is None or specification.loader is None:
        raise ImportError(f"cannot create an import specification for {file_path}")
    with _IMPORT_LOCK:
        module = importlib.util.module_from_spec(specification)
        previous = sys.modules.get(module_name)
        sys.modules[module_name] = module
        try:
            with _temporary_sys_path(import_root):
                specification.loader.exec_module(module)
        except BaseException:
            if previous is None:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = previous
            raise
    return module


def _defined_functions(module: ModuleType) -> list[tuple[str, Any]]:
    functions = [
        (name, value)
        for name, value in vars(module).items()
        if inspect.isfunction(value) and value.__module__ == module.__name__
    ]
    return sorted(
        functions,
        key=lambda item: (
            getattr(getattr(item[1], "__code__", None), "co_firstlineno", 0),
            item[0],
        ),
    )


def _fixture_bindings(namespace: Mapping[str, Any]) -> list[tuple[str, Any]]:
    fixtures: list[tuple[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for local_name, value in namespace.items():
        metadata = get_fixture_metadata(value) if inspect.isfunction(value) else None
        if metadata is None:
            continue
        effective_name = metadata.name or local_name
        identity = (id(value), effective_name)
        if identity not in seen:
            fixtures.append((local_name, value))
            seen.add(identity)
    return fixtures


def _visible_fixtures(module: ModuleType) -> list[tuple[str, Any]]:
    """Return fixture providers together with their local import names."""

    return _fixture_bindings(vars(module))


def _register_fixture_once(
    registry: FixtureRegistry,
    function: Any,
    *,
    local_name: str,
    module_name: str,
    path: Path,
) -> FixtureDefinition:
    metadata = get_fixture_metadata(function)
    assert metadata is not None
    effective_name = metadata.name or local_name
    existing = next(
        (
            definition
            for definition in registry.definitions
            if definition.module_name == module_name and definition.name == effective_name
        ),
        None,
    )
    if existing is not None:
        if existing.function is function:
            return existing
        raise FixtureRegistrationError(
            f"fixture {effective_name!r} is declared more than once in {module_name}"
        )
    return registry.register(
        function,
        module_name=module_name,
        path=path,
        local_name=local_name,
    )


def _fixture_dependencies(
    function: Any,
    *,
    module_name: str,
    path: Path,
) -> tuple[FixtureDefinition, ...]:
    """Resolve the decorated providers referenced by one shared fixture."""

    candidates = FixtureRegistry()
    # Functions retain the defining namespace even when imported under an alias.
    for local_name, candidate in _fixture_bindings(function.__globals__):
        _register_fixture_once(
            candidates,
            candidate,
            local_name=local_name,
            module_name=module_name,
            path=path,
        )

    dependencies: list[FixtureDefinition] = []
    for parameter in inspect.signature(function).parameters.values():
        if parameter.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        definition = candidates.find_for_parameter(
            parameter,
            module_name=module_name,
            function=function,
        )
        # Built-ins are runtime fallbacks rather than decorated source
        # functions, so there is no provider closure to register for them.
        if definition is not None and not definition.builtin:
            dependencies.append(definition)
    return tuple(dependencies)


def _register_fixture_closure(
    registry: FixtureRegistry,
    function: Any,
    *,
    local_name: str,
    module_name: str,
    path: Path,
    expanded: set[int],
) -> None:
    _register_fixture_once(
        registry,
        function,
        local_name=local_name,
        module_name=module_name,
        path=path,
    )
    identity = id(function)
    if identity in expanded:
        return
    expanded.add(identity)
    for dependency in _fixture_dependencies(
        function,
        module_name=module_name,
        path=path,
    ):
        _register_fixture_closure(
            registry,
            dependency.function,
            local_name=dependency.name,
            module_name=module_name,
            path=path,
            expanded=expanded,
        )


def _contract_path(path: Path) -> str:
    """Prefer checkout-stable POSIX paths while retaining outside-root safety."""

    resolved = path.resolve()
    try:
        return resolved.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _implicit_case_id(parameters: dict[str, Any], ordinal: int) -> str:
    # Parameter reprs are not identities: object addresses and unordered
    # containers can differ when a spawned worker imports the module again.
    # Declaration position is deterministic and keeps the manifest portable.
    return f"case-{ordinal}"


def _source_line(function: Any) -> int | None:
    try:
        source_function = inspect.unwrap(function)
    except ValueError:
        source_function = function
    code = getattr(source_function, "__code__", None)
    first_line = getattr(code, "co_firstlineno", None)
    if isinstance(first_line, int):
        return first_line
    try:
        return inspect.getsourcelines(source_function)[1]
    except (OSError, TypeError):
        return None


def _case_error(
    path: Path,
    function: Any,
    definition: CaseDefinition,
) -> str | None:
    signature = inspect.signature(function)
    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    unexpected = set(definition.parameters).difference(signature.parameters)
    if unexpected and not accepts_kwargs:
        names = ", ".join(sorted(unexpected))
        return f"case for {function.__name__} has unknown parameters: {names}"
    positional_only = [
        name
        for name in definition.parameters
        if name in signature.parameters
        and signature.parameters[name].kind is inspect.Parameter.POSITIONAL_ONLY
    ]
    if positional_only:
        names = ", ".join(positional_only)
        return f"case for {function.__name__} targets positional-only parameters: {names}"
    return None


def _materialise_specs(
    path: Path,
    module: ModuleType,
    function: Any,
    metadata: TestMetadata,
    *,
    contract_path: str,
) -> tuple[list[TestSpec], list[CollectionIssue]]:
    definitions = list(get_cases(function))
    concrete: list[CaseDefinition | None] = list(definitions)
    if not concrete:
        concrete.append(None)
    specs: list[TestSpec] = []
    issues: list[CollectionIssue] = []
    seen_ids: set[str] = set()
    for ordinal, definition in enumerate(concrete, 1):
        parameters = dict(definition.parameters) if definition is not None else {}
        case_id = None
        if definition is not None:
            error = _case_error(path, function, definition)
            if error is not None:
                issues.append(CollectionIssue(str(path), error))
                continue
            case_id = definition.id or _implicit_case_id(parameters, ordinal)
            if case_id in seen_ids:
                issues.append(
                    CollectionIssue(
                        str(path),
                        f"duplicate case id {case_id!r} for {function.__name__}",
                    )
                )
                continue
            seen_ids.add(case_id)

        base_id = f"{contract_path}::{function.__name__}"
        test_id = f"{base_id}[{case_id}]" if case_id is not None else base_id
        base_display = metadata.description or function.__name__
        display_name = f"{base_display} [{case_id}]" if case_id is not None else base_display
        specs.append(
            TestSpec(
                id=test_id,
                path=contract_path,
                module_name=module.__name__,
                function_name=function.__name__,
                display_name=display_name,
                parameters=parameters,
                case_id=case_id,
                tags=metadata.tags,
                skip_reason=getattr(function, SKIP_REASON_ATTR, None),
                xfail_reason=getattr(function, XFAIL_REASON_ATTR, None),
                timeout=metadata.timeout,
                source_line=_source_line(function),
            )
        )
    return specs, issues


def discover(
    paths: str | Path | Iterable[str | Path] = ".",
) -> CollectionResult:
    """Discover native tests and fixtures below one or more paths.

    Directories are searched recursively for ``test_*.py``.  An explicitly
    supplied Python file may have any name, which is useful for focused runs.
    Inside a module, ``test_*`` functions and functions decorated with
    ``@test`` (or parameterized with ``@case(s)``) are collected.
    """

    files, issues = _expand_paths(paths)
    registry = FixtureRegistry()
    modules: list[tuple[Path, ModuleType, list[tuple[str, Any]]]] = []
    for path in files:
        try:
            module = load_module(path)
        except Exception as error:
            issues.append(
                CollectionIssue(
                    path=str(path),
                    message=f"{type(error).__name__}: {error}",
                    traceback=traceback.format_exc(),
                )
            )
            continue
        functions = _defined_functions(module)
        modules.append((path, module, functions))
        expanded_fixtures: set[int] = set()
        for local_name, function in _visible_fixtures(module):
            try:
                _register_fixture_closure(
                    registry,
                    function,
                    local_name=local_name,
                    module_name=module.__name__,
                    path=path,
                    expanded=expanded_fixtures,
                )
            except FixtureRegistrationError as error:
                issues.append(CollectionIssue(str(path), str(error), traceback.format_exc()))

    collected: list[CollectedTest] = []
    for path, module, functions in modules:
        contract_path = _contract_path(path)
        for name, function in functions:
            if get_fixture_metadata(function) is not None:
                continue
            metadata = get_test_metadata(function)
            explicitly_parameterized = hasattr(function, CASES_METADATA_ATTR)
            if metadata is None and not name.startswith("test_") and not explicitly_parameterized:
                continue
            metadata = metadata or TestMetadata()
            specs, case_issues = _materialise_specs(
                path,
                module,
                function,
                metadata,
                contract_path=contract_path,
            )
            issues.extend(case_issues)
            collected.extend(CollectedTest(spec, function, registry) for spec in specs)

    collected.sort(
        key=lambda item: (
            item.spec.path,
            item.spec.source_line or 0,
            item.spec.function_name,
            item.spec.case_id or "",
        )
    )
    return CollectionResult(tuple(collected), registry, tuple(issues))


collect = discover


def discover_specs(
    paths: str | Path | Iterable[str | Path] = ".",
) -> tuple[TestSpec, ...]:
    """Convenience projection for consumers that only need the manifest."""

    return discover(paths).tests


__all__ = [
    "CollectedTest",
    "CollectionResult",
    "collect",
    "discover",
    "discover_specs",
    "load_module",
]
