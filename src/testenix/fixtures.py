"""Fixture registry and async-native dependency resolution for Testenix."""

from __future__ import annotations

import asyncio
import inspect
import traceback as traceback_module
import types
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Generator,
    Iterable,
    Iterator,
    Mapping,
)
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Union, get_args, get_origin, get_type_hints

from testenix.api import get_fixture_metadata
from testenix.builtin_fixtures import _monkeypatch_fixture, _tmp_path_fixture
from testenix.contracts import Scope


class FixtureError(RuntimeError):
    """Base class for authoring and runtime fixture errors."""


class FixtureRegistrationError(FixtureError):
    """Raised when fixture declarations are ambiguous or duplicated."""


class FixtureNotFoundError(FixtureError):
    """Raised when a required argument has no value or fixture provider."""


class FixtureAmbiguityError(FixtureError):
    """Raised when type-based resolution finds more than one provider."""


class FixtureCycleError(FixtureError):
    """Raised when the dependency graph contains a cycle."""


class FixtureScopeError(FixtureError):
    """Raised when a long-lived fixture depends on a shorter-lived fixture."""


class FixtureProtocolError(FixtureError):
    """Raised when a generator fixture yields zero or multiple values."""


_SCOPE_RANK = {Scope.TEST: 0, Scope.MODULE: 1, Scope.SESSION: 2}
_YIELD_ORIGINS = {
    Iterator,
    Iterable,
    Generator,
    AsyncIterator,
    AsyncGenerator,
}


def _safe_type_hints(function: Callable[..., Any]) -> Mapping[str, Any]:
    try:
        return get_type_hints(function, include_extras=True)
    except (NameError, TypeError):
        return {}


def _provided_type(function: Callable[..., Any]) -> Any:
    annotation = _safe_type_hints(function).get(
        "return", inspect.signature(function).return_annotation
    )
    if annotation is inspect.Signature.empty:
        return None
    origin = get_origin(annotation)
    if origin in _YIELD_ORIGINS and (
        inspect.isgeneratorfunction(function) or inspect.isasyncgenfunction(function)
    ):
        arguments = get_args(annotation)
        return arguments[0] if arguments else None
    if origin is Annotated:
        arguments = get_args(annotation)
        return arguments[0] if arguments else None
    return annotation


def _normalise_annotation(annotation: Any) -> Any:
    origin = get_origin(annotation)
    if origin is Annotated:
        arguments = get_args(annotation)
        return _normalise_annotation(arguments[0]) if arguments else annotation
    return annotation


def _annotations_match(requested: Any, provided: Any) -> bool:
    if requested is inspect.Signature.empty or provided is None:
        return False
    requested = _normalise_annotation(requested)
    provided = _normalise_annotation(provided)
    if requested == provided:
        return True
    if isinstance(requested, str) or isinstance(provided, str):
        requested_name = (
            requested if isinstance(requested, str) else getattr(requested, "__name__", "")
        )
        provided_name = provided if isinstance(provided, str) else getattr(provided, "__name__", "")
        return requested_name == provided_name
    requested_origin = get_origin(requested)
    if requested_origin in (Union, types.UnionType):
        return any(_annotations_match(option, provided) for option in get_args(requested))
    try:
        return (
            inspect.isclass(requested)
            and inspect.isclass(provided)
            and issubclass(provided, requested)
        )
    except TypeError:
        return False


def _next_generator(generator: Any) -> tuple[bool, Any]:
    try:
        return True, next(generator)
    except StopIteration:
        return False, None


@dataclass(frozen=True, slots=True)
class FixtureDefinition:
    """A validated fixture provider known to a registry."""

    name: str
    function: Callable[..., Any]
    scope: Scope
    module_name: str | None = None
    path: str | None = None
    provided_type: Any = None
    autouse: bool = False
    builtin: bool = False

    @property
    def key(self) -> str:
        owner = (
            f"{self.function.__module__}.{self.function.__qualname__}"
            if hasattr(self.function, "__qualname__")
            else self.path or self.module_name or "<global>"
        )
        return f"{owner}::{self.name}"


_BUILTIN_DEFINITIONS = {
    "monkeypatch": FixtureDefinition(
        name="monkeypatch",
        function=_monkeypatch_fixture,
        scope=Scope.TEST,
        builtin=True,
    ),
    "tmp_path": FixtureDefinition(
        name="tmp_path",
        function=_tmp_path_fixture,
        scope=Scope.TEST,
        builtin=True,
    ),
}


@dataclass(frozen=True, slots=True)
class TeardownFailure:
    """A single losslessly captured fixture finalization error."""

    fixture_name: str
    exception: BaseException
    traceback: str
    owner_test_id: str | None = None


@dataclass(frozen=True, slots=True)
class _Finalizer:
    fixture_name: str
    fixture_key: str
    callback: Callable[[], Any]


class FixtureRegistry:
    """A deterministic registry supporting name-first and type fallback lookup."""

    def __init__(self, definitions: Iterable[FixtureDefinition] = ()) -> None:
        self._definitions: list[FixtureDefinition] = []
        for definition in definitions:
            self.add(definition)

    @property
    def definitions(self) -> tuple[FixtureDefinition, ...]:
        return tuple(self._definitions)

    def add(self, definition: FixtureDefinition) -> None:
        duplicate = next(
            (
                current
                for current in self._definitions
                if current.name == definition.name and current.module_name == definition.module_name
            ),
            None,
        )
        if duplicate is not None:
            location = definition.module_name or "global registry"
            raise FixtureRegistrationError(
                f"fixture {definition.name!r} is declared more than once in {location}"
            )
        self._definitions.append(definition)

    def register(
        self,
        function: Callable[..., Any],
        *,
        module_name: str | None = None,
        path: str | Path | None = None,
        local_name: str | None = None,
    ) -> FixtureDefinition:
        metadata = get_fixture_metadata(function)
        if metadata is None:
            raise FixtureRegistrationError(
                f"{getattr(function, '__name__', function)!r} is not decorated with @fixture"
            )
        if local_name is not None and not local_name:
            raise FixtureRegistrationError("fixture local name must not be empty")
        name = metadata.name or local_name or function.__name__
        definition = FixtureDefinition(
            name=name,
            function=function,
            scope=metadata.scope,
            module_name=module_name,
            path=str(path) if path is not None else None,
            provided_type=_provided_type(function),
            autouse=metadata.autouse,
        )
        self.add(definition)
        return definition

    def merged(self, *registries: FixtureRegistry) -> FixtureRegistry:
        result = FixtureRegistry(self.definitions)
        for registry in registries:
            for definition in registry.definitions:
                if definition not in result._definitions:
                    result.add(definition)
        return result

    def find_for_parameter(
        self,
        parameter: inspect.Parameter,
        *,
        module_name: str | None,
        function: Callable[..., Any] | None = None,
    ) -> FixtureDefinition | None:
        visible = [
            definition
            for definition in self._definitions
            if definition.module_name in (None, module_name)
        ]
        named = [definition for definition in visible if definition.name == parameter.name]
        if named:
            local = [definition for definition in named if definition.module_name == module_name]
            return local[0] if local else named[0]

        # Native built-ins are name-only fallbacks. They deliberately do not
        # participate in type lookup (for example, an arbitrary ``Path``
        # parameter must not unexpectedly receive ``tmp_path``), and any user
        # fixture with the same visible name wins above.
        builtin = _BUILTIN_DEFINITIONS.get(parameter.name)
        if builtin is not None:
            return builtin

        annotation = parameter.annotation
        if function is not None:
            annotation = _safe_type_hints(function).get(parameter.name, annotation)
        typed = [
            definition
            for definition in visible
            if _annotations_match(annotation, definition.provided_type)
        ]
        local_typed = [definition for definition in typed if definition.module_name == module_name]
        candidates = local_typed or typed
        if len(candidates) > 1:
            names = ", ".join(definition.name for definition in candidates)
            raise FixtureAmbiguityError(
                f"parameter {parameter.name!r} matches multiple fixtures by type: {names}"
            )
        return candidates[0] if candidates else None

    def autouse_for(self, module_name: str | None) -> tuple[FixtureDefinition, ...]:
        """Return effective implicit fixtures for a module in setup order.

        A module-local definition replaces a global definition with the same
        name, even when the replacement is not autouse. This keeps normal
        fixture override rules intact instead of running both providers.
        """

        effective: dict[str, FixtureDefinition] = {}
        for definition in self._definitions:
            if definition.module_name is None:
                effective.setdefault(definition.name, definition)
        for definition in self._definitions:
            if definition.module_name == module_name:
                effective[definition.name] = definition
        return tuple(
            sorted(
                (definition for definition in effective.values() if definition.autouse),
                key=lambda definition: (
                    -_SCOPE_RANK[definition.scope],
                    definition.key,
                ),
            )
        )


class FixtureRuntime:
    """Own fixture caches and finalizers for one execution session.

    The runtime is intentionally async even for synchronous providers.  This
    gives sync and async tests one lifecycle model and lets dependencies mix
    both styles freely.
    """

    def __init__(self, registry: FixtureRegistry) -> None:
        self.registry = registry
        self._session_cache: dict[str, Any] = {}
        self._session_finalizers: list[_Finalizer] = []
        self._session_last_users: dict[str, str] = {}
        self._module_cache: dict[str, dict[str, Any]] = {}
        self._module_finalizers: dict[str, list[_Finalizer]] = {}
        self._module_last_users: dict[str, dict[str, str]] = {}
        self._module_order: list[str] = []
        self._test_cache: dict[str, Any] = {}
        self._test_finalizers: list[_Finalizer] = []
        self._active_test: str | None = None
        self._active_module: str | None = None
        self._resolution_stack: list[FixtureDefinition] = []
        self._dependency_keys: dict[str, set[str]] = {}
        self._fixture_scopes: dict[str, Scope] = {}

    def begin_test(self, test_id: str, module_name: str) -> None:
        if self._active_test is not None:
            raise FixtureError(
                f"cannot begin {test_id!r}; fixture runtime is still executing "
                f"{self._active_test!r}"
            )
        self._active_test = test_id
        self._active_module = module_name
        self._test_cache = {}
        self._test_finalizers = []

    async def resolve_arguments(
        self,
        function: Callable[..., Any],
        explicit: Mapping[str, Any] | None = None,
        *,
        module_name: str | None = None,
    ) -> dict[str, Any]:
        """Combine case parameters with recursively resolved fixture values."""

        if self._active_test is None:
            raise FixtureError("begin_test() must be called before resolving fixtures")
        effective_module = module_name or self._active_module
        kwargs = dict(explicit or {})
        signature = inspect.signature(function)
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        unexpected = set(kwargs).difference(signature.parameters)
        if unexpected and not accepts_kwargs:
            names = ", ".join(sorted(unexpected))
            raise FixtureError(f"unexpected case parameters for {function.__name__}: {names}")

        for autouse_definition in self.registry.autouse_for(effective_module):
            await self._resolve(autouse_definition, parent=None)

        for parameter in signature.parameters.values():
            if parameter.name in kwargs or parameter.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                continue
            definition = self.registry.find_for_parameter(
                parameter,
                module_name=effective_module,
                function=function,
            )
            if definition is not None:
                if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
                    raise FixtureError(
                        f"fixture-backed parameter {parameter.name!r} cannot be positional-only"
                    )
                kwargs[parameter.name] = await self._resolve(definition, parent=None)
            elif parameter.default is inspect.Parameter.empty:
                raise FixtureNotFoundError(
                    f"no case value or fixture found for required parameter {parameter.name!r} "
                    f"of {function.__name__}"
                )
        return kwargs

    async def _resolve(
        self,
        definition: FixtureDefinition,
        *,
        parent: FixtureDefinition | None,
    ) -> Any:
        self._fixture_scopes[definition.key] = definition.scope
        if parent is not None:
            self._fixture_scopes[parent.key] = parent.scope
            self._dependency_keys.setdefault(parent.key, set()).add(definition.key)
        if parent is not None and _SCOPE_RANK[definition.scope] < _SCOPE_RANK[parent.scope]:
            raise FixtureScopeError(
                f"{parent.scope.value}-scoped fixture {parent.name!r} cannot depend on "
                f"shorter-lived {definition.scope.value}-scoped fixture {definition.name!r}"
            )
        cache, finalizers = self._storage_for(definition.scope)
        if definition.key in cache:
            self._record_scope_usage(definition)
            return cache[definition.key]
        cycle_start = next(
            (
                index
                for index, current in enumerate(self._resolution_stack)
                if current.key == definition.key
            ),
            None,
        )
        if cycle_start is not None:
            cycle = self._resolution_stack[cycle_start:] + [definition]
            path = " -> ".join(item.name for item in cycle)
            raise FixtureCycleError(f"fixture dependency cycle: {path}")

        self._resolution_stack.append(definition)
        try:
            kwargs = await self._resolve_fixture_dependencies(definition)
            value, finalizer = await self._start_fixture(definition, kwargs)
            cache[definition.key] = value
            self._record_scope_usage(definition)
            if finalizer is not None:
                finalizers.append(_Finalizer(definition.name, definition.key, finalizer))
            return value
        finally:
            self._resolution_stack.pop()

    def _record_scope_usage(self, definition: FixtureDefinition) -> None:
        if self._active_test is None:
            return
        pending = [definition.key]
        visited: set[str] = set()
        while pending:
            key = pending.pop()
            if key in visited:
                continue
            visited.add(key)
            scope = self._fixture_scopes.get(key)
            if scope is Scope.SESSION:
                self._session_last_users[key] = self._active_test
            elif scope is Scope.MODULE and self._active_module is not None:
                self._module_last_users.setdefault(self._active_module, {})[key] = self._active_test
            pending.extend(self._dependency_keys.get(key, ()))

    async def _resolve_fixture_dependencies(self, definition: FixtureDefinition) -> dict[str, Any]:
        signature = inspect.signature(definition.function)
        kwargs: dict[str, Any] = {}
        for parameter in signature.parameters.values():
            if parameter.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                continue
            dependency = self.registry.find_for_parameter(
                parameter,
                module_name=definition.module_name,
                function=definition.function,
            )
            if dependency is None:
                if parameter.default is inspect.Parameter.empty:
                    raise FixtureNotFoundError(
                        f"fixture {definition.name!r} requires unknown fixture {parameter.name!r}"
                    )
                continue
            if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
                raise FixtureError(
                    f"fixture dependency {parameter.name!r} cannot be positional-only"
                )
            kwargs[parameter.name] = await self._resolve(dependency, parent=definition)
        return kwargs

    async def _start_fixture(
        self,
        definition: FixtureDefinition,
        kwargs: Mapping[str, Any],
    ) -> tuple[Any, Callable[[], Any] | None]:
        if inspect.iscoroutinefunction(definition.function) or inspect.isasyncgenfunction(
            definition.function
        ):
            result = definition.function(**kwargs)
        else:
            result = await asyncio.to_thread(definition.function, **dict(kwargs))
        if inspect.isawaitable(result):
            result = await result

        if inspect.isasyncgen(result):
            async_generator = result
            try:
                value = await anext(async_generator)
            except StopAsyncIteration as error:
                raise FixtureProtocolError(
                    f"async generator fixture {definition.name!r} did not yield a value"
                ) from error

            async def finish_async_generator() -> None:
                try:
                    extra = await anext(async_generator)
                except StopAsyncIteration:
                    return
                else:
                    raise FixtureProtocolError(
                        f"async generator fixture {definition.name!r} yielded more than once "
                        f"(extra value: {extra!r})"
                    )
                finally:
                    await async_generator.aclose()

            return value, finish_async_generator

        if inspect.isgenerator(result):
            generator = result
            yielded, value = await asyncio.to_thread(_next_generator, generator)
            if not yielded:
                raise FixtureProtocolError(
                    f"generator fixture {definition.name!r} did not yield a value"
                )

            async def finish_generator() -> None:
                yielded_extra, extra = await asyncio.to_thread(_next_generator, generator)
                try:
                    if yielded_extra:
                        raise FixtureProtocolError(
                            f"generator fixture {definition.name!r} yielded more than once "
                            f"(extra value: {extra!r})"
                        )
                finally:
                    await asyncio.to_thread(generator.close)

            return value, finish_generator

        return result, None

    def _storage_for(self, scope: Scope) -> tuple[dict[str, Any], list[_Finalizer]]:
        if scope is Scope.TEST:
            return self._test_cache, self._test_finalizers
        if scope is Scope.MODULE:
            if self._active_module is None:
                raise FixtureError("module-scoped fixture requested outside a test")
            if self._active_module not in self._module_cache:
                self._module_cache[self._active_module] = {}
                self._module_finalizers[self._active_module] = []
                self._module_last_users[self._active_module] = {}
                self._module_order.append(self._active_module)
            return (
                self._module_cache[self._active_module],
                self._module_finalizers[self._active_module],
            )
        return self._session_cache, self._session_finalizers

    async def finish_test(self) -> tuple[TeardownFailure, ...]:
        """Finalize all test-scoped fixtures, always attempting every cleanup."""

        failures = await self._run_finalizers(self._test_finalizers)
        self._test_cache.clear()
        self._test_finalizers.clear()
        self._active_test = None
        self._active_module = None
        self._resolution_stack.clear()
        return failures

    async def close_module(self, module_name: str) -> tuple[TeardownFailure, ...]:
        if self._active_test is not None:
            raise FixtureError("cannot close a module fixture scope during an active test")
        finalizers = self._module_finalizers.pop(module_name, [])
        owners = self._module_last_users.pop(module_name, {})
        failures = await self._run_finalizers(finalizers, owners=owners)
        self._module_cache.pop(module_name, None)
        if module_name in self._module_order:
            self._module_order.remove(module_name)
        return failures

    async def close_session(self) -> tuple[TeardownFailure, ...]:
        """Close remaining module scopes followed by the session scope."""

        if self._active_test is not None:
            raise FixtureError("cannot close the fixture session during an active test")
        failures: list[TeardownFailure] = []
        for module_name in reversed(tuple(self._module_order)):
            failures.extend(await self.close_module(module_name))
        failures.extend(
            await self._run_finalizers(
                self._session_finalizers,
                owners=self._session_last_users,
            )
        )
        self._session_cache.clear()
        self._session_finalizers.clear()
        self._session_last_users.clear()
        self._dependency_keys.clear()
        self._fixture_scopes.clear()
        return tuple(failures)

    async def _run_finalizers(
        self,
        finalizers: Iterable[_Finalizer],
        *,
        owners: Mapping[str, str] | None = None,
    ) -> tuple[TeardownFailure, ...]:
        failures: list[TeardownFailure] = []
        for finalizer in reversed(tuple(finalizers)):
            try:
                result = finalizer.callback()
                if isinstance(result, Awaitable) or inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                failures.append(
                    TeardownFailure(
                        fixture_name=finalizer.fixture_name,
                        exception=error,
                        traceback="".join(
                            traceback_module.format_exception(
                                type(error), error, error.__traceback__
                            )
                        ),
                        owner_test_id=(
                            None if owners is None else owners.get(finalizer.fixture_key)
                        ),
                    )
                )
        return tuple(failures)


__all__ = [
    "FixtureAmbiguityError",
    "FixtureCycleError",
    "FixtureDefinition",
    "FixtureError",
    "FixtureNotFoundError",
    "FixtureProtocolError",
    "FixtureRegistrationError",
    "FixtureRegistry",
    "FixtureRuntime",
    "FixtureScopeError",
    "TeardownFailure",
]
