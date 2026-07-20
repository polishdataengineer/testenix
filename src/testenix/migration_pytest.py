"""Conservative, source-to-source migration from pytest to native Testenix.

The converter intentionally understands a small, statically provable subset of
pytest.  It never imports a source module and it never mutates the supplied
``SourceFile`` objects.  Unsupported semantics become stable, line-addressed
diagnostics instead of best-effort rewrites.

``ast.unparse`` is used deliberately: generated files are artifacts, not edits
to the user's originals.  The orchestration layer owns staging, differential
execution, and atomic publication of these artifacts.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeAlias

from testenix.migration_models import (
    ConversionBundle,
    DiagnosticSeverity,
    GeneratedArtifact,
    MigrationDiagnostic,
    SourceFile,
    TestMapping,
)

_Function: TypeAlias = ast.FunctionDef | ast.AsyncFunctionDef

_BUILTIN_FIXTURES = frozenset(
    {
        "cache",
        "capfd",
        "capfdbinary",
        "caplog",
        "capsys",
        "capsysbinary",
        "doctest_namespace",
        "event_loop_policy",
        "monkeypatch",
        "pytestconfig",
        "record_property",
        "record_testsuite_property",
        "record_xml_attribute",
        "recwarn",
        "request",
        "tmp_path",
        "tmp_path_factory",
        "tmpdir",
        "tmpdir_factory",
    }
)

_SUPPORTED_BUILTIN_FIXTURES = frozenset({"monkeypatch", "tmp_path"})

_SUPPORTED_MONKEYPATCH_METHODS = frozenset({"setattr", "setenv", "undo"})

_ALLOWED_RUNTIME_HELPERS = frozenset(
    {
        "pytest.approx",
        "pytest.deprecated_call",
        "pytest.fail",
        "pytest.raises",
        "pytest.warns",
    }
)

_OUTCOME_CALLS = {
    "pytest.exit": "PYT403_RUNTIME_EXIT",
    "pytest.importorskip": "PYT404_RUNTIME_IMPORTORSKIP",
    "pytest.skip": "PYT401_RUNTIME_SKIP",
    "pytest.xfail": "PYT402_RUNTIME_XFAIL",
}

_XUNIT_HOOKS = frozenset(
    {
        "setup_function",
        "setup_module",
        "teardown_function",
        "teardown_module",
    }
)

_SEMANTIC_MARKERS = frozenset(
    {
        "anyio",
        "asyncio",
        "dependency",
        "filterwarnings",
        "flaky",
        "order",
        "repeat",
        "timeout",
    }
)

_NATIVE_ALIASES = {
    "case": "_testenix_case",
    "cases": "_testenix_cases",
    "fixture": "_testenix_fixture",
    "skip": "_testenix_skip",
    "test": "_testenix_test",
}

_MIGRATION_RUNTIME_ALIASES = {
    "isolated_pytest_asyncio": "_testenix_isolated_asyncio",
}


@dataclass(frozen=True, slots=True)
class _Aliases:
    modules: dict[str, str]
    symbols: dict[str, str]

    @classmethod
    def from_tree(cls, tree: ast.Module) -> _Aliases:
        modules: dict[str, str] = {}
        symbols: dict[str, str] = {}
        for statement in tree.body:
            if isinstance(statement, ast.Import):
                for alias in statement.names:
                    if alias.name in {"pytest", "pytest_asyncio"}:
                        local_name = alias.asname or alias.name
                        modules[local_name] = alias.name
            elif isinstance(statement, ast.ImportFrom) and statement.module in {
                "pytest",
                "pytest_asyncio",
            }:
                for alias in statement.names:
                    if alias.name == "*":
                        continue
                    local_name = alias.asname or alias.name
                    symbols[local_name] = f"{statement.module}.{alias.name}"
        return cls(modules, symbols)

    def canonical(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return self.symbols.get(node.id) or self.modules.get(node.id)
        if isinstance(node, ast.Attribute):
            owner = self.canonical(node.value)
            return f"{owner}.{node.attr}" if owner is not None else None
        return None


@dataclass(slots=True)
class _Fixture:
    function_name: str
    effective_name: str
    node: _Function
    dependencies: tuple[str, ...]
    autouse: bool = False


@dataclass(slots=True)
class _Module:
    source: SourceFile
    tree: ast.Module | None
    aliases: _Aliases
    diagnostics: list[MigrationDiagnostic] = field(default_factory=list)
    fixtures: dict[str, _Fixture] = field(default_factory=dict)
    test_classes: list[ast.ClassDef] = field(default_factory=list)
    test_mappings: list[TestMapping] = field(default_factory=list)
    imports: set[str] = field(default_factory=set)
    migration_runtime_imports: set[str] = field(default_factory=set)
    uses_bare_asyncio: bool = False

    @property
    def source_name(self) -> str:
        return self.source.project_relative.as_posix()

    @property
    def blocked(self) -> bool:
        return any(
            diagnostic.severity is DiagnosticSeverity.ERROR for diagnostic in self.diagnostics
        )

    def error(self, code: str, message: str, node: ast.AST | None = None) -> None:
        self.diagnostics.append(
            MigrationDiagnostic(
                code=code,
                message=message,
                source=self.source_name,
                line=getattr(node, "lineno", None),
            )
        )

    def warning(self, code: str, message: str, node: ast.AST | None = None) -> None:
        self.diagnostics.append(
            MigrationDiagnostic(
                code=code,
                message=message,
                source=self.source_name,
                line=getattr(node, "lineno", None),
                severity=DiagnosticSeverity.WARNING,
            )
        )


@dataclass(frozen=True, slots=True)
class _ConvertedDecorator:
    node: ast.expr | None
    imports: frozenset[str] = frozenset()
    tags: frozenset[str] = frozenset()
    parameter_names: tuple[str, ...] = ()
    cases: tuple[tuple[str, str], ...] = ()


def detect_pytest_module(source: SourceFile) -> bool:
    """Return whether *source* looks like a pytest-authored test module.

    Plain module-level ``test*`` functions count as pytest-style tests even if
    they do not import pytest.  A syntax error is conservatively classified by
    its path/name and text; conversion will later return ``PYT001``.
    """

    try:
        tree = ast.parse(source.text, filename=str(source.path), type_comments=True)
    except SyntaxError:
        return source.project_relative.name.startswith("test_") or "pytest" in source.text

    aliases = _Aliases.from_tree(tree)
    if aliases.modules or aliases.symbols:
        return True
    for statement in tree.body:
        if isinstance(
            statement, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) and statement.name.startswith("test"):
            return True
        if isinstance(statement, ast.ClassDef) and _is_test_class(statement):
            return True
        if isinstance(statement, (ast.Assign, ast.AnnAssign)) and _assigned_name(statement) in {
            "pytest_plugins",
            "pytestmark",
        }:
            return True
    return False


def convert_pytest_suite(
    test_files: Sequence[SourceFile],
    conftest_files: Sequence[SourceFile] = (),
) -> ConversionBundle:
    """Convert the statically safe pytest subset into native Testenix artifacts.

    Files with a blocking diagnostic do not produce an artifact or mappings.
    Safe files may still be returned alongside blocked files so a caller can
    implement an explicit file-level partial migration.  The default
    orchestrator should treat ``blocking_diagnostics`` transactionally.
    """

    diagnostics: list[MigrationDiagnostic] = []
    artifacts: list[GeneratedArtifact] = []
    mappings: list[TestMapping] = []

    tests, duplicate_test_diagnostics = _unique_sources(test_files, "test")
    conftests, duplicate_conftest_diagnostics = _unique_sources(conftest_files, "conftest")
    diagnostics.extend(duplicate_test_diagnostics)
    diagnostics.extend(duplicate_conftest_diagnostics)

    conftest_modules = [_parse_module(source) for source in conftests]
    for module in conftest_modules:
        _inspect_module(module, is_conftest=True)
        if module.tree is not None:
            _convert_fixtures(module)
            _validate_runtime_pytest_calls(module)
            _validate_fixture_dependencies(module, available=set(module.fixtures))
            _diagnose_native_import_collisions(module)

    _diagnose_conftest_conflicts(conftest_modules)
    conftests_by_parent: dict[Path, _Module] = {}
    for module in conftest_modules:
        parent = module.source.project_relative.parent
        previous = conftests_by_parent.get(parent)
        if previous is not None:
            module.error(
                "PYT210_MULTIPLE_CONFTEST",
                f"more than one conftest source was supplied for {parent.as_posix()!r}",
            )
            previous.error(
                "PYT210_MULTIPLE_CONFTEST",
                f"more than one conftest source was supplied for {parent.as_posix()!r}",
            )
        else:
            conftests_by_parent[parent] = module

    helper_names: dict[Path, str] = {
        module.source.project_relative: _conftest_helper_name(module.source)
        for module in conftest_modules
    }

    test_modules = [_parse_module(source) for source in tests]
    target_counts = Counter(_target_relative_path(module.source) for module in test_modules)
    for module in test_modules:
        _inspect_module(module, is_conftest=False)
        if module.tree is None:
            continue
        target_path = _target_relative_path(module.source)
        if target_counts[target_path] > 1:
            module.error(
                "PYT007_TARGET_COLLISION",
                f"more than one source maps to generated path {target_path.as_posix()!r}",
            )
        _convert_fixtures(module)

        same_directory = conftests_by_parent.get(module.source.project_relative.parent)
        ancestors = _ancestor_conftests(module.source.project_relative, conftests_by_parent)
        visible = set(module.fixtures)
        if same_directory is not None:
            visible.update(same_directory.fixtures)
            _diagnose_fixture_override(module, same_directory, ancestors)

        _convert_tests(module, visible)
        _validate_runtime_pytest_calls(module)
        _validate_fixture_dependencies(module, available=visible)
        _diagnose_ancestor_fixture_use(module, same_directory, ancestors)

        if same_directory is not None and same_directory.blocked:
            module.error(
                "PYT211_BLOCKED_CONFTEST",
                f"the adjacent conftest {same_directory.source_name!r} is not safely convertible",
            )
        if same_directory is not None and not same_directory.blocked and same_directory.fixtures:
            fixture_functions = tuple(
                sorted(fixture.function_name for fixture in same_directory.fixtures.values())
            )
            collisions = _top_level_bound_names(module.tree).intersection(fixture_functions)
            if collisions:
                module.error(
                    "PYT209_CONFTEST_IMPORT_COLLISION",
                    "generated conftest import would overwrite module name(s): "
                    + ", ".join(sorted(collisions)),
                )
            else:
                helper_name = helper_names[same_directory.source.project_relative]
                _insert_import_from(module.tree, helper_name, fixture_functions)
        _diagnose_native_import_collisions(module)

    for module in conftest_modules:
        diagnostics.extend(module.diagnostics)
        if module.tree is None or module.blocked or not module.fixtures:
            continue
        _insert_testenix_import(module.tree, module.imports)
        _insert_migration_runtime_import(module.tree, module.migration_runtime_imports)
        _remove_unused_pytest_imports(module.tree)
        helper_path = module.source.migration_relative.with_name(
            f"{helper_names[module.source.project_relative]}.py"
        )
        content = _render(module.tree, module.source_name)
        if content is None:
            diagnostics.append(
                MigrationDiagnostic(
                    code="PYT002_GENERATED_SYNTAX",
                    message="generated conftest helper did not compile",
                    source=module.source_name,
                )
            )
            continue
        artifacts.append(
            GeneratedArtifact(
                relative_path=helper_path,
                content=content,
                source_files=(module.source_name,),
            )
        )

    for module in test_modules:
        diagnostics.extend(module.diagnostics)
        if module.tree is None or module.blocked:
            continue
        _insert_testenix_import(module.tree, module.imports)
        _insert_migration_runtime_import(module.tree, module.migration_runtime_imports)
        _remove_unused_pytest_imports(module.tree)
        content = _render(module.tree, module.source_name)
        if content is None:
            diagnostics.append(
                MigrationDiagnostic(
                    code="PYT002_GENERATED_SYNTAX",
                    message="generated test module did not compile",
                    source=module.source_name,
                )
            )
            continue
        artifacts.append(
            GeneratedArtifact(
                relative_path=_target_relative_path(module.source),
                content=content,
                source_files=(module.source_name,),
            )
        )
        mappings.extend(module.test_mappings)

    return ConversionBundle(
        artifacts=tuple(sorted(artifacts, key=lambda artifact: artifact.relative_path.as_posix())),
        mappings=tuple(
            sorted(
                mappings,
                key=lambda mapping: (
                    mapping.source_id,
                    mapping.target_file,
                    mapping.target_function,
                    mapping.case_id or "",
                ),
            )
        ),
        diagnostics=tuple(
            sorted(
                _deduplicate_diagnostics(diagnostics),
                key=lambda diagnostic: (
                    diagnostic.source,
                    diagnostic.line or 0,
                    diagnostic.code,
                    diagnostic.message,
                ),
            )
        ),
    )


def _unique_sources(
    sources: Sequence[SourceFile], kind: str
) -> tuple[tuple[SourceFile, ...], tuple[MigrationDiagnostic, ...]]:
    ordered = sorted(sources, key=lambda source: source.project_relative.as_posix())
    counts = Counter(source.project_relative for source in ordered)
    diagnostics = tuple(
        MigrationDiagnostic(
            code="PYT004_DUPLICATE_SOURCE",
            message=f"the same {kind} source was supplied more than once",
            source=path.as_posix(),
        )
        for path, count in sorted(counts.items(), key=lambda item: item[0].as_posix())
        if count > 1
    )
    unique: dict[Path, SourceFile] = {}
    for source in ordered:
        unique.setdefault(source.project_relative, source)
    return tuple(unique.values()), diagnostics


def _parse_module(source: SourceFile) -> _Module:
    try:
        tree = ast.parse(source.text, filename=str(source.path), type_comments=True)
    except SyntaxError as error:
        diagnostic = MigrationDiagnostic(
            code="PYT001_SYNTAX",
            message=error.msg,
            source=source.project_relative.as_posix(),
            line=error.lineno,
        )
        return _Module(source, None, _Aliases({}, {}), [diagnostic])
    return _Module(source, tree, _Aliases.from_tree(tree))


def _inspect_module(module: _Module, *, is_conftest: bool) -> None:
    assert module.tree is not None
    for statement in module.tree.body:
        if (
            isinstance(statement, ast.ImportFrom)
            and statement.module
            in {
                "pytest",
                "pytest_asyncio",
            }
            and any(alias.name == "*" for alias in statement.names)
        ):
            module.error(
                "PYT003_WILDCARD_IMPORT",
                "wildcard imports prevent safe pytest symbol resolution",
                statement,
            )
        assigned = _assigned_name(statement)
        if assigned == "pytest_plugins":
            module.error(
                "PYT501_PLUGIN_REGISTRATION",
                "pytest_plugins changes collection and cannot be migrated natively",
                statement,
            )
        elif assigned == "pytestmark":
            module.error(
                "PYT504_MODULE_MARK",
                "module-level pytestmark cannot be translated safely",
                statement,
            )

        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if statement.name.startswith("pytest_"):
                module.error(
                    "PYT502_PLUGIN_HOOK",
                    f"pytest hook {statement.name!r} has no native Testenix equivalent",
                    statement,
                )
            if statement.name in _XUNIT_HOOKS:
                module.error(
                    "PYT503_XUNIT_HOOK",
                    f"xunit lifecycle hook {statement.name!r} is not called by Testenix",
                    statement,
                )
            if is_conftest and statement.name.startswith("test"):
                module.error(
                    "PYT212_CONFTEST_TEST",
                    "test functions declared in conftest.py are not a safe migration unit",
                    statement,
                )
        elif isinstance(statement, ast.ClassDef) and _is_test_class(statement):
            if _inspect_test_class(module, statement):
                module.test_classes.append(statement)


def _inspect_test_class(module: _Module, class_node: ast.ClassDef) -> bool:
    """Accept only classes whose pytest lifecycle reduces to ``object()`` per test.

    Generated wrappers instantiate the retained class once per native test item.  Anything
    which can alter construction, inheritance, or pytest's class/method lifecycle is therefore
    rejected before a wrapper is emitted.
    """

    safe = True
    if class_node.bases or class_node.keywords:
        module.error(
            "PYT311_CLASS_INHERITANCE",
            f"pytest class {class_node.name!r} uses inheritance or a metaclass",
            class_node,
        )
        safe = False
    if class_node.decorator_list:
        module.error(
            "PYT312_CLASS_DECORATOR",
            f"pytest class {class_node.name!r} has a class decorator",
            class_node.decorator_list[0],
        )
        safe = False

    lifecycle_names = {
        "__init__",
        "__new__",
        "setup_class",
        "setup_method",
        "teardown_class",
        "teardown_method",
    }
    for member in class_node.body:
        bindings = _class_scope_bound_names(member)
        if "pytestmark" in bindings:
            module.error(
                "PYT313_CLASS_MARK",
                f"pytest class {class_node.name!r} declares class-level pytestmark",
                member,
            )
            safe = False
        lifecycle_bindings = sorted(bindings.intersection(lifecycle_names))
        if lifecycle_bindings:
            module.error(
                "PYT314_CLASS_LIFECYCLE",
                "pytest class lifecycle binding(s) are not supported: "
                + ", ".join(lifecycle_bindings),
                member,
            )
            safe = False
        if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        fixture_decorator = next(
            (
                decorator
                for decorator in member.decorator_list
                if _decorator_canonical(decorator, module.aliases)
                in {"pytest.fixture", "pytest_asyncio.fixture"}
            ),
            None,
        )
        if fixture_decorator is not None:
            module.error(
                "PYT315_CLASS_FIXTURE",
                f"fixture method {member.name!r} is not supported inside pytest classes",
                fixture_decorator,
            )
            safe = False
        if not member.name.startswith("test"):
            continue
        positional = (*member.args.posonlyargs, *member.args.args)
        if (
            member.args.posonlyargs
            or not positional
            or positional[0].arg != "self"
            or member.args.vararg is not None
            or member.args.kwarg is not None
            or bool(member.args.defaults)
            or any(default is not None for default in member.args.kw_defaults)
            or bool(getattr(member, "type_params", ()))
        ):
            module.error(
                "PYT316_CLASS_SIGNATURE",
                f"pytest method {class_node.name}.{member.name} needs a plain self signature "
                "without defaults",
                member,
            )
            safe = False
        semantic_decorator = next(
            (
                decorator
                for decorator in member.decorator_list
                if _decorator_canonical(decorator, module.aliases)
                in {"builtins.classmethod", "builtins.staticmethod", "classmethod", "staticmethod"}
                or isinstance(decorator, ast.Name)
                and decorator.id in {"classmethod", "staticmethod"}
            ),
            None,
        )
        if semantic_decorator is not None:
            module.error(
                "PYT316_CLASS_SIGNATURE",
                f"pytest method {class_node.name}.{member.name} cannot be static or class-bound",
                semantic_decorator,
            )
            safe = False
    return safe


def _convert_fixtures(module: _Module) -> None:
    assert module.tree is not None
    converted: list[tuple[_Function, _Fixture]] = []
    for statement in module.tree.body:
        if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        fixture_decorators = [
            decorator
            for decorator in statement.decorator_list
            if _decorator_canonical(decorator, module.aliases)
            in {"pytest.fixture", "pytest_asyncio.fixture"}
        ]
        if not fixture_decorators:
            continue
        if len(fixture_decorators) != 1:
            module.error(
                "PYT205_MULTIPLE_FIXTURE_DECORATORS",
                f"fixture {statement.name!r} has more than one fixture decorator",
                statement,
            )
            continue
        fixture_decorator = fixture_decorators[0]
        canonical = _decorator_canonical(fixture_decorator, module.aliases)
        if canonical == "pytest_asyncio.fixture" or isinstance(statement, ast.AsyncFunctionDef):
            module.error(
                "PYT502_ASYNC_PLUGIN",
                f"async fixture {statement.name!r} depends on pytest event-loop semantics",
                fixture_decorator,
            )
            continue

        other_decorators = [
            decorator
            for decorator in statement.decorator_list
            if decorator is not fixture_decorator
        ]
        if other_decorators:
            module.error(
                "PYT206_FIXTURE_DECORATOR",
                f"fixture {statement.name!r} has additional decorators",
                other_decorators[0],
            )
            continue

        replacement, effective_name, autouse = _convert_fixture_decorator(
            module, fixture_decorator, statement.name
        )
        if replacement is None or effective_name is None:
            continue
        statement.decorator_list = [replacement]
        module.imports.add("fixture")
        dependencies = _required_parameters(statement)
        fixture = _Fixture(
            statement.name,
            effective_name,
            statement,
            dependencies,
            autouse=autouse,
        )
        converted.append((statement, fixture))

    names = Counter(fixture.effective_name for _, fixture in converted)
    for statement, fixture in converted:
        if names[fixture.effective_name] > 1:
            module.error(
                "PYT207_FIXTURE_CONFLICT",
                f"fixture name {fixture.effective_name!r} is declared more than once",
                statement,
            )
        else:
            module.fixtures[fixture.effective_name] = fixture

    fixture_function_names = {fixture.function_name for fixture in module.fixtures.values()}
    for call_node in ast.walk(module.tree):
        if not isinstance(call_node, ast.Call) or not isinstance(call_node.func, ast.Name):
            continue
        if call_node.func.id not in fixture_function_names:
            continue
        if any(call_node is call for call in _decorator_calls(module.tree)):
            continue
        module.error(
            "PYT208_DIRECT_FIXTURE_CALL",
            f"fixture {call_node.func.id!r} is called directly",
            call_node,
        )


def _convert_fixture_decorator(
    module: _Module, decorator: ast.expr, function_name: str
) -> tuple[ast.expr | None, str | None, bool]:
    if not isinstance(decorator, ast.Call):
        return _native_name("fixture"), function_name, False
    if decorator.args:
        module.error(
            "PYT201_FIXTURE_ARGUMENTS",
            "pytest fixture decorator positional arguments are not supported",
            decorator,
        )
        return None, None, False

    keywords = _keyword_map(module, decorator, "fixture")
    if keywords is None:
        return None, None, False
    if "params" in keywords or "ids" in keywords:
        module.error(
            "PYT202_FIXTURE_PARAMS",
            f"parametrized fixture {function_name!r} has no native MVP equivalent",
            decorator,
        )
        return None, None, False

    autouse = False
    if "autouse" in keywords:
        if _is_true(keywords["autouse"]):
            autouse = True
        elif not _is_false(keywords["autouse"]):
            module.error(
                "PYT203_FIXTURE_AUTOUSE",
                f"autouse for fixture {function_name!r} must be the static boolean True or False",
                keywords["autouse"],
            )
            return None, None, False

    allowed = {"autouse", "name", "scope"}
    unknown = sorted(set(keywords) - allowed)
    if unknown:
        module.error(
            "PYT201_FIXTURE_ARGUMENTS",
            "unsupported fixture option(s): " + ", ".join(unknown),
            decorator,
        )
        return None, None, False

    effective_name = function_name
    output_keywords: list[ast.keyword] = []
    if autouse:
        output_keywords.append(ast.keyword(arg="autouse", value=ast.Constant(True)))
    if "name" in keywords:
        name = _literal_nonempty_string(keywords["name"])
        if name is None:
            module.error(
                "PYT201_FIXTURE_ARGUMENTS",
                "fixture name must be a static non-empty string",
                keywords["name"],
            )
            return None, None, False
        effective_name = name
        output_keywords.append(ast.keyword(arg="name", value=ast.Constant(name)))

    if "scope" in keywords:
        scope = _literal_nonempty_string(keywords["scope"])
        scopes = {"function": "test", "module": "module"}
        if scope not in scopes:
            detail = (
                "pytest session fixtures are run-global, while Testenix session fixtures are "
                "worker-local; automatic migration would change lifecycle semantics"
                if scope == "session"
                else "only static function and module fixture scopes are supported"
            )
            module.error(
                "PYT204_FIXTURE_SCOPE",
                detail,
                keywords["scope"],
            )
            return None, None, False
        output_keywords.append(ast.keyword(arg="scope", value=ast.Constant(scopes[scope])))

    if not output_keywords:
        return _native_name("fixture"), effective_name, False
    return (
        ast.Call(
            func=_native_name("fixture"),
            args=[],
            keywords=output_keywords,
        ),
        effective_name,
        autouse,
    )


def _convert_tests(module: _Module, visible_fixtures: set[str]) -> None:
    assert module.tree is not None
    fixture_functions = {fixture.function_name for fixture in module.fixtures.values()}
    for statement in tuple(module.tree.body):
        if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not statement.name.startswith("test") or statement.name in fixture_functions:
            continue
        asyncio_marker = _bare_asyncio_marker(statement, module.aliases)
        module.uses_bare_asyncio |= asyncio_marker is not None
        decorators, parameter_names, parameter_cases = _convert_test_decorators(module, statement)
        statement.decorator_list = decorators
        _validate_test_parameters(
            module,
            statement,
            parameter_names=parameter_names,
            visible_fixtures=visible_fixtures,
        )
        _validate_supported_builtin_usage(
            module,
            statement,
            parameter_names=parameter_names,
            visible_fixtures=visible_fixtures,
        )
        _validate_asyncio_event_loop_policy(
            module,
            visible_fixtures=visible_fixtures,
            marker=asyncio_marker,
        )
        _append_test_mappings(
            module,
            source_qualname=statement.name,
            target_function=statement.name,
            parameter_cases=parameter_cases,
        )

    generated_wrappers: list[_Function] = []
    bound_names = _top_level_bound_names(module.tree)
    for class_node in module.test_classes:
        for member in class_node.body:
            if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not member.name.startswith("test"):
                continue
            asyncio_marker = _bare_asyncio_marker(member, module.aliases)
            module.uses_bare_asyncio |= asyncio_marker is not None
            decorators, parameter_names, parameter_cases = _convert_test_decorators(module, member)
            # The retained class is an implementation detail. Native discovery sees only the
            # generated module-level wrapper, so foreign runner decorators must not execute on
            # the original method when the generated module is imported.
            member.decorator_list = []
            _validate_test_parameters(
                module,
                member,
                parameter_names=parameter_names,
                visible_fixtures=visible_fixtures,
                ignored_parameters=frozenset({"self"}),
            )
            _validate_supported_builtin_usage(
                module,
                member,
                parameter_names=parameter_names,
                visible_fixtures=visible_fixtures,
                ignored_parameters=frozenset({"self"}),
            )
            _validate_asyncio_event_loop_policy(
                module,
                visible_fixtures=visible_fixtures,
                marker=asyncio_marker,
            )
            source_qualname = f"{class_node.name}.{member.name}"
            wrapper_name = _class_wrapper_name(module, class_node.name, member.name)
            if wrapper_name in bound_names:
                module.error(
                    "PYT317_CLASS_WRAPPER_COLLISION",
                    f"generated wrapper name {wrapper_name!r} already exists in the module",
                    member,
                )
                continue
            bound_names.add(wrapper_name)
            wrapper = _class_test_wrapper(
                class_node,
                member,
                wrapper_name=wrapper_name,
                decorators=decorators,
            )
            generated_wrappers.append(wrapper)
            _append_test_mappings(
                module,
                source_qualname=source_qualname,
                target_function=wrapper_name,
                parameter_cases=parameter_cases,
            )
    module.tree.body.extend(generated_wrappers)


def _bare_asyncio_marker(function: _Function, aliases: _Aliases) -> ast.expr | None:
    return next(
        (
            decorator
            for decorator in function.decorator_list
            if not isinstance(decorator, ast.Call)
            and _decorator_canonical(decorator, aliases) == "pytest.mark.asyncio"
        ),
        None,
    )


def _validate_asyncio_event_loop_policy(
    module: _Module,
    *,
    visible_fixtures: set[str],
    marker: ast.expr | None,
) -> None:
    if marker is None or "event_loop_policy" not in visible_fixtures:
        return
    module.error(
        "PYT509_EVENT_LOOP_POLICY",
        (
            "a custom event_loop_policy fixture implicitly changes pytest-asyncio loop "
            "creation and cannot be reproduced by native migration"
        ),
        marker,
    )


def _convert_test_decorators(
    module: _Module,
    function: _Function,
) -> tuple[list[ast.expr], tuple[str, ...], tuple[tuple[str, str], ...]]:
    decorators: list[ast.expr] = []
    tags: set[str] = set()
    parameter_names: tuple[str, ...] = ()
    parameter_cases: tuple[tuple[str, str], ...] = ()
    parametrize_count = 0
    asyncio_count = 0
    async_plugin_count = 0

    for decorator in function.decorator_list:
        canonical = _decorator_canonical(decorator, module.aliases)
        converted = _convert_test_decorator(module, function, decorator)
        if canonical == "pytest.mark.parametrize":
            parametrize_count += 1
        elif canonical == "pytest.mark.asyncio":
            asyncio_count += 1
            async_plugin_count += 1
        elif canonical == "pytest.mark.anyio":
            async_plugin_count += 1
        if converted is None:
            continue
        if converted.node is not None:
            decorators.append(converted.node)
        module.imports.update(converted.imports)
        tags.update(converted.tags)
        if converted.parameter_names:
            parameter_names = converted.parameter_names
            parameter_cases = converted.cases

    if parametrize_count > 1:
        module.error(
            "PYT104_STACKED_PARAMETRIZE",
            f"test {function.name!r} has stacked parametrize decorators",
            function,
        )
    if asyncio_count > 1:
        module.error(
            "PYT507_DUPLICATE_ASYNCIO_MARKER",
            f"test {function.name!r} has more than one asyncio marker",
            function,
        )
    if isinstance(function, ast.AsyncFunctionDef) and async_plugin_count == 0:
        module.error(
            "PYT508_UNMARKED_ASYNC_TEST",
            (
                f"unmarked async test {function.name!r} depends on pytest asyncio_mode; "
                "add a bare @pytest.mark.asyncio before migrating"
            ),
            function,
        )

    if tags:
        decorators.insert(
            0,
            ast.Call(
                func=_native_name("test"),
                args=[],
                keywords=[
                    ast.keyword(
                        arg="tags",
                        value=ast.Set(elts=[ast.Constant(tag) for tag in sorted(tags)]),
                    )
                ],
            ),
        )
        module.imports.add("test")
    elif not function.name.startswith("test_"):
        decorators.insert(0, _native_name("test"))
        module.imports.add("test")
    return decorators, parameter_names, parameter_cases


def _validate_test_parameters(
    module: _Module,
    function: _Function,
    *,
    parameter_names: tuple[str, ...],
    visible_fixtures: set[str],
    ignored_parameters: frozenset[str] = frozenset(),
) -> None:
    required = set(_required_parameters(function)) - set(parameter_names) - ignored_parameters
    for parameter in sorted(required):
        if parameter in visible_fixtures or parameter in _SUPPORTED_BUILTIN_FIXTURES:
            continue
        if parameter in _BUILTIN_FIXTURES:
            module.error(
                "PYT209_BUILTIN_FIXTURE",
                f"pytest built-in fixture {parameter!r} has no native Testenix equivalent",
                function,
            )
        else:
            module.error(
                "PYT210_UNKNOWN_FIXTURE",
                f"required parameter {parameter!r} is not a statically known fixture or case",
                function,
            )


def _validate_supported_builtin_usage(
    module: _Module,
    function: _Function,
    *,
    parameter_names: tuple[str, ...] = (),
    visible_fixtures: set[str],
    ignored_parameters: frozenset[str] = frozenset(),
) -> None:
    """Reject uses which escape the deliberately small native built-in contract.

    A fixture declared by the project under the name ``monkeypatch`` is ordinary user code and
    must not be constrained here.  The validation applies only when the parameter resolves to
    Testenix's built-in compatibility fixture.
    """

    required = set(_required_parameters(function)) - set(parameter_names) - ignored_parameters
    if "monkeypatch" not in required or "monkeypatch" in visible_fixtures:
        return
    _validate_builtin_monkeypatch_usage(module, function)


def _validate_builtin_monkeypatch_usage(module: _Module, function: _Function) -> None:
    _validate_monkeypatch_binding(
        module,
        function,
        parameter="monkeypatch",
        helpers=_static_module_helpers(module),
        visiting=set(),
        validated=set(),
    )


def _validate_monkeypatch_binding(
    module: _Module,
    function: _Function,
    *,
    parameter: str,
    helpers: dict[str, _Function],
    visiting: set[tuple[int, str]],
    validated: set[tuple[int, str]],
) -> None:
    key = (id(function), parameter)
    if key in validated or key in visiting:
        return
    visiting.add(key)

    parents: dict[int, ast.AST] = {}
    body_nodes: list[ast.AST] = []
    for statement in function.body:
        walked = tuple(ast.walk(statement))
        body_nodes.extend(walked)
        for ancestor in walked:
            for child in ast.iter_child_nodes(ancestor):
                parents[id(child)] = ancestor

    for node in body_nodes:
        if not isinstance(node, ast.Name) or node.id != parameter:
            continue
        parent = parents.get(id(node))
        attribute = parent if isinstance(parent, ast.Attribute) and parent.value is node else None
        call = None if attribute is None else parents.get(id(attribute))
        if (
            attribute is not None
            and isinstance(call, ast.Call)
            and call.func is attribute
            and attribute.attr in _SUPPORTED_MONKEYPATCH_METHODS
            and isinstance(node.ctx, ast.Load)
        ):
            continue

        forwarded = _forwarded_monkeypatch_parameter(
            function,
            node,
            parent=parent,
            parents=parents,
            helpers=helpers,
        )
        if forwarded is not None and isinstance(node.ctx, ast.Load):
            helper, helper_parameter = forwarded
            _validate_monkeypatch_binding(
                module,
                helper,
                parameter=helper_parameter,
                helpers=helpers,
                visiting=visiting,
                validated=validated,
            )
            continue

        if attribute is not None and isinstance(call, ast.Call) and call.func is attribute:
            detail = f"method {attribute.attr!r} is not supported"
        else:
            detail = "the fixture object is read, rebound, passed, or aliased"
        allowed = ", ".join(f"{name}()" for name in sorted(_SUPPORTED_MONKEYPATCH_METHODS))
        module.error(
            "PYT214_MONKEYPATCH_USAGE",
            f"built-in monkeypatch {detail}; only direct calls to {allowed} are migratable",
            attribute or node,
        )

    visiting.remove(key)
    validated.add(key)


def _static_module_helpers(module: _Module) -> dict[str, _Function]:
    if module.tree is None:
        return {}
    if any(
        isinstance(statement, ast.ImportFrom)
        and any(alias.name == "*" for alias in statement.names)
        for statement in module.tree.body
    ):
        return {}

    binding_counts: Counter[str] = Counter()
    for statement in module.tree.body:
        binding_counts.update(_class_scope_bound_names(statement))
    return {
        statement.name: statement
        for statement in module.tree.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not statement.decorator_list
        and binding_counts[statement.name] == 1
    }


def _forwarded_monkeypatch_parameter(
    caller: _Function,
    argument: ast.Name,
    *,
    parent: ast.AST | None,
    parents: dict[int, ast.AST],
    helpers: dict[str, _Function],
) -> tuple[_Function, str] | None:
    call: ast.Call | None
    if isinstance(parent, ast.Call) and any(item is argument for item in parent.args):
        call = parent
    elif isinstance(parent, ast.keyword) and parent.value is argument:
        possible_call = parents.get(id(parent))
        call = possible_call if isinstance(possible_call, ast.Call) else None
    else:
        return None
    if call is None or not isinstance(call.func, ast.Name):
        return None
    if call.func.id in _function_local_bound_names(caller):
        return None
    helper = helpers.get(call.func.id)
    if helper is None:
        return None
    target = _call_argument_parameter(call, argument, parent=parent, helper=helper)
    return None if target is None else (helper, target)


def _call_argument_parameter(
    call: ast.Call,
    argument: ast.Name,
    *,
    parent: ast.AST | None,
    helper: _Function,
) -> str | None:
    positional_parameters = (*helper.args.posonlyargs, *helper.args.args)
    if parent is call:
        position = next(
            (index for index, value in enumerate(call.args) if value is argument),
            None,
        )
        if position is None or any(
            isinstance(value, ast.Starred) for value in call.args[: position + 1]
        ):
            return None
        if position >= len(positional_parameters):
            return None
        return positional_parameters[position].arg

    if not isinstance(parent, ast.keyword) or parent.arg is None:
        return None
    keyword_parameters = {
        argument_node.arg for argument_node in (*helper.args.args, *helper.args.kwonlyargs)
    }
    return parent.arg if parent.arg in keyword_parameters else None


def _function_local_bound_names(function: _Function) -> set[str]:
    names = set(_all_parameter_names(function))
    if function.args.vararg is not None:
        names.add(function.args.vararg.arg)
    if function.args.kwarg is not None:
        names.add(function.args.kwarg.arg)
    for statement in function.body:
        names.update(_class_scope_bound_names(statement))
    return names


def _append_test_mappings(
    module: _Module,
    *,
    source_qualname: str,
    target_function: str,
    parameter_cases: tuple[tuple[str, str], ...],
) -> None:
    target_file = _target_relative_path(module.source).as_posix()
    source_base = f"{module.source_name}::{source_qualname}"
    if parameter_cases:
        module.test_mappings.extend(
            TestMapping(
                source_id=f"{source_base}[{source_case}]",
                target_file=target_file,
                target_function=target_function,
                case_id=target_case,
            )
            for source_case, target_case in parameter_cases
        )
    else:
        module.test_mappings.append(
            TestMapping(
                source_id=source_base,
                target_file=target_file,
                target_function=target_function,
            )
        )


def _class_wrapper_name(module: _Module, class_name: str, method_name: str) -> str:
    source_id = f"{module.source_name}::{class_name}.{method_name}"
    readable = re.sub(r"[^0-9A-Za-z_]", "_", f"test_{class_name}__{method_name}")[:100]
    digest = hashlib.sha256(source_id.encode()).hexdigest()[:10]
    return f"{readable}__{digest}"


def _class_test_wrapper(
    class_node: ast.ClassDef,
    method: _Function,
    *,
    wrapper_name: str,
    decorators: list[ast.expr],
) -> _Function:
    arguments = copy.deepcopy(method.args)
    # _inspect_test_class guarantees a conventional first ``self`` argument.
    arguments.args = arguments.args[1:]
    arguments.defaults = []
    arguments.kw_defaults = [None for _ in arguments.kwonlyargs]
    for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs):
        argument.annotation = None
        argument.type_comment = None
    forwarded = [
        ast.keyword(arg=argument.arg, value=ast.Name(id=argument.arg, ctx=ast.Load()))
        for argument in (*arguments.args, *arguments.kwonlyargs)
    ]
    method_call = ast.Call(
        func=ast.Attribute(
            value=ast.Call(
                func=ast.Name(id=class_node.name, ctx=ast.Load()),
                args=[],
                keywords=[],
            ),
            attr=method.name,
            ctx=ast.Load(),
        ),
        args=[],
        keywords=forwarded,
    )
    is_async = isinstance(method, ast.AsyncFunctionDef)
    value: ast.expr = ast.Await(method_call) if is_async else method_call
    wrapper_type = ast.AsyncFunctionDef if is_async else ast.FunctionDef
    wrapper = wrapper_type(
        name=wrapper_name,
        args=arguments,
        body=[ast.Return(value=value)],
        decorator_list=decorators,
        returns=None,
        type_comment=None,
    )
    if "type_params" in wrapper._fields:
        # Python 3.12+ added this field. Generic pytest methods are rejected above, so the
        # generated wrapper intentionally has no PEP 695 parameters of its own.
        wrapper.type_params = []  # type: ignore[union-attr]
    return ast.copy_location(wrapper, method)


def _convert_test_decorator(
    module: _Module, function: _Function, decorator: ast.expr
) -> _ConvertedDecorator | None:
    canonical = _decorator_canonical(decorator, module.aliases)
    if canonical is None:
        module.error(
            "PYT105_UNSUPPORTED_DECORATOR",
            f"test {function.name!r} has a decorator with unknown runner semantics",
            decorator,
        )
        return None
    if canonical == "pytest.mark.parametrize":
        return _convert_parametrize(module, function, decorator)
    if canonical == "pytest.mark.skip":
        return _convert_skip(module, decorator)
    if canonical == "pytest.mark.skipif":
        return _convert_skipif(module, decorator)
    if canonical == "pytest.mark.xfail":
        module.error(
            "PYT301_XFAIL_SEMANTICS",
            "pytest xfail/XPASS and fixture-setup semantics differ from native Testenix",
            decorator,
        )
        return None
    if canonical == "pytest.mark.usefixtures":
        module.error(
            "PYT302_USEFIXTURES",
            "usefixtures has no implicit native Testenix equivalent",
            decorator,
        )
        return None
    if canonical == "pytest.mark.asyncio":
        if not isinstance(function, ast.AsyncFunctionDef):
            module.error(
                "PYT502_ASYNC_PLUGIN",
                "bare pytest.mark.asyncio is supported only on async def tests",
                decorator,
            )
            return None
        if isinstance(decorator, ast.Call):
            module.error(
                "PYT502_ASYNC_PLUGIN",
                "called or configured pytest.mark.asyncio may change event-loop semantics",
                decorator,
            )
            return None
        module.migration_runtime_imports.add("isolated_pytest_asyncio")
        return _ConvertedDecorator(_migration_runtime_name("isolated_pytest_asyncio"))
    if canonical == "pytest.mark.anyio":
        module.error(
            "PYT502_ASYNC_PLUGIN",
            "pytest anyio backend and lifecycle semantics cannot be translated safely",
            decorator,
        )
        return None
    if canonical.startswith("pytest.mark."):
        marker = canonical.removeprefix("pytest.mark.")
        if marker in _SEMANTIC_MARKERS:
            module.error(
                "PYT603_SEMANTIC_MARKER",
                f"marker {marker!r} changes runtime semantics and cannot be treated as a tag",
                decorator,
            )
            return None
        if isinstance(decorator, ast.Call) and (decorator.args or decorator.keywords):
            module.error(
                "PYT602_MARKER_ARGUMENTS",
                f"marker {marker!r} has arguments and is not a plain selection tag",
                decorator,
            )
            return None
        module.warning(
            "PYT601_MARKER_AS_TAG",
            f"plain pytest marker {marker!r} was converted to a Testenix tag",
            decorator,
        )
        return _ConvertedDecorator(None, tags=frozenset({marker}))

    module.error(
        "PYT105_UNSUPPORTED_DECORATOR",
        f"decorator {canonical!r} is not supported by the pytest migrator",
        decorator,
    )
    return None


def _convert_skip(module: _Module, decorator: ast.expr) -> _ConvertedDecorator:
    if not isinstance(decorator, ast.Call):
        return _ConvertedDecorator(_native_name("skip"), imports=frozenset({"skip"}))
    if decorator.args:
        module.error(
            "PYT303_SKIP_ARGUMENTS",
            "pytest mark.skip accepts only a static reason in this migrator",
            decorator,
        )
        return _ConvertedDecorator(None)
    keywords = _keyword_map(module, decorator, "skip")
    reason = None if keywords is None else keywords.get("reason")
    if keywords is None or set(keywords) != {"reason"} or _literal_nonempty_string(reason) is None:
        module.error(
            "PYT303_SKIP_ARGUMENTS",
            "pytest mark.skip reason must be a static non-empty string",
            decorator,
        )
        return _ConvertedDecorator(None)
    return _ConvertedDecorator(
        ast.Call(
            func=_native_name("skip"),
            args=[ast.Constant(_literal_nonempty_string(reason))],
            keywords=[],
        ),
        imports=frozenset({"skip"}),
    )


def _convert_skipif(module: _Module, decorator: ast.expr) -> _ConvertedDecorator:
    if not isinstance(decorator, ast.Call):
        module.error(
            "PYT304_SKIPIF_ARGUMENTS",
            "skipif requires one static Python condition and a reason",
            decorator,
        )
        return _ConvertedDecorator(None)
    positional = list(decorator.args)
    keywords = _keyword_map(module, decorator, "skipif")
    if keywords is None:
        return _ConvertedDecorator(None)
    condition = positional.pop(0) if positional else keywords.pop("condition", None)
    reason = keywords.pop("reason", None)
    if (
        positional
        or keywords
        or condition is None
        or isinstance(condition, ast.Constant)
        and isinstance(condition.value, str)
        or _literal_nonempty_string(reason) is None
    ):
        module.error(
            "PYT304_SKIPIF_ARGUMENTS",
            "skipif requires one non-string condition and a static non-empty reason",
            decorator,
        )
        return _ConvertedDecorator(None)
    return _ConvertedDecorator(
        ast.Call(
            func=_native_name("skip"),
            args=[ast.Constant(_literal_nonempty_string(reason))],
            keywords=[ast.keyword(arg="when", value=condition)],
        ),
        imports=frozenset({"skip"}),
    )


def _convert_parametrize(
    module: _Module, function: _Function, decorator: ast.expr
) -> _ConvertedDecorator:
    if not isinstance(decorator, ast.Call):
        module.error(
            "PYT101_DYNAMIC_PARAMETRIZE",
            "parametrize must be a direct call with static names and rows",
            decorator,
        )
        return _ConvertedDecorator(None)

    positional = list(decorator.args)
    keywords = _keyword_map(module, decorator, "parametrize")
    if keywords is None:
        return _ConvertedDecorator(None)
    argnames_node = positional.pop(0) if positional else keywords.pop("argnames", None)
    argvalues_node = positional.pop(0) if positional else keywords.pop("argvalues", None)
    if positional:
        module.error(
            "PYT101_DYNAMIC_PARAMETRIZE",
            "parametrize has unsupported positional options",
            decorator,
        )
        return _ConvertedDecorator(None)

    if "indirect" in keywords and not _is_false(keywords.pop("indirect")):
        module.error(
            "PYT102_INDIRECT_PARAMETRIZE",
            "indirect parametrization requires request.param fixture semantics",
            decorator,
        )
        return _ConvertedDecorator(None)
    if "scope" in keywords or "_param_mark" in keywords:
        module.error(
            "PYT103_PARAMETRIZE_OPTIONS",
            "parametrize scope/internal mark options are not supported",
            decorator,
        )
        return _ConvertedDecorator(None)
    ids_node = keywords.pop("ids", None)
    if keywords:
        module.error(
            "PYT103_PARAMETRIZE_OPTIONS",
            "unsupported parametrize option(s): " + ", ".join(sorted(keywords)),
            decorator,
        )
        return _ConvertedDecorator(None)

    names = _parameter_names(argnames_node)
    if not names or len(set(names)) != len(names):
        module.error(
            "PYT101_DYNAMIC_PARAMETRIZE",
            "parameter names must be a static, unique string or string sequence",
            argnames_node or decorator,
        )
        return _ConvertedDecorator(None)
    function_parameters = set(_all_parameter_names(function))
    missing = sorted(set(names) - function_parameters)
    if missing:
        module.error(
            "PYT101_DYNAMIC_PARAMETRIZE",
            "parametrize targets unknown function argument(s): " + ", ".join(missing),
            argnames_node or decorator,
        )
        return _ConvertedDecorator(None)

    if not isinstance(argvalues_node, (ast.List, ast.Tuple)):
        module.error(
            "PYT101_DYNAMIC_PARAMETRIZE",
            "parameter rows must be a static list or tuple",
            argvalues_node or decorator,
        )
        return _ConvertedDecorator(None)
    if not argvalues_node.elts:
        module.error(
            "PYT101_DYNAMIC_PARAMETRIZE",
            "empty parameter sets have pytest-specific collection semantics",
            argvalues_node,
        )
        return _ConvertedDecorator(None)
    external_ids = _parameter_ids(ids_node, len(argvalues_node.elts))
    if ids_node is not None and external_ids is None:
        module.error(
            "PYT105_DYNAMIC_IDS",
            "parametrize ids must be a static sequence matching the rows",
            ids_node,
        )
        return _ConvertedDecorator(None)

    output_cases: list[ast.expr] = []
    mapping_cases: list[tuple[str, str]] = []
    seen_ids: set[str] = set()
    for ordinal, row in enumerate(argvalues_node.elts, 1):
        values, embedded_id = _parameter_row(module, row, len(names))
        if values is None:
            return _ConvertedDecorator(None)
        external_id = None if external_ids is None else external_ids[ordinal - 1]
        if embedded_id is not None and external_id is not None:
            module.error(
                "PYT106_CONFLICTING_IDS",
                "a row id and ids= entry both specify the same case identifier",
                row,
            )
            return _ConvertedDecorator(None)
        source_case_id = embedded_id or external_id or f"case-{ordinal:04d}"
        target_case_id = source_case_id
        if target_case_id in seen_ids:
            module.error(
                "PYT107_DUPLICATE_CASE_ID",
                f"duplicate static case id {target_case_id!r}",
                row,
            )
            return _ConvertedDecorator(None)
        seen_ids.add(target_case_id)
        output_cases.append(
            ast.Call(
                func=_native_name("case"),
                args=[],
                keywords=[
                    ast.keyword(arg="id", value=ast.Constant(target_case_id)),
                    *(
                        ast.keyword(arg=name, value=value)
                        for name, value in zip(names, values, strict=True)
                    ),
                ],
            )
        )
        mapping_cases.append((source_case_id, target_case_id))

    return _ConvertedDecorator(
        ast.Call(
            func=_native_name("cases"),
            args=output_cases,
            keywords=[],
        ),
        imports=frozenset({"case", "cases"}),
        parameter_names=names,
        cases=tuple(mapping_cases),
    )


def _parameter_row(
    module: _Module, row: ast.expr, width: int
) -> tuple[list[ast.expr] | None, str | None]:
    embedded_id: str | None = None
    if isinstance(row, ast.Call) and module.aliases.canonical(row.func) == "pytest.param":
        keywords = _keyword_map(module, row, "pytest.param")
        if keywords is None:
            return None, None
        if "marks" in keywords:
            module.error(
                "PYT108_PARAMETER_MARKS",
                "per-case pytest marks have no native CaseDefinition equivalent",
                keywords["marks"],
            )
            return None, None
        unknown = sorted(set(keywords) - {"id"})
        if unknown:
            module.error(
                "PYT103_PARAMETRIZE_OPTIONS",
                "unsupported pytest.param option(s): " + ", ".join(unknown),
                row,
            )
            return None, None
        if "id" in keywords:
            embedded_id = _literal_nonempty_string(keywords["id"])
            if embedded_id is None:
                module.error(
                    "PYT105_DYNAMIC_IDS",
                    "pytest.param id must be a static non-empty string",
                    keywords["id"],
                )
                return None, None
        values = list(row.args)
    elif width == 1:
        values = [row]
    elif isinstance(row, (ast.List, ast.Tuple)):
        values = list(row.elts)
    else:
        module.error(
            "PYT101_DYNAMIC_PARAMETRIZE",
            f"a {width}-argument parameter row must be a static tuple or list",
            row,
        )
        return None, None

    if any(isinstance(value, ast.Starred) for value in values) or len(values) != width:
        module.error(
            "PYT101_DYNAMIC_PARAMETRIZE",
            f"parameter row has {len(values)} values; expected {width}",
            row,
        )
        return None, None
    return values, embedded_id


def _parameter_names(node: ast.expr | None) -> tuple[str, ...]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        values = tuple(part.strip() for part in node.value.split(","))
        return values if all(value.isidentifier() for value in values) else ()
    if isinstance(node, (ast.List, ast.Tuple)):
        values = tuple(
            element.value
            for element in node.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        )
        if len(values) != len(node.elts) or not all(value.isidentifier() for value in values):
            return ()
        return values
    return ()


def _parameter_ids(node: ast.expr | None, count: int) -> tuple[str | None, ...] | None:
    if node is None:
        return None
    if not isinstance(node, (ast.List, ast.Tuple)) or len(node.elts) != count:
        return None
    values: list[str | None] = []
    for element in node.elts:
        if isinstance(element, ast.Constant) and element.value is None:
            values.append(None)
        elif isinstance(element, ast.Constant) and isinstance(
            element.value, (str, int, float, bool)
        ):
            value = str(element.value)
            if not value:
                return None
            values.append(value)
        else:
            return None
    return tuple(values)


def _validate_fixture_dependencies(module: _Module, *, available: set[str]) -> None:
    for fixture in module.fixtures.values():
        for dependency in fixture.dependencies:
            if dependency in available or dependency in _SUPPORTED_BUILTIN_FIXTURES:
                continue
            if dependency in _BUILTIN_FIXTURES:
                module.error(
                    "PYT209_BUILTIN_FIXTURE",
                    f"fixture {fixture.effective_name!r} depends on pytest built-in {dependency!r}",
                    fixture.node,
                )
            else:
                module.error(
                    "PYT210_UNKNOWN_FIXTURE",
                    f"fixture {fixture.effective_name!r} depends on unknown fixture {dependency!r}",
                    fixture.node,
                )
        if "monkeypatch" in fixture.dependencies and "monkeypatch" not in available:
            _validate_builtin_monkeypatch_usage(module, fixture.node)


def _validate_runtime_pytest_calls(module: _Module) -> None:
    if module.tree is None:
        return
    decorator_call_ids = {id(call) for call in _decorator_calls(module.tree)}
    for node in ast.walk(module.tree):
        if not isinstance(node, ast.Call) or id(node) in decorator_call_ids:
            continue
        canonical = module.aliases.canonical(node.func)
        if canonical is None:
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and node.args
                and (module.aliases.canonical(node.args[0]) or "").startswith("pytest")
            ):
                module.error(
                    "PYT005_DYNAMIC_PYTEST_ACCESS",
                    "dynamic getattr access to pytest cannot be classified safely",
                    node,
                )
            continue
        if canonical in _ALLOWED_RUNTIME_HELPERS:
            continue
        if canonical in _OUTCOME_CALLS:
            module.error(
                _OUTCOME_CALLS[canonical],
                f"runtime call {canonical} does not produce a native Testenix outcome",
                node,
            )
        elif canonical == "pytest.param":
            # A pytest.param outside a handled decorator remains runtime-specific.
            module.error(
                "PYT108_PARAMETER_MARKS",
                "pytest.param is only supported inside one static parametrize decorator",
                node,
            )
        elif canonical.startswith("pytest.") or canonical.startswith("pytest_asyncio."):
            module.error(
                "PYT506_UNSUPPORTED_PYTEST_API",
                f"pytest API {canonical!r} has no proven native equivalent",
                node,
            )


def _diagnose_conftest_conflicts(modules: Sequence[_Module]) -> None:
    by_parent = {module.source.project_relative.parent: module for module in modules}
    for module in modules:
        names = set(module.fixtures)
        parent = module.source.project_relative.parent.parent
        while True:
            ancestor = by_parent.get(parent)
            if ancestor is not None:
                conflicts = names.intersection(ancestor.fixtures)
                if conflicts:
                    module.error(
                        "PYT207_FIXTURE_CONFLICT",
                        "nested conftest overrides fixture(s): " + ", ".join(sorted(conflicts)),
                    )
            if parent == Path(".") or parent == parent.parent:
                break
            parent = parent.parent


def _diagnose_fixture_override(
    module: _Module, adjacent: _Module, ancestors: Sequence[_Module]
) -> None:
    conflicts = set(module.fixtures).intersection(adjacent.fixtures)
    conflicts.update(
        name
        for ancestor in ancestors
        for name in set(adjacent.fixtures).intersection(ancestor.fixtures)
    )
    if conflicts:
        module.error(
            "PYT207_FIXTURE_CONFLICT",
            "fixture override/conflict cannot be represented safely: "
            + ", ".join(sorted(conflicts)),
        )


def _diagnose_ancestor_fixture_use(
    module: _Module, adjacent: _Module | None, ancestors: Sequence[_Module]
) -> None:
    if not ancestors or module.tree is None:
        return
    adjacent_names = set() if adjacent is None else set(adjacent.fixtures)
    local_names = set(module.fixtures)
    requested: set[str] = set()
    for statement in module.tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            requested.update(_required_parameters(statement))
        elif isinstance(statement, ast.ClassDef) and statement in module.test_classes:
            for member in statement.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    requested.update(_required_parameters(member))
    requested.update(
        fixture.effective_name
        for ancestor in ancestors
        for fixture in ancestor.fixtures.values()
        if fixture.autouse
    )
    if module.uses_bare_asyncio:
        # pytest-asyncio requests this fixture implicitly for every marked test.
        requested.add("event_loop_policy")
    inherited = {
        name
        for ancestor in ancestors
        for name in ancestor.fixtures
        if name in requested and name not in adjacent_names and name not in local_names
    }
    if inherited:
        module.error(
            "PYT213_ANCESTOR_CONFTEST",
            "ancestor conftest fixtures require package-aware imports not supported by the MVP: "
            + ", ".join(sorted(inherited)),
        )


def _ancestor_conftests(
    path: Path, conftests_by_parent: dict[Path, _Module]
) -> tuple[_Module, ...]:
    modules: list[_Module] = []
    parent = path.parent.parent
    while True:
        module = conftests_by_parent.get(parent)
        if module is not None:
            modules.append(module)
        if parent == Path(".") or parent == parent.parent:
            break
        parent = parent.parent
    return tuple(modules)


def _conftest_helper_name(source: SourceFile) -> str:
    digest = hashlib.sha1(
        f"{source.project_relative.as_posix()}\0{source.sha256.lower()}".encode(),
        usedforsecurity=False,
    ).hexdigest()[:12]
    return f"_testenix_conftest_{digest}"


def _target_relative_path(source: SourceFile) -> Path:
    """Return a native-discoverable path without changing normal test_ names."""

    path = source.migration_relative
    if path.suffix == ".py" and not path.name.startswith("test_"):
        stem = path.stem.removesuffix("_test") if path.stem.endswith("_test") else path.stem
        return path.with_name(f"test_{stem}.py")
    return path


def _decorator_canonical(decorator: ast.expr, aliases: _Aliases) -> str | None:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    return aliases.canonical(target)


def _decorator_calls(tree: ast.Module) -> tuple[ast.Call, ...]:
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for decorator in node.decorator_list:
            calls.extend(call for call in ast.walk(decorator) if isinstance(call, ast.Call))
    return tuple(calls)


def _keyword_map(module: _Module, call: ast.Call, description: str) -> dict[str, ast.expr] | None:
    result: dict[str, ast.expr] = {}
    for keyword in call.keywords:
        if keyword.arg is None:
            module.error(
                "PYT006_DYNAMIC_KEYWORDS",
                f"{description} uses dynamic **kwargs",
                keyword.value,
            )
            return None
        if keyword.arg in result:
            module.error(
                "PYT006_DYNAMIC_KEYWORDS",
                f"{description} supplies {keyword.arg!r} more than once",
                keyword.value,
            )
            return None
        result[keyword.arg] = keyword.value
    return result


def _all_parameter_names(function: _Function) -> tuple[str, ...]:
    return tuple(
        argument.arg
        for argument in (
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
        )
    )


def _required_parameters(function: _Function) -> tuple[str, ...]:
    positional = (*function.args.posonlyargs, *function.args.args)
    required_positional_count = len(positional) - len(function.args.defaults)
    required = [argument.arg for argument in positional[:required_positional_count]]
    required.extend(
        argument.arg
        for argument, default in zip(
            function.args.kwonlyargs, function.args.kw_defaults, strict=True
        )
        if default is None
    )
    return tuple(required)


def _insert_testenix_import(tree: ast.Module, names: Iterable[str]) -> None:
    materialized = tuple(sorted(set(names)))
    if materialized:
        _insert_statement(
            tree,
            ast.ImportFrom(
                module="testenix",
                names=[ast.alias(name=name, asname=_NATIVE_ALIASES[name]) for name in materialized],
                level=0,
            ),
        )


def _insert_migration_runtime_import(tree: ast.Module, names: Iterable[str]) -> None:
    materialized = tuple(sorted(set(names)))
    if materialized:
        _insert_statement(
            tree,
            ast.ImportFrom(
                module="testenix.migration_runtime",
                names=[
                    ast.alias(name=name, asname=_MIGRATION_RUNTIME_ALIASES[name])
                    for name in materialized
                ],
                level=0,
            ),
        )


def _remove_unused_pytest_imports(tree: ast.Module) -> None:
    """Remove only foreign-runner imports made dead by decorator conversion.

    Import aliases are considered independently, so a partially used
    ``from pytest import fixture, raises`` becomes ``from pytest import raises``.
    Any remaining load of the local name keeps the import; this intentionally
    favors a harmless retained dependency over deleting a runtime helper.
    """

    loaded_names = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    statements: list[ast.stmt] = []
    for statement in tree.body:
        if isinstance(statement, ast.Import):
            retained = [
                alias
                for alias in statement.names
                if not _is_exact_pytest_module(alias.name)
                or (alias.asname or alias.name) in loaded_names
            ]
            if retained:
                statement.names = retained
                statements.append(statement)
            continue
        if isinstance(statement, ast.ImportFrom) and _is_pytest_import_module(statement.module):
            retained = [
                alias
                for alias in statement.names
                if alias.name == "*" or (alias.asname or alias.name) in loaded_names
            ]
            if retained:
                statement.names = retained
                statements.append(statement)
            continue
        statements.append(statement)
    tree.body = statements


def _is_exact_pytest_module(name: str) -> bool:
    return name in {"pytest", "pytest_asyncio"}


def _is_pytest_import_module(name: str | None) -> bool:
    return name is not None and any(
        name == root or name.startswith(f"{root}.") for root in ("pytest", "pytest_asyncio")
    )


def _native_name(name: str) -> ast.Name:
    return ast.Name(id=_NATIVE_ALIASES[name], ctx=ast.Load())


def _migration_runtime_name(name: str) -> ast.Name:
    return ast.Name(id=_MIGRATION_RUNTIME_ALIASES[name], ctx=ast.Load())


def _diagnose_native_import_collisions(module: _Module) -> None:
    if module.tree is None:
        return
    required_aliases = {_NATIVE_ALIASES[name] for name in module.imports}
    required_aliases.update(
        _MIGRATION_RUNTIME_ALIASES[name] for name in module.migration_runtime_imports
    )
    collisions = _top_level_bound_names(module.tree).intersection(required_aliases)
    if collisions:
        collision_node = next(
            (
                statement
                for statement in module.tree.body
                if _top_level_statement_bound_names(statement).intersection(collisions)
            ),
            None,
        )
        module.error(
            "PYT008_GENERATED_IMPORT_COLLISION",
            "source binds reserved generated import name(s): " + ", ".join(sorted(collisions)),
            collision_node,
        )


def _insert_import_from(tree: ast.Module, module: str, names: Sequence[str]) -> None:
    if names:
        _insert_statement(
            tree,
            ast.ImportFrom(
                module=module,
                names=[ast.alias(name=name) for name in names],
                level=0,
            ),
        )


def _insert_statement(tree: ast.Module, statement: ast.stmt) -> None:
    index = 0
    if (
        tree.body
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    ):
        index = 1
    while index < len(tree.body):
        current = tree.body[index]
        if not isinstance(current, ast.ImportFrom) or current.module != "__future__":
            break
        index += 1
    tree.body.insert(index, statement)


def _render(tree: ast.Module, source_name: str) -> str | None:
    ast.fix_missing_locations(tree)
    body = ast.unparse(tree)
    content = f"# Generated by Testenix from {source_name}; do not edit.\n{body}\n"
    try:
        compile(content, source_name, "exec")
    except (SyntaxError, ValueError, TypeError):
        return None
    return content


def _top_level_bound_names(tree: ast.Module) -> set[str]:
    return {name for statement in tree.body for name in _top_level_statement_bound_names(statement)}


def _top_level_statement_bound_names(statement: ast.stmt) -> set[str]:
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {statement.name}
    if isinstance(statement, (ast.Import, ast.ImportFrom)):
        return {
            alias.asname or alias.name.split(".")[0]
            for alias in statement.names
            if alias.name != "*"
        }
    if isinstance(statement, (ast.Assign, ast.AnnAssign)):
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        return {name for target in targets for name in _target_names(target)}
    return set()


def _target_names(target: ast.expr) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.List, ast.Tuple)):
        return {name for element in target.elts for name in _target_names(element)}
    return set()


class _ClassScopeBindingCollector(ast.NodeVisitor):
    """Collect names bound in a class namespace without entering nested scopes."""

    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802 - ast visitor protocol
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.names.add(node.id)

    def visit_FunctionDef(  # noqa: N802 - ast visitor protocol
        self, node: ast.FunctionDef
    ) -> None:
        self.names.add(node.name)

    def visit_AsyncFunctionDef(  # noqa: N802 - ast visitor protocol
        self, node: ast.AsyncFunctionDef
    ) -> None:
        self.names.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802 - ast visitor protocol
        self.names.add(node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802 - ast visitor protocol
        return

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802 - ast visitor protocol
        self.names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)

    def visit_ImportFrom(  # noqa: N802 - ast visitor protocol
        self, node: ast.ImportFrom
    ) -> None:
        self.names.update(alias.asname or alias.name for alias in node.names if alias.name != "*")

    def visit_ExceptHandler(  # noqa: N802 - ast visitor protocol
        self, node: ast.ExceptHandler
    ) -> None:
        if node.name is not None:
            self.names.add(node.name)
        for statement in node.body:
            self.visit(statement)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:  # noqa: N802 - ast visitor protocol
        if node.name is not None:
            self.names.add(node.name)
        if node.pattern is not None:
            self.visit(node.pattern)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:  # noqa: N802 - ast visitor protocol
        if node.name is not None:
            self.names.add(node.name)

    def visit_MatchMapping(  # noqa: N802 - ast visitor protocol
        self, node: ast.MatchMapping
    ) -> None:
        if node.rest is not None:
            self.names.add(node.rest)
        for pattern in node.patterns:
            self.visit(pattern)


def _class_scope_bound_names(statement: ast.stmt) -> set[str]:
    collector = _ClassScopeBindingCollector()
    collector.visit(statement)
    return collector.names


def _assigned_name(statement: ast.stmt) -> str | None:
    if (
        isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
    ):
        return statement.targets[0].id
    if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
        return statement.target.id
    return None


def _is_test_class(node: ast.ClassDef) -> bool:
    has_test_method = any(
        isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
        and statement.name.startswith("test")
        for statement in node.body
    )
    is_unittest = any(_dotted_name(base).endswith("TestCase") for base in node.bases)
    return has_test_method and (node.name.startswith("Test") or is_unittest)


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _dotted_name(node.value)
        return f"{owner}.{node.attr}" if owner else node.attr
    return ""


def _literal_nonempty_string(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.strip():
        return node.value
    return None


def _is_false(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _is_true(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _deduplicate_diagnostics(
    diagnostics: Iterable[MigrationDiagnostic],
) -> tuple[MigrationDiagnostic, ...]:
    result: list[MigrationDiagnostic] = []
    seen: set[tuple[str, str, str, int | None, DiagnosticSeverity]] = set()
    for diagnostic in diagnostics:
        key = (
            diagnostic.code,
            diagnostic.message,
            diagnostic.source,
            diagnostic.line,
            diagnostic.severity,
        )
        if key not in seen:
            seen.add(key)
            result.append(diagnostic)
    return tuple(result)


__all__ = ["convert_pytest_suite", "detect_pytest_module"]
