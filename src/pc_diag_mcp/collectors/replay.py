"""Replay collector: serves a recorded directory through the same Collector protocol.

Layout (see ``ReplayScenario``)::

    scenario.json          ReplayScenario (required)
    events.json            list[EventRecord] (required)
    process_snapshot.json  ProcessSnapshot (optional)
    sensors.json           list[SensorSnapshot], time-ordered (optional)
    drivers.json           list[DriverInfo] (optional)
    dxdiag.json            DxDiagSummary (optional)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TypeVar

from pydantic import TypeAdapter, ValidationError

from pc_diag_mcp.collectors.base import (
    CollectorError,
    CollectorSource,
    CollectorUnavailable,
    record_note,
    record_source,
)
from pc_diag_mcp.collectors.parsers import top_process_union
from pc_diag_mcp.models import (
    DriverInfo,
    DxDiagSummary,
    EventLevel,
    EventQuery,
    EventRecord,
    ProcessSnapshot,
    ReplayScenario,
    SensorSnapshot,
    SystemInfo,
    TimeWindow,
)

T = TypeVar("T")

_EVENTS = TypeAdapter(list[EventRecord])
_SENSORS = TypeAdapter(list[SensorSnapshot])
_DRIVERS = TypeAdapter(list[DriverInfo])


class ReplayCollector:
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        if not self._path.is_dir():
            raise CollectorError(f"replay directory not found: {self._path}")
        self.scenario = self._load("scenario.json", TypeAdapter(ReplayScenario), required=True)
        self._events = self._load("events.json", _EVENTS, required=True)

    @property
    def source(self) -> CollectorSource:
        return "replay"

    def _load(self, name: str, adapter: TypeAdapter[T], *, required: bool = False) -> T:
        file = self._path / name
        if not file.is_file():
            if required:
                raise CollectorError(f"replay directory {self._path} is missing {name}")
            raise CollectorUnavailable(f"replay:{name}", f"{self._path.name} has no {name}")
        try:
            return adapter.validate_json(file.read_bytes())
        except ValidationError as exc:
            raise CollectorError(f"invalid {name} in {self._path}: {exc}") from exc

    def events(self, query: EventQuery) -> list[EventRecord]:
        record_source("replay:events.json")
        logs = {log.lower() for log in query.logs}
        providers = {p.lower() for p in query.providers} if query.providers else None
        ids = set(query.event_ids) if query.event_ids else None
        levels = {EventLevel(level) for level in query.levels} if query.levels else None
        matched = [
            e
            for e in self._events
            if query.window.start <= e.time <= query.window.end
            and e.log.lower() in logs
            and (providers is None or e.provider.lower() in providers)
            and (ids is None or e.event_id in ids)
            and (levels is None or e.level in levels)
        ]
        matched.sort(key=lambda e: (e.time, e.record_id), reverse=True)
        return matched[: query.max_events]

    def process_snapshot(self, top_n: int) -> ProcessSnapshot:
        record_source("replay:process_snapshot.json")
        snapshot = self._load("process_snapshot.json", TypeAdapter(ProcessSnapshot))
        return snapshot.model_copy(
            update={"top_processes": top_process_union(snapshot.top_processes, top_n)}
        )

    def sensors(self, window: TimeWindow | None) -> list[SensorSnapshot]:
        record_source("replay:sensors.json")
        loaded: list[SensorSnapshot] = self._load("sensors.json", _SENSORS)
        snapshots = sorted(loaded, key=lambda s: s.taken_at)
        if window is None:
            # "Now" in a replay is the scenario's clock: the latest snapshot not after it.
            current = [s for s in snapshots if s.taken_at <= self.scenario.now]
            record_note("no window given: returned the latest snapshot at the scenario's 'now'")
            return current[-1:]
        return [s for s in snapshots if window.start <= s.taken_at <= window.end]

    def drivers(self, device_classes: list[str]) -> list[DriverInfo]:
        record_source("replay:drivers.json")
        wanted = {c.upper() for c in device_classes}
        return [d for d in self._load("drivers.json", _DRIVERS) if d.device_class.upper() in wanted]

    def now(self) -> datetime:
        return self.scenario.now

    def system_info(self) -> SystemInfo:
        record_source("replay:scenario.json")
        return self.scenario.system

    def dxdiag(self) -> DxDiagSummary:
        record_source("replay:dxdiag.json")
        return self._load("dxdiag.json", TypeAdapter(DxDiagSummary))
