"""The collector contract shared by the live (PowerShell) and replay backends.

Collectors return the shared models only. Provenance (which scripts or files were read, how
long it took, caveats such as "live sensors have no history") is recorded on the side through
``record_source``/``record_note`` and gathered by ``collect``, so tools can attach a
``CollectionManifest`` to every result without widening the collector methods.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from pc_diag_mcp.models import (
    DriverInfo,
    DxDiagSummary,
    EventQuery,
    EventRecord,
    ProcessSnapshot,
    SensorSnapshot,
    SystemInfo,
    TimeWindow,
)

CollectorSource = Literal["live", "replay"]


class CollectorError(RuntimeError):
    """A collection attempt failed (timeout, bad output, script error)."""


class CollectorUnavailable(CollectorError):
    """A data source is not present on this system, or needs elevation we will not request."""

    def __init__(self, source: str, reason: str) -> None:
        super().__init__(f"{source} unavailable: {reason}")
        self.source = source
        self.reason = reason


class CollectionManifest(BaseModel):
    """What a tool call actually read, so the user can audit it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: CollectorSource
    scripts: list[str] = Field(
        default_factory=list, description="Scripts, commands or replay files that were read."
    )
    queries: list[str] = Field(
        default_factory=list, description="Generated queries (e.g. Event Log FilterXml)."
    )
    elapsed_ms: int = Field(ge=0)
    notes: list[str] = Field(default_factory=list)


@runtime_checkable
class Collector(Protocol):
    """Read-only access to one machine's telemetry, live or replayed."""

    @property
    def source(self) -> CollectorSource: ...

    def events(self, query: EventQuery) -> list[EventRecord]:
        """Matching events, newest first, at most ``query.max_events``."""
        ...

    def process_snapshot(self, top_n: int) -> ProcessSnapshot:
        """Memory/disk state plus the union of the top ``top_n`` processes by working set,
        page faults and I/O bytes, so callers can re-rank by any of those keys."""
        ...

    def sensors(self, window: TimeWindow | None) -> list[SensorSnapshot]: ...

    def drivers(self, device_classes: list[str]) -> list[DriverInfo]: ...

    def system_info(self) -> SystemInfo: ...

    def now(self) -> datetime:
        """The host's current time, timezone-aware UTC. A replay answers with its recorded
        clock, so relative times ("tonight") resolve inside the scenario."""
        ...

    def dxdiag(self) -> DxDiagSummary: ...


@dataclass
class _Recorder:
    scripts: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


_current: ContextVar[_Recorder | None] = ContextVar("pc_diag_collection", default=None)


def _append_unique(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


def record_source(script: str) -> None:
    recorder = _current.get()
    if recorder is not None:
        _append_unique(recorder.scripts, script)


def record_query(query: str) -> None:
    recorder = _current.get()
    if recorder is not None:
        _append_unique(recorder.queries, query)


def record_note(note: str) -> None:
    recorder = _current.get()
    if recorder is not None:
        _append_unique(recorder.notes, note)


def collect[T](collector: Collector, call: Callable[[], T]) -> tuple[T, CollectionManifest]:
    """Run ``call`` and return its result with a manifest of everything it read."""
    recorder = _Recorder()
    token = _current.set(recorder)
    started = time.perf_counter()
    try:
        result = call()
    finally:
        _current.reset(token)
    manifest = CollectionManifest(
        source=collector.source,
        scripts=recorder.scripts,
        queries=recorder.queries,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        notes=recorder.notes,
    )
    return result, manifest
