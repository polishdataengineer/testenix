"""Conservative metadata for opt-in intra-module sharding.

The normal Testenix scheduler keeps every module in one process.  That remains
the default because module and session fixtures, import-time lifecycle hooks,
and mutable module globals can make test order and process affinity observable.

``ShardingPolicy(intra_module=True)`` is an explicit acknowledgement that
unobservable mutable state cannot be proven safe by static analysis.  The
analyser still fails closed for hazards Testenix can identify reliably:

* module- or session-scoped fixtures;
* fixtures imported from outside the collected module source;
* direct writes to module globals through ``global``;
* mutations of obvious nested module containers and mutable class state; and
* executable import-time control flow or lifecycle calls.

Function-scoped fixtures, including autouse fixtures, are safe to recreate in
each process and therefore do not prevent sharding.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import re
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from testenix.contracts import CollectionIssue, Scope, TestSpec
from testenix.discovery import CollectionResult, enumerate_test_files

COLLECTION_MANIFEST_FORMAT = "testenix.collection-manifest"
COLLECTION_MANIFEST_SCHEMA_VERSION = 1
REDACTED_PARAMETER_VALUE = "<redacted>"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class CollectionManifestError(ValueError):
    """A trusted collection manifest is malformed or unsafe to resolve."""


@dataclass(frozen=True, slots=True)
class SourceFingerprint:
    """SHA-256 identity of one project-relative collection source file."""

    path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class TrustedCollectionManifest:
    """Explicit portable collection result that may bypass collection imports.

    This is deliberately not an implicit cache.  A caller creates or loads the
    manifest and opts into trusting it for a run. Parameter names are retained
    for diagnostics, but every parameter value is replaced with the explicit
    ``<redacted>`` sentinel. Testenix still compares the complete test-file
    inventory plus local import dependencies and every SHA-256 digest before
    using it.
    Dynamic collection influenced by anything other than source bytes (for
    example environment variables) remains the manifest producer's trust
    decision.
    """

    collection_roots: tuple[str, ...]
    files: tuple[SourceFingerprint, ...]
    tests: tuple[TestSpec, ...]
    issues: tuple[CollectionIssue, ...] = ()
    sharding: tuple[ModuleShardingDecision, ...] = ()


@dataclass(frozen=True, slots=True)
class ShardingPolicy:
    """Core scheduling policy independent of CLI/configuration concerns.

    Intra-module sharding is deliberately opt-in.  Callers which do not pass a
    policy, or pass the default instance, retain the original module-affinity
    behaviour exactly.
    """

    intra_module: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.intra_module, bool):
            raise TypeError("intra_module must be a boolean")


@dataclass(frozen=True, slots=True)
class ModuleShardingDecision:
    """Portable result of analysing one collected module."""

    path: str
    module_name: str
    eligible: bool
    blockers: tuple[str, ...] = ()


_MUTABLE_FACTORIES = frozenset(
    {
        "ChainMap",
        "Counter",
        "OrderedDict",
        "defaultdict",
        "deque",
        "dict",
        "list",
        "set",
    }
)
_MUTATING_METHODS = frozenset(
    {
        "add",
        "append",
        "clear",
        "discard",
        "extend",
        "insert",
        "pop",
        "popitem",
        "remove",
        "reverse",
        "setdefault",
        "sort",
        "update",
    }
)
_SAFE_TESTENIX_DECORATOR_FACTORIES = frozenset(
    {
        "case",
        "cases",
        "fixture",
        "skip",
        "test",
        "xfail",
    }
)


def _qualified_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _qualified_name(node.value)
        return f"{owner}.{node.attr}" if owner is not None else None
    return None


def _testenix_decorator_names(tree: ast.Module) -> frozenset[str]:
    """Resolve only explicitly imported Testenix decorator factories.

    A bare name is not trusted merely because it happens to be called
    ``test`` or ``fixture``.  It must originate from Testenix, and any other
    top-level binding of that import name makes the analyser fail closed.
    """

    imported: set[str] = set()
    rebound: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom):
            for alias in statement.names:
                local_name = alias.asname or alias.name
                if (
                    statement.module in {"testenix", "testenix.api"}
                    and alias.name in _SAFE_TESTENIX_DECORATOR_FACTORIES
                ):
                    imported.add(alias.asname or alias.name)
                elif alias.name != "*":
                    rebound.add(local_name)
            continue

        if isinstance(statement, ast.Import):
            for alias in statement.names:
                local_name = alias.asname or alias.name.split(".", 1)[0]
                if alias.name in {"testenix", "testenix.api"}:
                    for factory in _SAFE_TESTENIX_DECORATOR_FACTORIES:
                        imported.add(f"{local_name}.{factory}")
                        if alias.name == "testenix" and alias.asname is None:
                            imported.add(f"{local_name}.api.{factory}")
                else:
                    rebound.add(local_name)
            continue

        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            rebound.add(statement.name)
        elif isinstance(statement, ast.Assign):
            for target in statement.targets:
                rebound.update(_assigned_names(target))
                root = _root_name(target)
                if root is not None:
                    rebound.add(root)
        elif isinstance(statement, (ast.AnnAssign, ast.AugAssign)):
            rebound.update(_assigned_names(statement.target))
            root = _root_name(statement.target)
            if root is not None:
                rebound.add(root)
        elif isinstance(statement, ast.Delete):
            for target in statement.targets:
                root = _root_name(target)
                if root is not None:
                    rebound.add(root)

    return frozenset(name for name in imported if name.split(".", 1)[0] not in rebound)


def _fixture_scope_blockers(
    statement: ast.stmt,
    *,
    safe_decorators: frozenset[str],
) -> tuple[str, ...]:
    """Require a local fixture's test scope to be evident from its syntax.

    Sharding also runs without a trusted manifest, and dynamic imports can sit
    outside the manifest's project-local dependency boundary. Trusting the
    runtime value of ``scope=IMPORTED`` would therefore make the safety proof
    depend on mutable external state. Only the decorator default and the
    literal string ``"test"`` are stable enough for the opt-in proof.
    """

    if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return ()
    fixture_names = frozenset(
        name for name in safe_decorators if name.rsplit(".", 1)[-1] == "fixture"
    )
    blockers: set[str] = set()
    for decorator in statement.decorator_list:
        decorated_by = decorator.func if isinstance(decorator, ast.Call) else decorator
        if _qualified_name(decorated_by) not in fixture_names:
            continue
        if not isinstance(decorator, ast.Call):
            # ``@fixture`` uses the API's immutable test-scope default.
            continue
        scope_keywords = tuple(keyword for keyword in decorator.keywords if keyword.arg == "scope")
        has_dynamic_keywords = any(keyword.arg is None for keyword in decorator.keywords)
        scope_is_literal_test = (
            len(scope_keywords) == 1
            and isinstance(scope_keywords[0].value, ast.Constant)
            and scope_keywords[0].value.value == Scope.TEST.value
        )
        uses_default_scope = not scope_keywords and not has_dynamic_keywords and not decorator.args
        if scope_is_literal_test and not has_dynamic_keywords and not decorator.args:
            continue
        if uses_default_scope:
            continue
        blockers.add(f"fixture {statement.name!r} does not have a statically guaranteed test scope")
    return tuple(sorted(blockers))


class _EagerCallVisitor(ast.NodeVisitor):
    """Find calls evaluated while an enclosing expression is constructed."""

    def __init__(self) -> None:
        self.calls: list[ast.Call] = []

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 - ast visitor API
        self.calls.append(node)
        self.generic_visit(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802 - ast visitor API
        # Creating a lambda evaluates its defaults, but not its body.
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)


def _eager_calls(node: ast.AST | None) -> tuple[ast.Call, ...]:
    if node is None:
        return ()
    visitor = _EagerCallVisitor()
    visitor.visit(node)
    return tuple(visitor.calls)


def _render_call(call: ast.Call) -> str:
    return _qualified_name(call.func) or "<dynamic>"


def _definition_time_call_blockers(
    statement: ast.stmt,
    *,
    evaluate_annotations: bool,
    safe_decorators: frozenset[str],
) -> tuple[str, ...]:
    blockers: set[str] = set()

    def block_calls(node: ast.AST | None, context: str) -> None:
        for call in _eager_calls(node):
            blockers.add(f"executes import-time {context} call {_render_call(call)}")

    def inspect_decorators(
        decorators: Sequence[ast.expr],
    ) -> None:
        for decorator in decorators:
            decorated_by = decorator.func if isinstance(decorator, ast.Call) else decorator
            decorator_name = _qualified_name(decorated_by)
            if decorator_name not in safe_decorators:
                blockers.add(f"executes import-time decorator call {decorator_name or '<dynamic>'}")
            for call in _eager_calls(decorator):
                name = _qualified_name(call.func)
                if name not in safe_decorators:
                    blockers.add(f"executes import-time decorator call {name or '<dynamic>'}")

    if isinstance(statement, ast.Assign):
        for target in statement.targets:
            block_calls(target, "assignment target")
        block_calls(statement.value, "assignment")
    elif isinstance(statement, ast.AnnAssign):
        block_calls(statement.target, "assignment target")
        block_calls(statement.value, "assignment")
        if evaluate_annotations:
            block_calls(statement.annotation, "annotation")
    elif isinstance(statement, ast.AugAssign):
        block_calls(statement.target, "assignment target")
        block_calls(statement.value, "assignment")
    elif isinstance(statement, ast.Assert):
        block_calls(statement.test, "assertion")
        block_calls(statement.msg, "assertion")
    elif isinstance(statement, ast.Delete):
        for target in statement.targets:
            block_calls(target, "delete target")
    elif isinstance(statement, ast.Expr):
        # The outer expression is handled by ``_import_time_blocker``. Walk it
        # as well so nested calls can never hide behind another lifecycle call.
        block_calls(statement.value, "expression")
    elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
        inspect_decorators(statement.decorator_list)
        for default in (*statement.args.defaults, *statement.args.kw_defaults):
            block_calls(default, "default")
        if evaluate_annotations:
            arguments = (
                *statement.args.posonlyargs,
                *statement.args.args,
                *statement.args.kwonlyargs,
            )
            for argument in arguments:
                block_calls(argument.annotation, "annotation")
            if statement.args.vararg is not None:
                block_calls(statement.args.vararg.annotation, "annotation")
            if statement.args.kwarg is not None:
                block_calls(statement.args.kwarg.annotation, "annotation")
            block_calls(statement.returns, "annotation")
    elif isinstance(statement, ast.ClassDef):
        inspect_decorators(statement.decorator_list)
        for base in statement.bases:
            block_calls(base, "class base")
        for keyword in statement.keywords:
            block_calls(keyword.value, "class declaration")
        # A class body runs while the module is imported.  Apply the same
        # checks to its assignments and method/nested-class definitions, but
        # never descend into ordinary function bodies.
        for child in statement.body:
            blocker = _import_time_blocker(child)
            if blocker is not None:
                blockers.add(blocker)
            blockers.update(
                _definition_time_call_blockers(
                    child,
                    evaluate_annotations=evaluate_annotations,
                    safe_decorators=safe_decorators,
                )
            )

    return tuple(sorted(blockers))


def _assigned_names(target: ast.AST) -> tuple[str, ...]:
    if isinstance(target, ast.Name):
        return (target.id,)
    if isinstance(target, (ast.Tuple, ast.List)):
        return tuple(name for item in target.elts for name in _assigned_names(item))
    return ()


class _MutableValueVisitor(ast.NodeVisitor):
    """Recognise mutable values retained by a module/class binding."""

    def __init__(self) -> None:
        self.found = False

    def visit_List(self, node: ast.List) -> None:  # noqa: N802 - ast visitor API
        self.found = True

    def visit_Dict(self, node: ast.Dict) -> None:  # noqa: N802 - ast visitor API
        self.found = True

    def visit_Set(self, node: ast.Set) -> None:  # noqa: N802 - ast visitor API
        self.found = True

    def visit_ListComp(self, node: ast.ListComp) -> None:  # noqa: N802 - ast visitor API
        self.found = True

    def visit_DictComp(self, node: ast.DictComp) -> None:  # noqa: N802 - ast visitor API
        self.found = True

    def visit_SetComp(self, node: ast.SetComp) -> None:  # noqa: N802 - ast visitor API
        self.found = True

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 - ast visitor API
        qualified = _qualified_name(node.func)
        if qualified is not None and qualified.rsplit(".", 1)[-1] in _MUTABLE_FACTORIES:
            self.found = True
            return
        self.generic_visit(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802 - ast visitor API
        # A lambda body is lazy, while its defaults are retained immediately and
        # can themselves become shared mutable state.
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)


def _is_mutable_value(node: ast.AST | None) -> bool:
    if node is None:
        return False
    visitor = _MutableValueVisitor()
    visitor.visit(node)
    return visitor.found


def _class_defines_mutable_state(statement: ast.ClassDef) -> bool:
    for child in statement.body:
        if isinstance(child, (ast.Assign, ast.AnnAssign)) and _is_mutable_value(child.value):
            return True
        if isinstance(child, ast.ClassDef) and _class_defines_mutable_state(child):
            return True
    return False


def _mutable_class_bindings(tree: ast.Module) -> frozenset[str]:
    """Return module class roots that retain obvious mutable class state."""

    class_names = {statement.name for statement in tree.body if isinstance(statement, ast.ClassDef)}
    names = {
        statement.name
        for statement in tree.body
        if isinstance(statement, ast.ClassDef) and _class_defines_mutable_state(statement)
    }
    for statement in tree.body:
        targets: Sequence[ast.expr]
        if isinstance(statement, ast.Assign) and _is_mutable_value(statement.value):
            targets = statement.targets
        elif isinstance(statement, ast.AnnAssign) and _is_mutable_value(statement.value):
            targets = (statement.target,)
        else:
            continue
        for target in targets:
            root = _root_name(target)
            if root in class_names and not isinstance(target, ast.Name):
                # Covers both ``State.values = []`` and a nested target such as
                # ``Outer.Inner.values = []`` without attempting alias analysis.
                names.add(root)
    return frozenset(names)


def _mutable_module_bindings(tree: ast.Module) -> frozenset[str]:
    names: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and _is_mutable_value(statement.value):
            for target in statement.targets:
                names.update(_assigned_names(target))
        elif isinstance(statement, ast.AnnAssign) and _is_mutable_value(statement.value):
            names.update(_assigned_names(statement.target))
    names.update(_mutable_class_bindings(tree))
    return frozenset(names)


def _root_name(node: ast.AST) -> str | None:
    current = node
    while isinstance(current, (ast.Attribute, ast.Subscript)):
        current = current.value
    return current.id if isinstance(current, ast.Name) else None


class _StateHazardVisitor(ast.NodeVisitor):
    def __init__(self, mutable_bindings: frozenset[str]) -> None:
        self.mutable_bindings = mutable_bindings
        self.blockers: set[str] = set()

    def visit_Global(self, node: ast.Global) -> None:  # noqa: N802 - ast visitor API
        names = ", ".join(sorted(node.names))
        self.blockers.add(f"writes module globals via global: {names}")

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 - ast visitor API
        if isinstance(node.func, ast.Attribute):
            root = _root_name(node.func.value)
            if root in self.mutable_bindings and node.func.attr in _MUTATING_METHODS:
                self.blockers.add(f"mutates module-level collection {root!r}")
        self.generic_visit(node)

    def _visit_write_target(self, target: ast.AST) -> None:
        root = _root_name(target)
        if root in self.mutable_bindings and not isinstance(target, ast.Name):
            self.blockers.add(f"mutates module-level collection {root!r}")

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802 - ast visitor API
        for target in node.targets:
            self._visit_write_target(target)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802 - ast visitor API
        self._visit_write_target(node.target)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:  # noqa: N802 - ast visitor API
        self._visit_write_target(node.target)
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:  # noqa: N802 - ast visitor API
        for target in node.targets:
            self._visit_write_target(target)
        self.generic_visit(node)


def _import_time_blocker(statement: ast.stmt) -> str | None:
    if isinstance(statement, ast.Expr):
        if isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
            return None
        if isinstance(statement.value, ast.Call):
            name = _qualified_name(statement.value.func)
            return f"executes import-time call {name or '<dynamic>'}"
        return "executes an import-time expression"
    if isinstance(
        statement,
        (
            ast.For,
            ast.AsyncFor,
            ast.While,
            ast.If,
            ast.With,
            ast.AsyncWith,
            ast.Try,
            ast.Match,
        ),
    ):
        return f"executes import-time {type(statement).__name__.lower()} control flow"
    return None


def _source_blockers(path: str) -> tuple[str, ...]:
    source_path = Path(path)
    try:
        tree = ast.parse(source_path.read_bytes(), filename=str(source_path))
    except (OSError, SyntaxError, UnicodeError) as error:
        return (f"cannot statically inspect source: {type(error).__name__}: {error}",)

    blockers: set[str] = set()
    evaluate_annotations = not any(
        isinstance(statement, ast.ImportFrom)
        and statement.module == "__future__"
        and any(alias.name == "annotations" for alias in statement.names)
        for statement in tree.body
    )
    safe_decorators = _testenix_decorator_names(tree)
    for statement in tree.body:
        blocker = _import_time_blocker(statement)
        if blocker is not None:
            blockers.add(blocker)
        blockers.update(
            _definition_time_call_blockers(
                statement,
                evaluate_annotations=evaluate_annotations,
                safe_decorators=safe_decorators,
            )
        )
        blockers.update(_fixture_scope_blockers(statement, safe_decorators=safe_decorators))

    mutable_class_bindings = _mutable_class_bindings(tree)
    blockers.update(f"defines mutable class state on {name!r}" for name in mutable_class_bindings)
    visitor = _StateHazardVisitor(_mutable_module_bindings(tree))
    # Module-level assignments establish state; only function/class bodies can
    # make that state order-dependent across tests.
    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            visitor.visit(statement)
    blockers.update(visitor.blockers)
    return tuple(sorted(blockers))


def assess_collection_sharding(
    collection: CollectionResult,
) -> tuple[ModuleShardingDecision, ...]:
    """Return deterministic, portable decisions for every collected module."""

    modules: dict[tuple[str, str], list[str]] = defaultdict(list)
    for item in collection.items:
        modules[(item.spec.path, item.spec.module_name)].append(item.spec.id)

    decisions: list[ModuleShardingDecision] = []
    definitions = collection.registry.definitions
    for (path, module_name), _test_ids in sorted(modules.items()):
        blockers = set(_source_blockers(path))
        module_source = Path(path).resolve()
        for definition in definitions:
            if definition.module_name not in (None, module_name):
                continue
            code = getattr(definition.function, "__code__", None)
            source_name = getattr(code, "co_filename", None)
            try:
                fixture_source = (
                    Path(source_name).resolve() if isinstance(source_name, str) else None
                )
            except (OSError, RuntimeError):
                fixture_source = None
            if not definition.builtin and fixture_source != module_source:
                # Keep the sharding proof local even though trusted manifests
                # also fingerprint discoverable project-local imports. A
                # provider can live outside that boundary or be resolved by
                # dynamic import machinery that static dependency discovery
                # cannot prove complete.
                blockers.add(
                    f"fixture {definition.name!r} is imported from outside "
                    "the collected module source"
                )
            if definition.scope in {Scope.MODULE, Scope.SESSION}:
                blockers.add(f"fixture {definition.name!r} has {definition.scope.value} scope")
        rendered = tuple(sorted(blockers))
        decisions.append(
            ModuleShardingDecision(
                path=path,
                module_name=module_name,
                eligible=not rendered,
                blockers=rendered,
            )
        )
    return tuple(decisions)


def _project_root(project_root: str | Path | None) -> Path:
    return Path.cwd().resolve() if project_root is None else Path(project_root).resolve()


def _safe_relative_path(value: object, *, allow_dot: bool = False) -> str:
    if not isinstance(value, str) or not value:
        raise CollectionManifestError("manifest paths must be non-empty strings")
    if value == "." and allow_dot:
        return value
    if "\\" in value:
        raise CollectionManifestError(f"manifest path is not portable: {value!r}")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    segments = value.split("/")
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or value != posix.as_posix()
        or any(segment in {"", ".", ".."} for segment in segments)
    ):
        raise CollectionManifestError(f"manifest path is not a safe relative path: {value!r}")
    return value


def _relative_to_root(path: str | Path, root: Path, *, allow_dot: bool = False) -> str:
    candidate = Path(path)
    resolved = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as error:
        raise CollectionManifestError(f"path escapes project root: {path!s}") from error
    rendered = relative.as_posix() or "."
    return _safe_relative_path(rendered, allow_dot=allow_dot)


def _hash_source(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _module_file_candidates(base: Path, module_name: str) -> tuple[Path, ...]:
    parts = tuple(part for part in module_name.split(".") if part)
    if not parts:
        return ()
    target = base.joinpath(*parts)
    candidates = [target.with_suffix(".py"), target / "__init__.py"]
    for index in range(1, len(parts)):
        candidates.append(base.joinpath(*parts[:index], "__init__.py"))
    return tuple(candidates)


def _local_import_candidates(source: Path, tree: ast.Module, root: Path) -> tuple[Path, ...]:
    search_roots = {source.parent, root}
    for raw_path in sys.path:
        try:
            candidate = Path(raw_path or ".").resolve()
            candidate.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            continue
        search_roots.add(candidate)

    candidates: set[Path] = set()
    # Imports inside a helper called by a decorator/case factory can execute
    # during collection just as top-level imports do. Walking the complete AST
    # is deliberately conservative: runtime-only local imports may cause extra
    # invalidation, but can never escape the manifest's dependency boundary.
    for statement in ast.walk(tree):
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                if alias.name == "testenix" or alias.name.startswith("testenix."):
                    continue
                for search_root in search_roots:
                    candidates.update(_module_file_candidates(search_root, alias.name))
            continue
        if not isinstance(statement, ast.ImportFrom) or statement.module == "__future__":
            continue
        module_name = statement.module or ""
        if statement.level == 0:
            if module_name == "testenix" or module_name.startswith("testenix."):
                continue
            bases = tuple(search_roots)
        else:
            relative_base = source.parent
            for _ in range(statement.level - 1):
                relative_base = relative_base.parent
            bases = (relative_base,)
        for base in bases:
            candidates.update(_module_file_candidates(base, module_name))
            for alias in statement.names:
                if alias.name != "*":
                    imported_name = ".".join(part for part in (module_name, alias.name) if part)
                    candidates.update(_module_file_candidates(base, imported_name))
    return tuple(candidates)


def _local_import_dependencies(files: Sequence[Path], root: Path) -> tuple[Path, ...]:
    """Find local Python imports whose bytes can influence collection metadata."""

    initial = {path.resolve() for path in files}
    dependencies: set[Path] = set()
    pending = list(initial)
    inspected: set[Path] = set()
    while pending:
        source = pending.pop()
        if source in inspected:
            continue
        inspected.add(source)
        try:
            tree = ast.parse(source.read_bytes(), filename=str(source))
        except (OSError, SyntaxError, UnicodeError):
            continue
        for candidate in _local_import_candidates(source, tree, root):
            try:
                resolved = candidate.resolve()
                relative = resolved.relative_to(root)
            except (OSError, RuntimeError, ValueError):
                continue
            if (
                not resolved.is_file()
                or resolved.suffix != ".py"
                or any(
                    part in {".venv", "venv", "site-packages", "__pycache__"}
                    for part in relative.parts
                )
            ):
                continue
            if resolved not in initial and resolved not in dependencies:
                dependencies.add(resolved)
                pending.append(resolved)
    return tuple(sorted(dependencies, key=lambda path: path.as_posix()))


def _redacted_parameters(parameters: Mapping[str, Any]) -> dict[str, str]:
    names = tuple(parameters)
    if any(not isinstance(name, str) or not name for name in names):
        raise CollectionManifestError("test parameter names must be non-empty strings")
    return {name: REDACTED_PARAMETER_VALUE for name in sorted(names)}


def _collection_inputs(
    paths: str | Path | Iterable[str | Path],
    root: Path,
) -> tuple[tuple[str, ...], tuple[Path, ...]]:
    supplied = (paths,) if isinstance(paths, (str, Path)) else tuple(paths)
    if not supplied:
        raise CollectionManifestError("collection_roots must not be empty")
    relative: list[str] = []
    resolved: list[Path] = []
    seen: set[str] = set()
    for path in supplied:
        rendered = _relative_to_root(path, root, allow_dot=True)
        if rendered in seen:
            raise CollectionManifestError(f"duplicate collection root: {rendered!r}")
        seen.add(rendered)
        relative.append(rendered)
        resolved.append(root if rendered == "." else root / rendered)
    return tuple(relative), tuple(resolved)


def _portable_spec(spec: TestSpec, root: Path) -> TestSpec:
    parameters = _redacted_parameters(spec.parameters)
    return TestSpec(
        id=spec.id,
        path=_relative_to_root(spec.path, root),
        module_name=spec.module_name,
        function_name=spec.function_name,
        display_name=spec.display_name,
        parameters=parameters,
        case_id=spec.case_id,
        tags=frozenset(spec.tags),
        skip_reason=spec.skip_reason,
        xfail_reason=spec.xfail_reason,
        timeout=spec.timeout,
        source_line=spec.source_line,
    )


def build_trusted_collection_manifest(
    paths: str | Path | Iterable[str | Path],
    collection: CollectionResult,
    *,
    project_root: str | Path | None = None,
) -> TrustedCollectionManifest:
    """Build an explicit manifest from a completed native collection.

    The returned value contains no executable objects.  It is suitable for
    deterministic JSON serialization and for a later opt-in ``run`` call.
    """

    root = _project_root(project_root)
    collection_roots, inputs = _collection_inputs(paths, root)
    files, enumeration_issues = enumerate_test_files(inputs)
    if enumeration_issues:
        details = "; ".join(issue.message for issue in enumeration_issues)
        raise CollectionManifestError(f"cannot fingerprint collection roots: {details}")
    dependency_files = _local_import_dependencies(files, root)
    fingerprint_files = tuple(sorted({*files, *dependency_files}, key=lambda path: path.as_posix()))
    fingerprints = tuple(
        SourceFingerprint(
            path=_relative_to_root(path, root),
            sha256=_hash_source(path),
        )
        for path in fingerprint_files
    )
    portable_tests = tuple(_portable_spec(item.spec, root) for item in collection.items)
    decisions = tuple(
        ModuleShardingDecision(
            path=_relative_to_root(decision.path, root),
            module_name=decision.module_name,
            eligible=decision.eligible,
            blockers=decision.blockers,
        )
        for decision in assess_collection_sharding(collection)
    )
    manifest = TrustedCollectionManifest(
        collection_roots=collection_roots,
        files=fingerprints,
        tests=portable_tests,
        issues=collection.issues,
        sharding=decisions,
    )
    # One validation path keeps hand-built and decoded manifests subject to the
    # same invariants as manifests produced by this helper.
    return trusted_collection_manifest_from_dict(trusted_collection_manifest_to_dict(manifest))


def trusted_collection_manifest_to_dict(
    manifest: TrustedCollectionManifest,
) -> dict[str, Any]:
    """Return the versioned JSON-compatible representation of a manifest."""

    return {
        "format": COLLECTION_MANIFEST_FORMAT,
        "schema_version": COLLECTION_MANIFEST_SCHEMA_VERSION,
        "collection_roots": list(manifest.collection_roots),
        "files": [
            {"path": fingerprint.path, "sha256": fingerprint.sha256}
            for fingerprint in manifest.files
        ],
        "tests": [
            {
                "id": spec.id,
                "path": spec.path,
                "module_name": spec.module_name,
                "function_name": spec.function_name,
                "display_name": spec.display_name,
                "parameters": _redacted_parameters(spec.parameters),
                "case_id": spec.case_id,
                "tags": sorted(spec.tags),
                "skip_reason": spec.skip_reason,
                "xfail_reason": spec.xfail_reason,
                "timeout": spec.timeout,
                "source_line": spec.source_line,
            }
            for spec in manifest.tests
        ],
        "issues": [
            {
                "path": issue.path,
                "message": issue.message,
                "traceback": issue.traceback,
            }
            for issue in manifest.issues
        ],
        "sharding": [
            {
                "path": decision.path,
                "module_name": decision.module_name,
                "eligible": decision.eligible,
                "blockers": list(decision.blockers),
            }
            for decision in manifest.sharding
        ],
    }


def validate_trusted_collection_manifest(
    manifest: TrustedCollectionManifest,
) -> TrustedCollectionManifest:
    """Return a canonical inert copy or raise ``CollectionManifestError``."""

    return trusted_collection_manifest_from_dict(trusted_collection_manifest_to_dict(manifest))


def serialize_trusted_collection_manifest(manifest: TrustedCollectionManifest) -> str:
    """Serialize a trusted collection manifest as deterministic JSON."""

    validated = validate_trusted_collection_manifest(manifest)
    return json.dumps(
        trusted_collection_manifest_to_dict(validated),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CollectionManifestError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _object(value: object, *, name: str, keys: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CollectionManifestError(f"{name} must be an object")
    actual = frozenset(value)
    if actual != keys:
        missing = sorted(keys - actual)
        unexpected = sorted(actual - keys)
        raise CollectionManifestError(
            f"{name} has invalid fields (missing={missing}, unexpected={unexpected})"
        )
    if any(not isinstance(key, str) for key in value):
        raise CollectionManifestError(f"{name} keys must be strings")
    return value


def _array(value: object, *, name: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise CollectionManifestError(f"{name} must be an array")
    return value


def _string(value: object, *, name: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        raise CollectionManifestError(f"{name} must be a non-empty string")
    return value


def _optional_timeout(value: object, *, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CollectionManifestError(f"{name} must be a positive finite number or null")
    rendered = float(value)
    if not math.isfinite(rendered) or rendered <= 0.0:
        raise CollectionManifestError(f"{name} must be a positive finite number or null")
    return rendered


def _optional_source_line(value: object, *, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CollectionManifestError(f"{name} must be a positive integer or null")
    return value


def _unique_strings(value: object, *, name: str) -> tuple[str, ...]:
    raw = _array(value, name=name)
    items: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        rendered = _string(item, name=f"{name}[{index}]")
        assert rendered is not None
        if rendered in seen:
            raise CollectionManifestError(f"{name} contains duplicate {rendered!r}")
        seen.add(rendered)
        items.append(rendered)
    return tuple(items)


_TOP_LEVEL_FIELDS = frozenset(
    {
        "format",
        "schema_version",
        "collection_roots",
        "files",
        "tests",
        "issues",
        "sharding",
    }
)
_FILE_FIELDS = frozenset({"path", "sha256"})
_TEST_FIELDS = frozenset(
    {
        "id",
        "path",
        "module_name",
        "function_name",
        "display_name",
        "parameters",
        "case_id",
        "tags",
        "skip_reason",
        "xfail_reason",
        "timeout",
        "source_line",
    }
)
_ISSUE_FIELDS = frozenset({"path", "message", "traceback"})
_SHARDING_FIELDS = frozenset({"path", "module_name", "eligible", "blockers"})


def trusted_collection_manifest_from_dict(data: Mapping[str, Any]) -> TrustedCollectionManifest:
    """Validate and reconstruct an inert trusted collection manifest."""

    top = _object(data, name="manifest", keys=_TOP_LEVEL_FIELDS)
    if top["format"] != COLLECTION_MANIFEST_FORMAT:
        raise CollectionManifestError("unsupported collection manifest format")
    version = top["schema_version"]
    if isinstance(version, bool) or version != COLLECTION_MANIFEST_SCHEMA_VERSION:
        raise CollectionManifestError(f"unsupported collection manifest schema: {version!r}")

    roots = tuple(
        _safe_relative_path(value, allow_dot=True)
        for value in _unique_strings(top["collection_roots"], name="collection_roots")
    )
    if not roots:
        raise CollectionManifestError("collection_roots must not be empty")

    files: list[SourceFingerprint] = []
    file_paths: set[str] = set()
    for index, raw in enumerate(_array(top["files"], name="files")):
        item = _object(raw, name=f"files[{index}]", keys=_FILE_FIELDS)
        path = _safe_relative_path(item["path"])
        digest = _string(item["sha256"], name=f"files[{index}].sha256")
        assert digest is not None
        if not _SHA256.fullmatch(digest):
            raise CollectionManifestError(f"files[{index}].sha256 is not a lowercase SHA-256")
        if path in file_paths:
            raise CollectionManifestError(f"duplicate source fingerprint path: {path!r}")
        file_paths.add(path)
        files.append(SourceFingerprint(path, digest))

    tests: list[TestSpec] = []
    test_ids: set[str] = set()
    for index, raw in enumerate(_array(top["tests"], name="tests")):
        item = _object(raw, name=f"tests[{index}]", keys=_TEST_FIELDS)
        test_id = _string(item["id"], name=f"tests[{index}].id")
        path = _safe_relative_path(item["path"])
        assert test_id is not None
        if test_id in test_ids:
            raise CollectionManifestError(f"duplicate test id: {test_id!r}")
        if path not in file_paths:
            raise CollectionManifestError(f"test path has no source fingerprint: {path!r}")
        parameters = item["parameters"]
        if not isinstance(parameters, Mapping) or any(
            not isinstance(key, str) for key in parameters
        ):
            raise CollectionManifestError(f"tests[{index}].parameters must be an object")
        if any(value != REDACTED_PARAMETER_VALUE for value in parameters.values()):
            raise CollectionManifestError(
                f"tests[{index}].parameters must contain only redacted values"
            )
        inert_parameters = _redacted_parameters(parameters)
        tags = _unique_strings(item["tags"], name=f"tests[{index}].tags")
        module_name = _string(item["module_name"], name=f"tests[{index}].module_name")
        function_name = _string(item["function_name"], name=f"tests[{index}].function_name")
        display_name = _string(item["display_name"], name=f"tests[{index}].display_name")
        assert module_name is not None and function_name is not None and display_name is not None
        tests.append(
            TestSpec(
                id=test_id,
                path=path,
                module_name=module_name,
                function_name=function_name,
                display_name=display_name,
                parameters=dict(inert_parameters),
                case_id=_string(item["case_id"], name=f"tests[{index}].case_id", optional=True),
                tags=frozenset(tags),
                skip_reason=_string(
                    item["skip_reason"], name=f"tests[{index}].skip_reason", optional=True
                ),
                xfail_reason=_string(
                    item["xfail_reason"], name=f"tests[{index}].xfail_reason", optional=True
                ),
                timeout=_optional_timeout(item["timeout"], name=f"tests[{index}].timeout"),
                source_line=_optional_source_line(
                    item["source_line"], name=f"tests[{index}].source_line"
                ),
            )
        )
        test_ids.add(test_id)

    issues: list[CollectionIssue] = []
    for index, raw in enumerate(_array(top["issues"], name="issues")):
        item = _object(raw, name=f"issues[{index}]", keys=_ISSUE_FIELDS)
        issue_path = _string(item["path"], name=f"issues[{index}].path")
        message = _string(item["message"], name=f"issues[{index}].message")
        assert issue_path is not None and message is not None
        issues.append(
            CollectionIssue(
                path=issue_path,
                message=message,
                traceback=_string(
                    item["traceback"], name=f"issues[{index}].traceback", optional=True
                ),
            )
        )

    sharding: list[ModuleShardingDecision] = []
    sharding_paths: set[str] = set()
    for index, raw in enumerate(_array(top["sharding"], name="sharding")):
        item = _object(raw, name=f"sharding[{index}]", keys=_SHARDING_FIELDS)
        path = _safe_relative_path(item["path"])
        if path not in file_paths:
            raise CollectionManifestError(f"sharding path has no source fingerprint: {path!r}")
        if path in sharding_paths:
            raise CollectionManifestError(f"duplicate sharding decision path: {path!r}")
        eligible = item["eligible"]
        if not isinstance(eligible, bool):
            raise CollectionManifestError(f"sharding[{index}].eligible must be a boolean")
        blockers = _unique_strings(item["blockers"], name=f"sharding[{index}].blockers")
        if eligible == bool(blockers):
            raise CollectionManifestError(
                f"sharding[{index}] eligibility is inconsistent with its blockers"
            )
        module_name = _string(item["module_name"], name=f"sharding[{index}].module_name")
        assert module_name is not None
        sharding.append(ModuleShardingDecision(path, module_name, eligible, blockers))
        sharding_paths.add(path)

    test_modules = {(spec.path, spec.module_name) for spec in tests}
    for decision in sharding:
        if (decision.path, decision.module_name) not in test_modules:
            raise CollectionManifestError(
                f"sharding decision does not identify a collected module: {decision.path!r}"
            )

    return TrustedCollectionManifest(
        collection_roots=roots,
        files=tuple(files),
        tests=tuple(tests),
        issues=tuple(issues),
        sharding=tuple(sharding),
    )


def deserialize_trusted_collection_manifest(
    data: str | bytes | bytearray | Mapping[str, Any],
) -> TrustedCollectionManifest:
    """Decode and validate trusted collection manifest JSON or mapping data."""

    if isinstance(data, Mapping):
        decoded = data
    else:
        try:
            decoded = json.loads(data, object_pairs_hook=_reject_duplicate_json_keys)
        except CollectionManifestError:
            raise
        except (TypeError, ValueError, UnicodeDecodeError) as error:
            raise CollectionManifestError("invalid collection manifest JSON") from error
    if not isinstance(decoded, Mapping):
        raise CollectionManifestError("manifest must be a JSON object")
    return trusted_collection_manifest_from_dict(decoded)


def verify_trusted_collection_manifest(
    manifest: TrustedCollectionManifest,
    paths: str | Path | Iterable[str | Path],
    *,
    project_root: str | Path | None = None,
) -> bool:
    """Return whether roots, inventory, and source hashes still match exactly.

    Any malformed value, missing/added file, unreadable source, or digest
    mismatch returns ``False``.  The runner can therefore fall back to its
    normal isolated collection process instead of turning staleness into a run
    failure.
    """

    try:
        validated = validate_trusted_collection_manifest(manifest)
        root = _project_root(project_root)
        collection_roots, inputs = _collection_inputs(paths, root)
        if collection_roots != validated.collection_roots:
            return False
        files, issues = enumerate_test_files(inputs)
        if issues:
            return False
        expected = {fingerprint.path: fingerprint.sha256 for fingerprint in validated.files}
        actual_test_paths = {_relative_to_root(path, root) for path in files}
        if not actual_test_paths.issubset(expected):
            return False
        actual: dict[str, str] = {}
        for relative_path in expected:
            source = (root / relative_path).resolve()
            if _relative_to_root(source, root) != relative_path:
                return False
            actual[relative_path] = _hash_source(source)
        return actual == expected
    except (CollectionManifestError, OSError, TypeError, ValueError):
        return False


__all__ = [
    "COLLECTION_MANIFEST_FORMAT",
    "COLLECTION_MANIFEST_SCHEMA_VERSION",
    "CollectionManifestError",
    "ModuleShardingDecision",
    "ShardingPolicy",
    "SourceFingerprint",
    "TrustedCollectionManifest",
    "assess_collection_sharding",
    "build_trusted_collection_manifest",
    "deserialize_trusted_collection_manifest",
    "serialize_trusted_collection_manifest",
    "trusted_collection_manifest_from_dict",
    "trusted_collection_manifest_to_dict",
    "validate_trusted_collection_manifest",
    "verify_trusted_collection_manifest",
]
