"""Turn a ``Diagnosis`` into timeline evidence and an ``Incident``, deterministically.

The model cites digest ids (``E3``) that only exist inside its run, so the timeline's
evidence is rebuilt from the collector for the diagnosis window instead:

* ``ev:<record_id>``: every TDR/crash/WHEA signature hit (``list_tdr_incidents``)
* ``sensor:<hardware>/<sensor>:<kind>@<time>``: one series per unit, GPU sensors first
* ``frametime:<n>``: the worst frame in each second of a replay's ``frametimes.json``
* ``driver:<device_name>``: the display driver(s)
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter

from pc_diag_mcp.collectors.base import Collector, CollectorUnavailable
from pc_diag_mcp.models import (
    Diagnosis,
    EvidenceItem,
    EvidenceKind,
    FrameTimeSample,
    Incident,
    ReplayScenario,
    RunStats,
    SensorReading,
    SystemInfo,
    TimeWindow,
)
from pc_diag_mcp.signatures import Severity
from pc_diag_mcp.tools import driver_inventory, list_tdr_incidents, sensor_snapshot, system_info

ItemSeverity = Literal["info", "warning", "error", "critical"]

_SEVERITY: dict[Severity, ItemSeverity] = {
    "critical": "critical",
    "high": "error",
    "medium": "warning",
    "low": "info",
}
_CHART_KINDS = ("temperature", "power", "clock", "load")
_GPU_WORDS = ("gpu", "geforce", "radeon", "arc", "graphics")
# The synthetic generator records the capture start in the scenario description.
_FRAMETIME_ANCHOR = re.compile(r"Frame-time capture \(frametimes\.json\) starts at (\S+?)Z\.")
_FRAMES = TypeAdapter(list[FrameTimeSample])
_ID_MAX = 128


@dataclass(frozen=True)
class FrameTimes:
    start: datetime
    samples: list[FrameTimeSample]


def load_frametimes(replay_dir: Path) -> FrameTimes | None:
    """``frametimes.json`` with its absolute start time, or None if either is missing."""
    frames, scenario = replay_dir / "frametimes.json", replay_dir / "scenario.json"
    if not frames.is_file() or not scenario.is_file():
        return None
    description = ReplayScenario.model_validate_json(scenario.read_bytes()).description
    match = _FRAMETIME_ANCHOR.search(description)
    if match is None:
        return None
    start = datetime.fromisoformat(match.group(1)).replace(tzinfo=UTC)
    return FrameTimes(start=start, samples=_FRAMES.validate_json(frames.read_bytes()))


def _event_items(window: TimeWindow, collector: Collector) -> list[EvidenceItem]:
    result = list_tdr_incidents(collector, window.start, window.end)
    hits = sorted(
        (hit for incident in result.incidents for hit in incident.signals),
        key=lambda h: (h.time, h.log, h.record_id),
    )
    items: list[EvidenceItem] = []
    seen: set[str] = set()
    for hit in hits:
        item_id = f"ev:{hit.record_id}"
        if item_id in seen:  # record ids are per log
            item_id = f"ev:{hit.log}:{hit.record_id}"
        if item_id in seen:
            continue
        seen.add(item_id)
        items.append(
            EvidenceItem(
                id=item_id,
                kind=EvidenceKind.EVENT,
                time=hit.time,
                label=f"{hit.provider} {hit.event_id}: {hit.meaning}",
                detail=f"{hit.log} log, record {hit.record_id}. {hit.detail}",
                severity=_SEVERITY[hit.severity],
            )
        )
    return items


def _is_gpu(hardware: str, system: SystemInfo) -> bool:
    lowered = hardware.lower()
    return any(g.name == hardware for g in system.gpus) or any(w in lowered for w in _GPU_WORDS)


def _sensor_severity(reading: SensorReading) -> ItemSeverity:
    if reading.kind == "temperature":
        if reading.value >= 100:
            return "error"
        if reading.value >= 90:
            return "warning"
    return "info"


def _sensor_items(
    window: TimeWindow, collector: Collector, system: SystemInfo
) -> list[EvidenceItem]:
    try:
        snapshots = sensor_snapshot(collector, window.start, window.end).snapshots
    except CollectorUnavailable:
        return []
    series: dict[tuple[str, str, str], list[tuple[datetime, SensorReading]]] = defaultdict(list)
    for snap in snapshots:
        if not window.start <= snap.taken_at <= window.end:
            continue  # live collection ignores the window and returns "now"
        for reading in snap.readings:
            if reading.kind in _CHART_KINDS:
                series[(reading.hardware, reading.sensor, reading.unit)].append(
                    (snap.taken_at, reading)
                )
    # The timeline draws one line per unit, so keep the most telling series for each unit.
    best: dict[str, tuple[str, str, str]] = {}
    for key, points in sorted(series.items()):
        unit = key[2]
        rank = (_is_gpu(key[0], system), max(r.value for _, r in points))
        current = best.get(unit)
        if current is None or rank > (
            _is_gpu(current[0], system),
            max(r.value for _, r in series[current]),
        ):
            best[unit] = key
    items: list[EvidenceItem] = []
    for hardware, sensor, unit in sorted(best.values()):
        for taken_at, reading in series[(hardware, sensor, unit)]:
            items.append(
                EvidenceItem(
                    id=f"sensor:{hardware}/{sensor}:{reading.kind}@{taken_at:%Y%m%dT%H%M%SZ}",
                    kind=EvidenceKind.SENSOR,
                    time=taken_at,
                    label=f"{hardware} {sensor} {reading.kind}",
                    detail=f"{reading.kind} from {reading.source}",
                    severity=_sensor_severity(reading),
                    value=reading.value,
                    unit=unit,
                )
            )
    return items


def _frametime_items(window: TimeWindow, frames: FrameTimes) -> list[EvidenceItem]:
    worst: dict[int, FrameTimeSample] = {}
    for sample in frames.samples:
        second = int(sample.time_offset_ms // 1000)
        if second not in worst or sample.frame_time_ms > worst[second].frame_time_ms:
            worst[second] = sample
    items: list[EvidenceItem] = []
    for second, sample in sorted(worst.items()):
        time = frames.start + timedelta(seconds=second)
        if not window.start <= time <= window.end:
            continue
        ft = sample.frame_time_ms
        busy = f"; GPU busy {sample.gpu_busy_ms:.1f} ms" if sample.gpu_busy_ms is not None else ""
        items.append(
            EvidenceItem(
                id=f"frametime:{second:04d}",
                kind=EvidenceKind.FRAMETIME,
                time=time,
                label="Frame time (worst in 1 s)",
                detail=f"Frame at +{sample.time_offset_ms:.0f} ms into the capture{busy}.",
                severity="error" if ft >= 100 else "warning" if ft >= 50 else "info",
                value=ft,
                unit="ms",
            )
        )
    return items


def _driver_items(
    window: TimeWindow, collector: Collector, system: SystemInfo
) -> list[EvidenceItem]:
    items: dict[str, EvidenceItem] = {}
    try:
        drivers = driver_inventory(collector, ["DISPLAY"]).drivers
    except CollectorUnavailable:
        drivers = []
    for d in drivers:
        date = f" ({d.date})" if d.date else ""
        signed = {True: "signed", False: "unsigned", None: "signature unknown"}[d.signed]
        items.setdefault(
            f"driver:{d.device_name}",
            EvidenceItem(
                id=f"driver:{d.device_name}",
                kind=EvidenceKind.DRIVER,
                time=window.start,
                label=f"{d.device_name} {d.version}{date}",
                detail=f"Display driver from {d.provider}, {d.inf_name or 'unknown INF'}, "
                f"{signed}.",
            ),
        )
    if not items:  # no driver inventory: fall back to what system_info knows
        for gpu in system.gpus:
            date = f" ({gpu.driver_date})" if gpu.driver_date else ""
            items.setdefault(
                f"driver:{gpu.name}",
                EvidenceItem(
                    id=f"driver:{gpu.name}",
                    kind=EvidenceKind.DRIVER,
                    time=window.start,
                    label=f"{gpu.name} {gpu.driver_version}{date}",
                    detail="Display driver version reported by system_info.",
                ),
            )
    return list(items.values())


def build_evidence(
    diagnosis: Diagnosis, collector: Collector, *, frametimes: FrameTimes | None = None
) -> list[EvidenceItem]:
    """Evidence for ``diagnosis.window``, sorted by time then id; ids are unique."""
    window = diagnosis.window
    system = system_info(collector).system
    items = [
        *_event_items(window, collector),
        *_sensor_items(window, collector, system),
        *(_frametime_items(window, frametimes) if frametimes else []),
        *_driver_items(window, collector, system),
    ]
    return sorted(items, key=lambda i: (i.time, i.id))


def incident_id(diagnosis: Diagnosis) -> str:
    """``<title slug>-<occurred_at>``, within the timeline's URL-safe id alphabet."""
    stamp = f"{diagnosis.bug_report.occurred_at.astimezone(UTC):%Y%m%dT%H%M%SZ}"
    slug = re.sub(r"[^a-z0-9]+", "-", diagnosis.bug_report.title.lower()).strip("-")
    slug = slug[: _ID_MAX - len(stamp) - 1].rstrip("-") or "incident"
    return f"{slug}-{stamp}"


def build_incident(
    question: str,
    diagnosis: Diagnosis,
    collector: Collector,
    *,
    run: RunStats | None = None,
    created_at: datetime | None = None,
    replay_dir: Path | None = None,
) -> Incident:
    frametimes = load_frametimes(replay_dir) if replay_dir is not None else None
    return Incident(
        id=incident_id(diagnosis),
        question=question,
        created_at=created_at or datetime.now(UTC),
        system=system_info(collector).system,
        diagnosis=diagnosis,
        evidence=build_evidence(diagnosis, collector, frametimes=frametimes),
        run=run,
    )
