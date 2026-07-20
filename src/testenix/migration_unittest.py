"""Conservative source analyzer and wrapper generator for unittest suites."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from testenix.migration_models import (
    ConversionBundle,
    GeneratedArtifact,
    MigrationDiagnostic,
    SourceFile,
    TestMapping,
)

_DIRECT_TEST_CASES = {
    "unittest.TestCase": "sync",
    "unittest.case.TestCase": "sync",
    "unittest.IsolatedAsyncioTestCase": "async",
    "unittest.async_case.IsolatedAsyncioTestCase": "async",
}
_STATIC_UNITTEST_DECORATORS = {
    "unittest.expectedFailure",
    "unittest.skip",
    "unittest.skipIf",
    "unittest.skipUnless",
}
_CLASS_LIFECYCLE_METHODS = {"setUpClass", "tearDownClass"}
_CUSTOM_RUNNER_METHODS = {
    "__init__",
    "_addExpectedFailure",
    "_addUnexpectedSuccess",
    "_callCleanup",
    "_callSetUp",
    "_callTearDown",
    "_callTestMethod",
    "addClassCleanup",
    "defaultTestResult",
    "doClassCleanups",
    "enterClassContext",
    "run",
}
_DYNAMIC_DECORATOR_PARTS = {
    "data",
    "ddt",
    "expand",
    "parameterized",
    "parameterized_class",
    "parametrize",
    "unpack",
}
_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}\Z")
_MANIFEST_PATH = Path(".testenix-unittest-sources.json")

FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True, slots=True)
class _Aliases:
    qualified: dict[str, str]


@dataclass(frozen=True, slots=True)
class _ConvertibleMethod:
    class_node: ast.ClassDef
    class_kind: str
    method: FunctionNode


def _aliases(tree: ast.Module) -> _Aliases:
    qualified: dict[str, str] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Import):
            for imported in statement.names:
                if imported.asname is not None:
                    qualified[imported.asname] = imported.name
                else:
                    root = imported.name.split(".", 1)[0]
                    qualified[root] = root
        elif isinstance(statement, ast.ImportFrom) and statement.module is not None:
            for imported in statement.names:
                if imported.name == "*":
                    continue
                local_name = imported.asname or imported.name
                qualified[local_name] = f"{statement.module}.{imported.name}"
    return _Aliases(qualified)


def _qualified_name(node: ast.expr, aliases: _Aliases) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.qualified.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        owner = _qualified_name(node.value, aliases)
        return f"{owner}.{node.attr}" if owner is not None else None
    return None


def _decorator_name(decorator: ast.expr, aliases: _Aliases) -> str | None:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    return _qualified_name(target, aliases)


def _direct_case_kind(node: ast.ClassDef, aliases: _Aliases) -> str | None:
    if len(node.bases) != 1 or node.keywords:
        return None
    name = _qualified_name(node.bases[0], aliases)
    return _DIRECT_TEST_CASES.get(name or "")


def _diagnostic(
    code: str,
    message: str,
    source: SourceFile,
    node: ast.AST | None = None,
) -> MigrationDiagnostic:
    return MigrationDiagnostic(
        code=code,
        message=message,
        source=source.project_relative.as_posix(),
        line=getattr(node, "lineno", None),
    )


def _walk_bodies(function: FunctionNode) -> Iterable[ast.AST]:
    for statement in function.body:
        yield from ast.walk(statement)


def _contains_call_named(
    function: FunctionNode,
    names: set[str],
    aliases: _Aliases,
) -> ast.Call | None:
    for node in _walk_bodies(function):
        if not isinstance(node, ast.Call):
            continue
        qualified = _qualified_name(node.func, aliases) or ""
        if any(qualified == name or qualified.endswith(f".{name}") for name in names):
            return node
    return None


def _contains_skip_raise(
    function: FunctionNode,
    aliases: _Aliases,
) -> ast.Raise | None:
    for node in _walk_bodies(function):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        expression = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
        name = _qualified_name(expression, aliases) or ""
        if name == "unittest.SkipTest" or name.endswith(".SkipTest"):
            return node
    return None


def _is_patch_decorator(name: str | None) -> bool:
    if name is None:
        return False
    return name == "unittest.mock.patch" or name.startswith("unittest.mock.patch.")


def _is_dynamic_decorator(name: str | None) -> bool:
    if name is None:
        return False
    parts = {part.lower() for part in name.split(".")}
    return bool(parts.intersection(_DYNAMIC_DECORATOR_PARTS))


def _safe_class_decorator(name: str | None) -> bool:
    return name in _STATIC_UNITTEST_DECORATORS or _is_patch_decorator(name)


def _required_parameter_count(function: FunctionNode) -> int:
    positional = [*function.args.posonlyargs, *function.args.args]
    positional_required = max(0, len(positional) - len(function.args.defaults))
    keyword_required = sum(default is None for default in function.args.kw_defaults)
    return positional_required + keyword_required


def _implicit_parameter_count(function: FunctionNode, aliases: _Aliases) -> int:
    decorators = {
        _decorator_name(decorator, aliases) or "" for decorator in function.decorator_list
    }
    if "staticmethod" in decorators or "builtins.staticmethod" in decorators:
        return 0
    return 1


def _method_diagnostics(
    source: SourceFile,
    method: FunctionNode,
    *,
    class_kind: str,
    aliases: _Aliases,
) -> tuple[MigrationDiagnostic, ...]:
    diagnostics: list[MigrationDiagnostic] = []
    subtest = _contains_call_named(method, {"subTest"}, aliases)
    if subtest is not None:
        diagnostics.append(
            _diagnostic(
                "UNIT001",
                "unittest subTest has no lossless native Testenix result model",
                source,
                subtest,
            )
        )

    dynamic_skip = _contains_call_named(method, {"skipTest"}, aliases)
    skip_raise = _contains_skip_raise(method, aliases)
    if dynamic_skip is not None or skip_raise is not None:
        diagnostics.append(
            _diagnostic(
                "UNIT002",
                "runtime skipTest/SkipTest is unsupported; use a static unittest skip decorator",
                source,
                dynamic_skip or skip_raise,
            )
        )

    for decorator in method.decorator_list:
        name = _decorator_name(decorator, aliases)
        if _is_dynamic_decorator(name):
            diagnostics.append(
                _diagnostic(
                    "UNIT008",
                    f"dynamic test generation decorator {name!r} is unsupported",
                    source,
                    decorator,
                )
            )

    if isinstance(method, ast.AsyncFunctionDef) and class_kind != "async":
        diagnostics.append(
            _diagnostic(
                "UNIT012",
                "async test methods require unittest.IsolatedAsyncioTestCase",
                source,
                method,
            )
        )
    if any(isinstance(node, (ast.Yield, ast.YieldFrom)) for node in _walk_bodies(method)):
        diagnostics.append(
            _diagnostic(
                "UNIT012",
                "generator test methods are unsupported by the unittest migration adapter",
                source,
                method,
            )
        )

    decorators = [_decorator_name(value, aliases) for value in method.decorator_list]
    if not any(_is_patch_decorator(name) for name in decorators):
        implicit = _implicit_parameter_count(method, aliases)
        if _required_parameter_count(method) > implicit:
            diagnostics.append(
                _diagnostic(
                    "UNIT014",
                    "test method requires arguments that unittest cannot supply statically",
                    source,
                    method,
                )
            )
    return tuple(diagnostics)


def _class_diagnostics(
    source: SourceFile,
    node: ast.ClassDef,
    aliases: _Aliases,
) -> tuple[MigrationDiagnostic, ...]:
    diagnostics: list[MigrationDiagnostic] = []
    direct_names = {
        _qualified_name(base, aliases) for base in node.bases if _qualified_name(base, aliases)
    }
    direct_unittest = direct_names.intersection(_DIRECT_TEST_CASES)
    if direct_names.intersection({"unittest.FunctionTestCase", "unittest.case.FunctionTestCase"}):
        diagnostics.append(
            _diagnostic(
                "UNIT009",
                "unittest.FunctionTestCase cannot be converted as a TestCase subclass",
                source,
                node,
            )
        )
    if direct_unittest and (len(node.bases) != 1 or node.keywords):
        diagnostics.append(
            _diagnostic(
                "UNIT006",
                "unittest classes with mixins, multiple bases, or metaclasses are unsupported",
                source,
                node,
            )
        )

    for decorator in node.decorator_list:
        name = _decorator_name(decorator, aliases)
        if _is_dynamic_decorator(name):
            diagnostics.append(
                _diagnostic(
                    "UNIT008",
                    f"dynamic class decorator {name!r} is unsupported",
                    source,
                    decorator,
                )
            )
        elif not _safe_class_decorator(name):
            diagnostics.append(
                _diagnostic(
                    "UNIT015",
                    f"class decorator {name or '<dynamic>'!r} cannot be proven migration-safe",
                    source,
                    decorator,
                )
            )

    seen_members: set[str] = set()
    for member in node.body:
        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if member.name in seen_members and member.name.startswith("test"):
                diagnostics.append(
                    _diagnostic(
                        "UNIT008",
                        f"test method {member.name!r} is redefined in the same class",
                        source,
                        member,
                    )
                )
            seen_members.add(member.name)
            if member.name in _CLASS_LIFECYCLE_METHODS:
                diagnostics.append(
                    _diagnostic(
                        "UNIT003",
                        f"{member.name} requires class-scoped execution affinity",
                        source,
                        member,
                    )
                )
            if member.name in _CUSTOM_RUNNER_METHODS:
                diagnostics.append(
                    _diagnostic(
                        "UNIT005",
                        f"custom unittest runner hook {member.name!r} is unsupported",
                        source,
                        member,
                    )
                )
            class_cleanup = _contains_call_named(
                member,
                {"addClassCleanup", "enterClassContext"},
                aliases,
            )
            if class_cleanup is not None:
                diagnostics.append(
                    _diagnostic(
                        "UNIT003",
                        "unittest class cleanups require class-scoped execution affinity",
                        source,
                        class_cleanup,
                    )
                )
            if not member.name.startswith("test"):
                nested_subtest = _contains_call_named(member, {"subTest"}, aliases)
                if nested_subtest is not None:
                    diagnostics.append(
                        _diagnostic(
                            "UNIT001",
                            "a helper method uses subTest, so callers cannot be "
                            "migrated losslessly",
                            source,
                            nested_subtest,
                        )
                    )
                nested_skip = _contains_call_named(member, {"skipTest"}, aliases)
                nested_raise = _contains_skip_raise(member, aliases)
                if nested_skip is not None or nested_raise is not None:
                    diagnostics.append(
                        _diagnostic(
                            "UNIT002",
                            "a helper method can trigger a dynamic unittest skip",
                            source,
                            nested_skip or nested_raise,
                        )
                    )
        elif isinstance(member, (ast.Assign, ast.AnnAssign)):
            targets = list(member.targets) if isinstance(member, ast.Assign) else [member.target]
            if any(
                isinstance(target, ast.Name) and target.id.startswith("test") for target in targets
            ):
                diagnostics.append(
                    _diagnostic(
                        "UNIT008",
                        "class-level assignment of unittest test methods is unsupported",
                        source,
                        member,
                    )
                )
    return tuple(diagnostics)


def _module_diagnostics(
    source: SourceFile,
    tree: ast.Module,
    aliases: _Aliases,
) -> tuple[MigrationDiagnostic, ...]:
    diagnostics: list[MigrationDiagnostic] = []
    seen_classes: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, ast.ClassDef):
            if statement.name in seen_classes:
                diagnostics.append(
                    _diagnostic(
                        "UNIT008",
                        f"class {statement.name!r} is redefined in the same module",
                        source,
                        statement,
                    )
                )
            seen_classes.add(statement.name)
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if statement.name in {"setUpModule", "tearDownModule"}:
                diagnostics.append(
                    _diagnostic(
                        "UNIT004",
                        f"{statement.name} requires module lifecycle emulation",
                        source,
                        statement,
                    )
                )
            elif statement.name == "load_tests":
                diagnostics.append(
                    _diagnostic(
                        "UNIT007",
                        "custom unittest load_tests changes the test inventory dynamically",
                        source,
                        statement,
                    )
                )

        nested_nodes: Iterable[ast.AST]
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # Function and class internals are analyzed by their dedicated
            # checks. Treating instance attributes such as ``self.test_data``
            # as module-level test generation would create false blockers.
            nested_nodes = ()
        else:
            nested_nodes = ast.walk(statement)
        for node in nested_nodes:
            if isinstance(node, ast.Call):
                name = _qualified_name(node.func, aliases) or ""
                if name in {
                    "unittest.addModuleCleanup",
                    "unittest.enterModuleContext",
                }:
                    diagnostics.append(
                        _diagnostic(
                            "UNIT004",
                            "unittest module cleanups require module lifecycle emulation",
                            source,
                            node,
                        )
                    )
                if name in {"setattr", "builtins.setattr"} and len(node.args) >= 2:
                    dynamic_name = node.args[1]
                    if (
                        isinstance(dynamic_name, ast.Constant)
                        and isinstance(dynamic_name.value, str)
                        and dynamic_name.value.startswith("test")
                    ):
                        diagnostics.append(
                            _diagnostic(
                                "UNIT008",
                                "dynamic assignment of unittest test methods is unsupported",
                                source,
                                node,
                            )
                        )
            elif isinstance(node, (ast.Name, ast.Attribute)):
                name = _qualified_name(node, aliases) or ""
                if name in {
                    "unittest.FunctionTestCase",
                    "unittest.case.FunctionTestCase",
                }:
                    diagnostics.append(
                        _diagnostic(
                            "UNIT009",
                            "unittest.FunctionTestCase is unsupported by class wrapper migration",
                            source,
                            node,
                        )
                    )
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.ctx, ast.Store)
                    and node.attr.startswith("test")
                ):
                    diagnostics.append(
                        _diagnostic(
                            "UNIT008",
                            "dynamic assignment of unittest test methods is unsupported",
                            source,
                            node,
                        )
                    )
    return tuple(diagnostics)


def _indirect_inheritance_diagnostics(
    source: SourceFile,
    tree: ast.Module,
    aliases: _Aliases,
) -> tuple[MigrationDiagnostic, ...]:
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    unittest_classes = {
        node.name for node in classes if _direct_case_kind(node, aliases) is not None
    }
    diagnostics: list[MigrationDiagnostic] = []
    changed = True
    while changed:
        changed = False
        for node in classes:
            if node.name in unittest_classes:
                continue
            base_names = {_qualified_name(base, aliases) for base in node.bases}
            if base_names.intersection(unittest_classes):
                unittest_classes.add(node.name)
                diagnostics.append(
                    _diagnostic(
                        "UNIT006",
                        f"indirect unittest inheritance for {node.name!r} is unsupported",
                        source,
                        node,
                    )
                )
                changed = True
    return tuple(diagnostics)


def _parse(source: SourceFile) -> ast.Module | MigrationDiagnostic:
    try:
        return ast.parse(source.text, filename=source.project_relative.as_posix())
    except SyntaxError as error:
        return MigrationDiagnostic(
            code="UNIT010",
            message=f"cannot parse unittest source: {error.msg}",
            source=source.project_relative.as_posix(),
            line=error.lineno,
        )


def detect_unittest_module(source: SourceFile) -> bool:
    """Return whether a source module contains recognizable unittest constructs."""

    parsed = _parse(source)
    if isinstance(parsed, MigrationDiagnostic):
        return "unittest" in source.text and (
            "TestCase" in source.text or "FunctionTestCase" in source.text
        )
    aliases = _aliases(parsed)
    for node in ast.walk(parsed):
        if isinstance(node, ast.ClassDef) and any(
            (_qualified_name(base, aliases) or "") in _DIRECT_TEST_CASES for base in node.bases
        ):
            return True
        if isinstance(node, ast.Name) and aliases.qualified.get(node.id) in {
            "unittest.FunctionTestCase",
            "unittest.case.FunctionTestCase",
        }:
            return True
        if isinstance(node, ast.Attribute) and (_qualified_name(node, aliases) or "") in {
            "unittest.FunctionTestCase",
            "unittest.case.FunctionTestCase",
        }:
            return True
    return False


def _safe_identifier(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z_]", "_", value)
    if not normalized or normalized[0].isdigit():
        normalized = f"_{normalized}"
    return normalized


def _wrapper_name(source_id: str, class_name: str, method_name: str) -> str:
    readable = _safe_identifier(f"test_{class_name}__{method_name}")[:100]
    suffix = hashlib.sha256(source_id.encode()).hexdigest()[:10]
    return f"{readable}__{suffix}"


def _artifact_relative(source: SourceFile) -> Path:
    target = source.migration_relative
    if target.name.startswith("test_"):
        return target
    return target.with_name(f"test_{target.name}")


def _generated_content(
    source: SourceFile,
    methods: Sequence[_ConvertibleMethod],
    mappings: Sequence[TestMapping],
    source_relative_to_wrapper: str,
    manifest_relative_to_wrapper: str,
    manifest_sha256: str,
) -> str:
    class_aliases: dict[str, str] = {}
    class_kinds: dict[str, str] = {}
    for method in methods:
        class_kinds[method.class_node.name] = method.class_kind
    for index, class_name in enumerate(class_kinds, 1):
        class_aliases[class_name] = f"_UNITTEST_CASE_{index}"

    source_path = source.project_relative.as_posix()
    lines = [
        "# Generated by Testenix unittest migration. Do not edit.",
        f"# Original: {source_path} (sha256: {source.sha256.lower()})",
        "from __future__ import annotations",
        "",
        "from testenix import skip as _testenix_skip",
        "from testenix import test as _testenix_test",
        "from testenix import xfail as _testenix_xfail",
        "from testenix.migration_runtime import (",
        "    load_unittest_case as _load_unittest_case,",
        "    resolve_unittest_source as _resolve_unittest_source,",
        "    run_unittest_case as _run_unittest_case,",
        "    unittest_case_expects_failure as _unittest_case_expects_failure,",
        "    unittest_case_is_skipped as _unittest_case_is_skipped,",
        "    unittest_case_skip_reason as _unittest_case_skip_reason,",
        ")",
        "",
        "# Keep pytest from collecting the generated shadow suite.",
        "__test__ = False",
        f"_SOURCE_SHA256 = {source.sha256.lower()!r}",
        "_SOURCE_PATH = _resolve_unittest_source(",
        f"    __file__, {source_relative_to_wrapper!r}, _SOURCE_SHA256,",
        f"    project_relative_source={source_path!r},",
        f"    manifest_relative_to_wrapper={manifest_relative_to_wrapper!r},",
        f"    manifest_sha256={manifest_sha256!r},",
        ")",
        "",
    ]
    for class_name, alias in class_aliases.items():
        lines.append(f"{alias} = _load_unittest_case(_SOURCE_PATH, {class_name!r}, _SOURCE_SHA256)")
    lines.append("")

    for method, mapping in zip(methods, mappings, strict=True):
        alias = class_aliases[method.class_node.name]
        method_name = method.method.name
        lines.extend(
            [
                f"@_testenix_test({mapping.source_id!r}, tags={{'migrated', 'unittest'}})",
                "@_testenix_skip(",
                f"    _unittest_case_skip_reason({alias}, {method_name!r}),",
                f"    when=_unittest_case_is_skipped({alias}, {method_name!r}),",
                ")",
                "@_testenix_xfail(",
                "    'migrated unittest.expectedFailure',",
                f"    when=_unittest_case_expects_failure({alias}, {method_name!r}),",
                ")",
                f"def {mapping.target_function}() -> None:",
                "    # Reload through the digest-checked cache for every execution.",
                "    test_case = _load_unittest_case(",
                "        _SOURCE_PATH,",
                f"        {method.class_node.name!r},",
                "        _SOURCE_SHA256,",
                "    )",
                f"    _run_unittest_case(test_case, {method_name!r})",
                "",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _convert_source(
    source: SourceFile,
    *,
    output_relative: Path,
    manifest_sha256: str,
) -> tuple[GeneratedArtifact | None, tuple[TestMapping, ...], tuple[MigrationDiagnostic, ...]]:
    parsed = _parse(source)
    if isinstance(parsed, MigrationDiagnostic):
        return None, (), (parsed,)
    if not detect_unittest_module(source):
        return None, (), ()

    aliases = _aliases(parsed)
    diagnostics = [
        *_module_diagnostics(source, parsed, aliases),
        *_indirect_inheritance_diagnostics(source, parsed, aliases),
    ]
    module_blocked = any(
        diagnostic.code in {"UNIT004", "UNIT007", "UNIT008", "UNIT009"}
        for diagnostic in diagnostics
    )

    methods: list[_ConvertibleMethod] = []
    for class_node in (node for node in parsed.body if isinstance(node, ast.ClassDef)):
        kind = _direct_case_kind(class_node, aliases)
        class_diagnostics = _class_diagnostics(source, class_node, aliases)
        diagnostics.extend(class_diagnostics)
        if kind is None:
            continue
        class_blocked = bool(class_diagnostics)
        for member in class_node.body:
            if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not member.name.startswith("test"):
                continue
            method_diagnostics = _method_diagnostics(
                source,
                member,
                class_kind=kind,
                aliases=aliases,
            )
            diagnostics.extend(method_diagnostics)
            if not module_blocked and not class_blocked and not method_diagnostics:
                methods.append(_ConvertibleMethod(class_node, kind, member))

    if not methods:
        return None, (), tuple(diagnostics)
    if _SHA256_PATTERN.fullmatch(source.sha256) is None:
        diagnostics.append(
            _diagnostic(
                "UNIT017",
                "source manifest contains an invalid SHA-256 digest",
                source,
            )
        )
        return None, (), tuple(diagnostics)

    artifact_relative = _artifact_relative(source)
    wrapper_parent = output_relative / artifact_relative.parent
    source_relative_to_wrapper = os.path.relpath(
        source.project_relative,
        start=wrapper_parent,
    )
    manifest_relative_to_wrapper = os.path.relpath(
        output_relative / _MANIFEST_PATH,
        start=wrapper_parent,
    )
    target_file = artifact_relative.as_posix()
    mappings = tuple(
        TestMapping(
            source_id=(
                f"{source.project_relative.as_posix()}::"
                f"{method.class_node.name}.{method.method.name}"
            ),
            target_file=target_file,
            target_function=_wrapper_name(
                (
                    f"{source.project_relative.as_posix()}::"
                    f"{method.class_node.name}.{method.method.name}"
                ),
                method.class_node.name,
                method.method.name,
            ),
        )
        for method in methods
    )
    artifact = GeneratedArtifact(
        relative_path=artifact_relative,
        content=_generated_content(
            source,
            methods,
            mappings,
            source_relative_to_wrapper,
            manifest_relative_to_wrapper,
            manifest_sha256,
        ),
        source_files=(source.project_relative.as_posix(),),
    )
    return artifact, mappings, tuple(diagnostics)


def convert_unittest_suite(
    files: Sequence[SourceFile],
    *,
    output_relative: Path = Path("generated"),
    manifest_files: Sequence[SourceFile] | None = None,
) -> ConversionBundle:
    """Convert the provably safe unittest subset into native shadow wrappers."""

    manifest_sources = tuple(manifest_files if manifest_files is not None else files)
    manifest_hashes = {
        source.project_relative.as_posix(): source.sha256.lower()
        for source in sorted(manifest_sources, key=lambda item: item.project_relative.as_posix())
    }
    manifest_content = (
        json.dumps(
            {
                "format": "testenix.unittest-source-manifest",
                "schema_version": 1,
                "source_hashes": manifest_hashes,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    manifest_sha256 = hashlib.sha256(manifest_content.encode("utf-8")).hexdigest()
    artifacts: list[GeneratedArtifact] = []
    mappings: list[TestMapping] = []
    diagnostics: list[MigrationDiagnostic] = []
    seen_targets: dict[Path, SourceFile] = {}
    for source in sorted(files, key=lambda item: item.project_relative.as_posix()):
        target = _artifact_relative(source)
        if target.is_absolute() or ".." in target.parts:
            diagnostics.append(
                _diagnostic(
                    "UNIT013",
                    "generated unittest artifact path must stay inside migration output",
                    source,
                )
            )
            continue
        previous = seen_targets.get(target)
        if previous is not None:
            diagnostics.append(
                _diagnostic(
                    "UNIT013",
                    (
                        f"generated artifact path collides with "
                        f"{previous.project_relative.as_posix()!r}: {target.as_posix()}"
                    ),
                    source,
                )
            )
            continue
        seen_targets[target] = source
        artifact, source_mappings, source_diagnostics = _convert_source(
            source,
            output_relative=output_relative,
            manifest_sha256=manifest_sha256,
        )
        diagnostics.extend(source_diagnostics)
        if artifact is not None:
            artifacts.append(artifact)
            mappings.extend(source_mappings)

    if artifacts:
        artifacts.append(
            GeneratedArtifact(
                relative_path=_MANIFEST_PATH,
                content=manifest_content,
                source_files=tuple(manifest_hashes),
            )
        )

    return ConversionBundle(
        artifacts=tuple(sorted(artifacts, key=lambda item: item.relative_path.as_posix())),
        mappings=tuple(
            sorted(
                mappings,
                key=lambda item: (item.source_id, item.target_file, item.target_function),
            )
        ),
        diagnostics=tuple(
            sorted(
                diagnostics,
                key=lambda item: (
                    item.source,
                    item.line or 0,
                    item.code,
                    item.message,
                ),
            )
        ),
    )


__all__ = ["convert_unittest_suite", "detect_unittest_module"]
