"""State and inventory tools: processes, sensors, drivers, dxdiag, system info."""

from __future__ import annotations

from datetime import datetime

from pc_diag_mcp.collectors.base import CollectionManifest, Collector, collect
from pc_diag_mcp.collectors.parsers import PROCESS_SORT_KEYS
from pc_diag_mcp.models import (
    DriverInfo,
    DxDiagSummary,
    ProcessSnapshot,
    SensorSnapshot,
    SystemInfo,
    TimeWindow,
)
from pc_diag_mcp.tools.params import (
    DriverInventoryParams,
    ProcessSnapshotParams,
    SensorSnapshotParams,
    SortKey,
)
from pc_diag_mcp.tools.results import Result as _Result
from pc_diag_mcp.tools.results import UntrustedResult


class ProcessSnapshotResult(_Result):
    sort_by: SortKey
    snapshot: ProcessSnapshot
    collection: CollectionManifest


class SensorSnapshotResult(UntrustedResult):
    snapshots: list[SensorSnapshot]
    collection: CollectionManifest


class DriverInventoryResult(_Result):
    device_classes: list[str]
    drivers: list[DriverInfo]
    collection: CollectionManifest


class DxDiagResult(_Result):
    dxdiag: DxDiagSummary
    collection: CollectionManifest


class SystemInfoResult(_Result):
    system: SystemInfo
    collection: CollectionManifest


def process_snapshot(
    collector: Collector, top_n: int = 15, sort_by: SortKey = "working_set"
) -> ProcessSnapshotResult:
    """Memory and disk state plus the top processes by working set, page faults or I/O."""
    params = ProcessSnapshotParams(top_n=top_n, sort_by=sort_by)
    snapshot, manifest = collect(collector, lambda: collector.process_snapshot(params.top_n))
    ranked = sorted(snapshot.top_processes, key=PROCESS_SORT_KEYS[params.sort_by], reverse=True)
    return ProcessSnapshotResult(
        sort_by=params.sort_by,
        snapshot=snapshot.model_copy(update={"top_processes": ranked[: params.top_n]}),
        collection=manifest,
    )


def sensor_snapshot(
    collector: Collector, start: datetime | None = None, end: datetime | None = None
) -> SensorSnapshotResult:
    """Temperatures, clocks, loads and power. Live collection has no history: it returns one
    snapshot taken now. Replays return the recorded snapshots inside the window."""
    params = SensorSnapshotParams(start=start, end=end)
    window = (
        TimeWindow(start=params.start, end=params.end)
        if params.start is not None and params.end is not None
        else None
    )
    snapshots, manifest = collect(collector, lambda: collector.sensors(window))
    return SensorSnapshotResult(snapshots=snapshots, collection=manifest)


def driver_inventory(
    collector: Collector, device_classes: list[str] | None = None
) -> DriverInventoryResult:
    """Installed drivers for allowlisted device classes (DISPLAY, SYSTEM, SCSIADAPTER, HDC,
    NET, MEDIA). Any other class is rejected."""
    params = (
        DriverInventoryParams(device_classes=device_classes)
        if device_classes is not None
        else DriverInventoryParams()
    )
    classes = list(dict.fromkeys(c.upper() for c in params.device_classes))
    drivers, manifest = collect(collector, lambda: collector.drivers(classes))
    return DriverInventoryResult(device_classes=classes, drivers=drivers, collection=manifest)


def dxdiag_summary(collector: Collector) -> DxDiagResult:
    """DirectX version, display devices and any problems dxdiag reports (takes up to ~90 s)."""
    summary, manifest = collect(collector, collector.dxdiag)
    return DxDiagResult(dxdiag=summary, collection=manifest)


def system_info(collector: Collector) -> SystemInfoResult:
    """OS caption and build, GPUs with driver versions, and the local UTC offset."""
    info, manifest = collect(collector, collector.system_info)
    return SystemInfoResult(system=info, collection=manifest)
