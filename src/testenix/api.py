"""Public authoring API for native Testenix tests.

Decorators in this module deliberately do not wrap user callables.  They only
attach immutable metadata which keeps signatures, annotations and tracebacks
intact for discovery and fixture injection.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import product
from math import isfinite
from typing import Any, ParamSpec, Protocol, TypeVar, cast, overload

from testenix.contracts import Scope

P = ParamSpec("P")
R = TypeVar("R")
F = TypeVar("F", bound=Callable[..., Any])

TEST_METADATA_ATTR = "__testenix_test__"
FIXTURE_METADATA_ATTR = "__testenix_fixture__"
CASES_METADATA_ATTR = "__testenix_cases__"
SKIP_REASON_ATTR = "__testenix_skip_reason__"
XFAIL_REASON_ATTR = "__testenix_xfail_reason__"


@dataclass(frozen=True, slots=True)
class TestMetadata:
    """Author-provided metadata for one test function."""

    description: str | None = None
    tags: frozenset[str] = frozenset()
    timeout: float | None = None


@dataclass(frozen=True, slots=True)
class FixtureMetadata:
    """Author-provided metadata for one fixture provider."""

    scope: Scope = Scope.TEST
    name: str | None = None


@dataclass(frozen=True, slots=True)
class CaseDefinition:
    """One concrete parameter mapping attached to a test function."""

    parameters: Mapping[str, Any]
    id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", dict(self.parameters))

    def __call__(self, function: F) -> F:
        """Allow a case value to double as ``@case(...)`` decorator."""

        return _attach_cases(function, (self,))


def _normalise_tags(tags: Iterable[str] | str) -> frozenset[str]:
    values = (tags,) if isinstance(tags, str) else tuple(tags)
    if any(not isinstance(value, str) for value in values):
        raise TypeError("test tags must be strings")
    return frozenset(value.strip() for value in values if value.strip())


def _normalise_timeout(timeout: float | None) -> float | None:
    if timeout is None:
        return None
    value = float(timeout)
    if value <= 0 or not isfinite(value):
        raise ValueError("test timeout must be a finite number greater than zero")
    return value


@overload
def test(function: Callable[P, R], /) -> Callable[P, R]: ...


@overload
def test(
    description: str | None = None,
    /,
    *,
    tags: Iterable[str] | str = (),
    timeout: float | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def test(
    function_or_description: Callable[..., Any] | str | None = None,
    /,
    *,
    description: str | None = None,
    tags: Iterable[str] | str = (),
    timeout: float | None = None,
) -> Callable[..., Any]:
    """Mark a function as a native Testenix test.

    Supported forms are ``@test``, ``@test()``, ``@test("description")`` and
    ``@test(description="description", tags={...}, timeout=...)``.
    """

    function = function_or_description if callable(function_or_description) else None
    positional_description = (
        function_or_description if isinstance(function_or_description, str) else None
    )
    if positional_description is not None and description is not None:
        raise TypeError("test description was supplied twice")
    final_description = description if description is not None else positional_description
    if final_description is not None and not final_description.strip():
        raise ValueError("test description cannot be empty")
    metadata = TestMetadata(
        description=final_description,
        tags=_normalise_tags(tags),
        timeout=_normalise_timeout(timeout),
    )

    def decorate(target: F) -> F:
        if not callable(target):
            raise TypeError("@test can only decorate a callable")
        setattr(target, TEST_METADATA_ATTR, metadata)
        return target

    return decorate(function) if function is not None else decorate


@overload
def fixture(function: Callable[P, R], /) -> Callable[P, R]: ...


@overload
def fixture(
    function: None = None,
    /,
    *,
    scope: Scope | str = Scope.TEST,
    name: str | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def fixture(
    function: Callable[..., Any] | None = None,
    /,
    *,
    scope: Scope | str = Scope.TEST,
    name: str | None = None,
) -> Callable[..., Any]:
    """Declare a dependency provider with test, module or session lifetime."""

    try:
        normalised_scope = Scope(scope)
    except ValueError as error:
        choices = ", ".join(value.value for value in Scope)
        raise ValueError(f"unknown fixture scope {scope!r}; expected one of: {choices}") from error
    if name is not None and (not isinstance(name, str) or not name.strip()):
        raise ValueError("fixture name must be a non-empty string")
    metadata = FixtureMetadata(scope=normalised_scope, name=name)

    def decorate(target: F) -> F:
        if not callable(target):
            raise TypeError("@fixture can only decorate a callable")
        if hasattr(target, TEST_METADATA_ATTR):
            raise TypeError("a callable cannot be both a test and a fixture")
        setattr(target, FIXTURE_METADATA_ATTR, metadata)
        return target

    return decorate(function) if function is not None else decorate


def _attach_cases(function: F, definitions: Sequence[CaseDefinition]) -> F:
    existing = tuple(getattr(function, CASES_METADATA_ATTR, ()))
    setattr(function, CASES_METADATA_ATTR, (*existing, *definitions))
    return function


def case(
    values: Mapping[str, Any] | str | None = None,
    /,
    *,
    id: str | None = None,
    **parameters: Any,
) -> CaseDefinition:
    """Attach one concrete parameter case.

    Examples::

        @case(user="alice", expected=True)
        @case("anonymous", user=None, expected=False)
        @case({"id": 42}, id="record-42")
    """

    case_id: str | None
    mapping: Mapping[str, Any]
    if isinstance(values, str):
        if id is not None:
            raise TypeError("case id was supplied twice")
        case_id = values
        mapping = parameters
    elif values is None:
        case_id = id
        mapping = parameters
    elif isinstance(values, Mapping):
        case_id = id
        overlap = set(values).intersection(parameters)
        if overlap:
            names = ", ".join(sorted(overlap))
            raise TypeError(f"case parameters supplied twice: {names}")
        mapping = {**values, **parameters}
    else:
        raise TypeError("case expects a parameter mapping, a case id, or keyword parameters")
    if case_id is not None and (not isinstance(case_id, str) or not case_id.strip()):
        raise ValueError("case id must be a non-empty string")
    return CaseDefinition(mapping, case_id)


def cases(
    *definitions: CaseDefinition | Mapping[str, Any],
    ids: Sequence[str] | None = None,
    **dimensions: Iterable[Any],
) -> Callable[[F], F]:
    """Attach several cases, either explicitly or as a Cartesian product.

    ``@cases({"x": 1}, {"x": 2})`` declares explicit mappings, while
    ``@cases(role=["admin", "editor"], active=[True, False])`` creates the
    Cartesian product of the supplied dimensions.
    """

    if definitions and dimensions:
        raise TypeError("cases accepts explicit mappings or dimensions, not both")
    materialised: list[CaseDefinition]
    if definitions:
        materialised = [
            value if isinstance(value, CaseDefinition) else CaseDefinition(value)
            for value in definitions
        ]
    elif dimensions:
        names = tuple(dimensions)
        value_sets = [tuple(dimensions[name]) for name in names]
        materialised = [
            CaseDefinition(dict(zip(names, combination, strict=True)))
            for combination in product(*value_sets)
        ]
    else:
        raise ValueError("cases requires at least one case or parameter dimension")
    if ids is not None:
        case_ids = tuple(ids)
        if len(case_ids) != len(materialised):
            raise ValueError("the number of case ids must match the number of cases")
        materialised = [
            CaseDefinition(definition.parameters, case_id)
            for definition, case_id in zip(materialised, case_ids, strict=True)
        ]

    def decorate(function: F) -> F:
        return _attach_cases(function, materialised)

    return decorate


class _ReasonDecorator(Protocol):
    @overload
    def __call__(
        self,
        reason_or_function: F,
        /,
        *,
        reason: str | None = None,
        when: bool = True,
    ) -> F: ...

    @overload
    def __call__(
        self,
        reason_or_function: str | None = None,
        /,
        *,
        reason: str | None = None,
        when: bool = True,
    ) -> Callable[[F], F]: ...


def _reason_decorator(attribute: str, default_reason: str) -> _ReasonDecorator:
    def marker(
        reason_or_function: str | F | None = None,
        /,
        *,
        reason: str | None = None,
        when: bool = True,
    ) -> F | Callable[[F], F]:
        function = reason_or_function if callable(reason_or_function) else None
        positional_reason = reason_or_function if isinstance(reason_or_function, str) else None
        if positional_reason is not None and reason is not None:
            raise TypeError("reason was supplied twice")
        final_reason = reason if reason is not None else positional_reason or default_reason
        if not isinstance(final_reason, str) or not final_reason.strip():
            raise ValueError("reason must be a non-empty string")

        def decorate(target: F) -> F:
            if when:
                setattr(target, attribute, final_reason)
            return target

        return decorate(function) if function is not None else decorate

    return cast(_ReasonDecorator, marker)


skip = _reason_decorator(SKIP_REASON_ATTR, "marked as skipped")
xfail = _reason_decorator(XFAIL_REASON_ATTR, "marked as expected failure")


def get_test_metadata(function: Callable[..., Any]) -> TestMetadata | None:
    """Return attached test metadata without relying on private attribute names."""

    value = getattr(function, TEST_METADATA_ATTR, None)
    return value if isinstance(value, TestMetadata) else None


def get_fixture_metadata(function: Callable[..., Any]) -> FixtureMetadata | None:
    """Return attached fixture metadata without relying on private attribute names."""

    value = getattr(function, FIXTURE_METADATA_ATTR, None)
    return value if isinstance(value, FixtureMetadata) else None


def get_cases(function: Callable[..., Any]) -> tuple[CaseDefinition, ...]:
    """Return concrete cases in declaration order."""

    return tuple(getattr(function, CASES_METADATA_ATTR, ()))


__all__ = [
    "CaseDefinition",
    "FixtureMetadata",
    "TestMetadata",
    "case",
    "cases",
    "fixture",
    "get_cases",
    "get_fixture_metadata",
    "get_test_metadata",
    "skip",
    "test",
    "xfail",
]
