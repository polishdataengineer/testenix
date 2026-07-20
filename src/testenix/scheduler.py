"""Deterministic Longest Processing Time (LPT) scheduling for local workers."""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from testenix.contracts import TestSpec

T = TypeVar("T")


def _item_id(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, Mapping):
        value = item.get("id", item.get("test_id"))
    else:
        value = getattr(item, "id", getattr(item, "test_id", None))
    if value is None:
        raise TypeError(f"scheduled item {item!r} has no stable id or test_id")
    return str(value)


def _valid_duration(value: Any) -> float | None:
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(duration) or duration < 0:
        return None
    return duration


def _history_duration(value: Any) -> float | None:
    direct = _valid_duration(value)
    if direct is not None:
        return direct
    if isinstance(value, Mapping):
        for key in ("duration", "estimated_duration", "ewma"):
            if key in value:
                parsed = _valid_duration(value[key])
                if parsed is not None:
                    return parsed
    for attribute in ("duration", "estimated_duration", "ewma"):
        parsed = _valid_duration(getattr(value, attribute, None))
        if parsed is not None:
            return parsed
    return None


@dataclass(frozen=True, slots=True)
class Shard(Generic[T]):
    """One static worker assignment, ordered by descending estimated cost."""

    shard_id: int
    items: tuple[T, ...]
    estimated_duration: float

    @property
    def tests(self) -> tuple[T, ...]:
        """Domain-friendly alias used by the runner."""

        return self.items

    @property
    def test_ids(self) -> tuple[str, ...]:
        return tuple(_item_id(item) for item in self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.items)


ScheduledShard = Shard


def schedule_lpt(
    items: Sequence[T],
    shard_count: int,
    durations: Mapping[str, Any] | None = None,
    *,
    history: Mapping[str, Any] | None = None,
    default_duration: float | None = None,
    key: Callable[[T], str] | None = None,
) -> tuple[Shard[T], ...]:
    """Assign items with the LPT greedy algorithm.

    Items are sorted by decreasing historical duration, then by stable id. Each
    item goes to the currently lightest shard, with ``shard_id`` breaking load
    ties. The returned plan is therefore independent of input ordering.

    ``durations`` is the canonical argument; ``history`` is accepted as an
    ergonomic alias. If no explicit default is supplied, the median known
    duration is used (or ``1.0`` for a completely new suite).
    """

    if shard_count < 1:
        raise ValueError("shard_count must be at least 1")
    if durations is not None and history is not None and durations is not history:
        raise ValueError("pass durations or history, not both")
    duration_history = durations if durations is not None else (history or {})

    known = [
        parsed
        for value in duration_history.values()
        if (parsed := _history_duration(value)) is not None
    ]
    if default_duration is None:
        fallback = float(statistics.median(known)) if known else 1.0
    else:
        parsed_default = _valid_duration(default_duration)
        if parsed_default is None:
            raise ValueError("default_duration must be finite and non-negative")
        fallback = parsed_default

    identify = key or _item_id
    annotated: list[tuple[float, str, T]] = []
    seen_ids: set[str] = set()
    for item in items:
        item_id = str(identify(item))
        if not item_id:
            raise ValueError("scheduled item id must not be empty")
        if item_id in seen_ids:
            raise ValueError(f"duplicate scheduled item id {item_id!r}")
        seen_ids.add(item_id)
        estimate = _history_duration(duration_history.get(item_id))
        annotated.append((fallback if estimate is None else estimate, item_id, item))

    annotated.sort(key=lambda entry: (-entry[0], entry[1]))
    shard_items: list[list[T]] = [[] for _ in range(shard_count)]
    shard_loads = [0.0] * shard_count
    for estimate, _, item in annotated:
        shard_id = min(range(shard_count), key=lambda index: (shard_loads[index], index))
        shard_items[shard_id].append(item)
        shard_loads[shard_id] += estimate

    return tuple(
        Shard(
            shard_id=shard_id,
            items=tuple(shard_items[shard_id]),
            estimated_duration=shard_loads[shard_id],
        )
        for shard_id in range(shard_count)
    )


@dataclass(frozen=True, slots=True)
class LPTScheduler:
    """Reusable scheduling policy configured with duration history."""

    durations: Mapping[str, Any] = field(default_factory=dict)
    default_duration: float | None = None

    def schedule(
        self,
        items: Sequence[T],
        shard_count: int,
        *,
        key: Callable[[T], str] | None = None,
    ) -> tuple[Shard[T], ...]:
        return schedule_lpt(
            items,
            shard_count,
            self.durations,
            default_duration=self.default_duration,
            key=key,
        )

    plan = schedule


def schedule_tests(
    tests: Sequence[TestSpec],
    workers: int,
    history: Mapping[str, Any] | None = None,
) -> tuple[Shard[TestSpec], ...]:
    """Typed convenience wrapper for the common runner path."""

    return schedule_lpt(tests, workers, history)


lpt_schedule = schedule_lpt


__all__ = [
    "LPTScheduler",
    "ScheduledShard",
    "Shard",
    "lpt_schedule",
    "schedule_lpt",
    "schedule_tests",
]
