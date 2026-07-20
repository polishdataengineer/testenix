"""Versioned event creation and an append-only JSONL event log.

The JSON representation intentionally uses a small, allow-listed encoder instead
of pickle.  Event payloads are allowed to contain domain dataclasses, enums and a
few common value objects; unknown objects are reduced to bounded diagnostic
metadata and are never imported or reconstructed while reading the log.
"""

from __future__ import annotations

import base64
import json
import math
import os
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import suppress
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, Self, runtime_checkable

from testenix.contracts import EVENT_SCHEMA_VERSION, Event, EventType

try:  # pragma: no cover - Windows has no fcntl; O_APPEND remains the fallback.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


_TYPE_KEY = "__testenix_type__"
_MAX_REPR = 2_000
_MAX_DEPTH = 50


class EventSerializationError(ValueError):
    """Raised when an event record cannot be encoded or decoded safely."""


class UnsupportedEventSchemaError(EventSerializationError):
    """Raised when a reader sees an event schema it does not understand."""


@runtime_checkable
class EventSink(Protocol):
    """Minimal sink contract used by runners, workers and adapters."""

    def emit(self, event: Event) -> Event:
        """Persist one immutable event and return it."""

        ...


def _qualified_name(value: object) -> str:
    value_type = type(value)
    return f"{value_type.__module__}.{value_type.__qualname__}"


def _bounded_repr(value: object) -> str:
    try:
        representation = repr(value)
    except Exception:  # pragma: no cover - deliberately defensive around user objects.
        return "<repr failed>"
    if len(representation) <= _MAX_REPR:
        return representation
    return f"{representation[:_MAX_REPR]}..."


def _json_safe(value: Any, *, seen: set[int] | None = None, depth: int = 0) -> Any:
    """Convert ``value`` to JSON data without executable deserialization hooks."""

    if depth > _MAX_DEPTH:
        return {_TYPE_KEY: "depth_limit"}
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return {_TYPE_KEY: "float", "value": str(value)}
    if isinstance(value, Enum):
        return _json_safe(value.value, seen=seen, depth=depth + 1)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return {
            _TYPE_KEY: "bytes",
            "base64": base64.b64encode(value).decode("ascii"),
        }
    if isinstance(value, BaseException):
        return {
            _TYPE_KEY: _qualified_name(value),
            "message": str(value),
        }

    tracked = isinstance(value, (Mapping, list, tuple, set, frozenset)) or is_dataclass(value)
    value_id = id(value)
    if tracked:
        if seen is None:
            seen = set()
        if value_id in seen:
            return {_TYPE_KEY: "cycle"}
        seen.add(value_id)

    try:
        if is_dataclass(value) and not isinstance(value, type):
            return {
                field.name: _json_safe(getattr(value, field.name), seen=seen, depth=depth + 1)
                for field in fields(value)
            }
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
                safe_key = key if isinstance(key, str) else str(key)
                if safe_key in result:
                    raise EventSerializationError(
                        f"payload contains colliding JSON object key {safe_key!r}"
                    )
                result[safe_key] = _json_safe(item, seen=seen, depth=depth + 1)
            return result
        if isinstance(value, (list, tuple)):
            return [_json_safe(item, seen=seen, depth=depth + 1) for item in value]
        if isinstance(value, (set, frozenset)):
            converted = [_json_safe(item, seen=seen, depth=depth + 1) for item in value]
            return sorted(converted, key=lambda item: json.dumps(item, sort_keys=True))
    finally:
        if tracked and seen is not None:
            seen.discard(value_id)

    # Unknown parameter values remain useful for diagnostics, but readers never
    # import their type or execute a reconstruction callback.
    return {_TYPE_KEY: _qualified_name(value), "repr": _bounded_repr(value)}


def json_safe(value: Any) -> Any:
    """Return an inert JSON-compatible diagnostic representation of a value."""

    return _json_safe(value)


def event_to_dict(event: Event) -> dict[str, Any]:
    """Return the stable JSON-compatible representation of an event."""

    return {
        "schema_version": event.schema_version,
        "event_id": event.event_id,
        "run_id": event.run_id,
        "event_type": event.event_type.value,
        "timestamp": event.timestamp,
        "sequence": event.sequence,
        "test_id": event.test_id,
        "attempt": event.attempt,
        "worker_id": event.worker_id,
        "payload": _json_safe(event.payload),
    }


def serialize_event(event: Event) -> str:
    """Serialize one event as a compact, deterministic JSON object."""

    try:
        return json.dumps(
            event_to_dict(event),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        if isinstance(error, EventSerializationError):
            raise
        raise EventSerializationError(f"cannot serialize event {event.event_id!r}") from error


def event_from_dict(
    data: Mapping[str, Any],
    *,
    supported_versions: frozenset[int] = frozenset({EVENT_SCHEMA_VERSION}),
) -> Event:
    """Decode an event from inert JSON data and validate its schema version."""

    try:
        schema_version = int(data["schema_version"])
    except (KeyError, TypeError, ValueError) as error:
        raise EventSerializationError("event has no valid schema_version") from error
    if schema_version not in supported_versions:
        raise UnsupportedEventSchemaError(
            f"unsupported event schema {schema_version}; supported: {sorted(supported_versions)}"
        )

    try:
        payload = data.get("payload", {})
        if not isinstance(payload, Mapping):
            raise TypeError("payload is not an object")
        event_id = str(data["event_id"])
        run_id = str(data["run_id"])
        timestamp = float(data["timestamp"])
        sequence = int(data["sequence"])
        attempt = None if data.get("attempt") is None else int(data["attempt"])
        if not event_id or not run_id:
            raise ValueError("event_id and run_id must not be empty")
        if not math.isfinite(timestamp):
            raise ValueError("timestamp must be finite")
        if sequence < 1:
            raise ValueError("sequence must be positive")
        if attempt is not None and attempt < 1:
            raise ValueError("attempt must be positive")
        return Event(
            event_id=event_id,
            run_id=run_id,
            event_type=EventType(str(data["event_type"])),
            timestamp=timestamp,
            sequence=sequence,
            schema_version=schema_version,
            test_id=None if data.get("test_id") is None else str(data["test_id"]),
            attempt=attempt,
            worker_id=None if data.get("worker_id") is None else str(data["worker_id"]),
            payload=dict(payload),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise EventSerializationError("invalid event record") from error


def deserialize_event(
    line: str | bytes,
    *,
    supported_versions: frozenset[int] = frozenset({EVENT_SCHEMA_VERSION}),
) -> Event:
    """Deserialize one JSONL line."""

    try:
        data = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise EventSerializationError("invalid event JSON") from error
    if not isinstance(data, Mapping):
        raise EventSerializationError("event JSON must be an object")
    return event_from_dict(data, supported_versions=supported_versions)


def safe_event_copy(event: Event) -> Event:
    """Return an inert, deeply independent copy of an event.

    ``Event`` is a frozen dataclass, but its contract intentionally keeps a
    JSON-shaped ``dict`` payload. A serialization round trip prevents one sink
    from mutating the payload observed by another sink without invoking pickle,
    imports or user-defined reconstruction hooks.
    """

    return deserialize_event(
        serialize_event(event),
        supported_versions=frozenset({event.schema_version}),
    )


class EventFactory:
    """Thread-safe producer of ordered, versioned events for one run."""

    def __init__(
        self,
        run_id: str,
        *,
        schema_version: int = EVENT_SCHEMA_VERSION,
        sequence_start: int = 0,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], object] | None = None,
    ) -> None:
        if not run_id:
            raise ValueError("run_id must not be empty")
        if schema_version < 1:
            raise ValueError("schema_version must be positive")
        if sequence_start < 0:
            raise ValueError("sequence_start must be non-negative")
        self.run_id = run_id
        self.schema_version = schema_version
        self._sequence = sequence_start
        self._clock = clock
        self._id_factory = id_factory
        self._lock = threading.Lock()

    @property
    def sequence(self) -> int:
        """The last allocated sequence number."""

        with self._lock:
            return self._sequence

    def create(
        self,
        event_type: EventType | str,
        *,
        test_id: str | None = None,
        attempt: int | None = None,
        worker_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        timestamp: float | None = None,
        event_id: str | None = None,
    ) -> Event:
        """Create the next event. Explicit timestamps are useful in replay tests."""

        resolved_type = event_type if isinstance(event_type, EventType) else EventType(event_type)
        if attempt is not None and attempt < 1:
            raise ValueError("attempt must be positive")
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
            if event_id is not None:
                resolved_id = str(event_id)
            elif self._id_factory is not None:
                resolved_id = str(self._id_factory())
            else:
                resolved_id = f"{self.run_id}:{sequence}"
            resolved_timestamp = float(timestamp if timestamp is not None else self._clock())
        if not resolved_id:
            raise ValueError("event_id must not be empty")
        if not math.isfinite(resolved_timestamp):
            raise ValueError("event timestamp must be finite")
        return Event(
            event_id=resolved_id,
            run_id=self.run_id,
            event_type=resolved_type,
            timestamp=resolved_timestamp,
            sequence=sequence,
            schema_version=self.schema_version,
            test_id=test_id,
            attempt=attempt,
            worker_id=worker_id,
            payload=dict(payload or {}),
        )

    # Small aliases make the factory pleasant for adapters without widening the
    # actual event contract.
    new = create
    make = create

    def emit(self, sink: EventSink, event_type: EventType | str, **values: Any) -> Event:
        """Create and immediately persist an event."""

        event = self.create(event_type, **values)
        return sink.emit(event)


class JsonlEventSink:
    """Multi-thread/process-safe append-only JSONL sink on local filesystems."""

    def __init__(self, path: str | os.PathLike[str], *, fsync: bool = False) -> None:
        self.path = Path(path)
        self.fsync = fsync
        self._thread_lock = threading.Lock()
        self._descriptor: int | None = None

    def emit(self, event: Event) -> Event:
        return self.emit_serialized(event, serialize_event(event))

    def emit_serialized(self, event: Event, serialized: str) -> Event:
        """Append a pre-serialized canonical event without encoding it again."""

        record = f"{serialized}\n".encode()
        with self._thread_lock:
            if self._descriptor is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
                self._descriptor = os.open(self.path, flags, 0o644)
            descriptor = self._descriptor
            try:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                view = memoryview(record)
                while view:
                    written = os.write(descriptor, view)
                    if written == 0:  # pragma: no cover - defensive OS failure path.
                        raise OSError("short write while appending event")
                    view = view[written:]
                if self.fsync:
                    os.fsync(descriptor)
            finally:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        return event

    append = emit
    write = emit

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        with self._thread_lock:
            descriptor = self._descriptor
            self._descriptor = None
            if descriptor is not None:
                os.close(descriptor)

    def __del__(self) -> None:  # pragma: no cover - interpreter cleanup fallback.
        with suppress(Exception):
            self.close()


class InMemoryEventSink:
    """A deterministic sink useful for embedding and tests."""

    def __init__(self) -> None:
        self._events: list[Event] = []
        self._lock = threading.Lock()

    def emit(self, event: Event) -> Event:
        with self._lock:
            self._events.append(event)
        return event

    append = emit
    write = emit

    @property
    def events(self) -> tuple[Event, ...]:
        with self._lock:
            return tuple(self._events)


class FanoutEventSink:
    """Emit canonical, isolated event copies to several sinks in one order.

    Calls are serialized so concurrent producers cannot observe a different
    ordering in individual sinks. Sink failures still propagate immediately;
    this helper provides isolation, not transactional delivery.
    """

    def __init__(self, *sinks: EventSink) -> None:
        self._sinks = tuple(sinks)
        self._lock = threading.Lock()

    def emit(self, event: Event) -> Event:
        canonical = serialize_event(event)
        supported_versions = frozenset({event.schema_version})
        with self._lock:
            for sink in self._sinks:
                isolated = deserialize_event(
                    canonical,
                    supported_versions=supported_versions,
                )
                sink.emit(isolated)
        return event

    append = emit
    write = emit

    @property
    def sinks(self) -> tuple[EventSink, ...]:
        return self._sinks


def read_events(
    path: str | os.PathLike[str],
    *,
    supported_versions: frozenset[int] = frozenset({EVENT_SCHEMA_VERSION}),
) -> Iterator[Event]:
    """Stream events from a JSONL log, reporting the failing line precisely."""

    with Path(path).open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                yield deserialize_event(line, supported_versions=supported_versions)
            except EventSerializationError as error:
                raise EventSerializationError(
                    f"invalid event at {path}:{line_number}: {error}"
                ) from error


def write_events(sink: EventSink, events: Iterable[Event]) -> None:
    """Append an iterable of events to a sink."""

    for event in events:
        sink.emit(event)


load_events = read_events


__all__ = [
    "EventFactory",
    "EventSerializationError",
    "EventSink",
    "FanoutEventSink",
    "InMemoryEventSink",
    "JsonlEventSink",
    "UnsupportedEventSchemaError",
    "deserialize_event",
    "event_from_dict",
    "event_to_dict",
    "json_safe",
    "load_events",
    "read_events",
    "safe_event_copy",
    "serialize_event",
    "write_events",
]
