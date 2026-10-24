"""State and inventory tools: processes, sensors, drivers, dxdiag, system info."""

from __future__ import annotations

from datetime import datetime

from pc_diag_mcp.collectors.base import CollectionManifest, Collector, collect
from pc_diag_mcp.collectors.eventxml import sanitize_label, sanitize_text
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
from pc_diag_mcp.tools.results import UntrustedResult


class ProcessSnapshotResult(UntrustedResult):
    sort_by: SortKey
    snapshot: ProcessSnapshot
    collection: CollectionManifest


class SensorSnapshotResult(UntrustedResult):
    snapshots: list[SensorSnapshot]
    collection: CollectionManifest


class DriverInventoryResult(UntrustedResult):
    device_classes: list[str]
    drivers: list[DriverInfo]
    collection: CollectionManifest


class DxDiagResult(UntrustedResult):
    dxdiag: DxDiagSummary
    collection: CollectionManifest


class SystemInfoResult(UntrustedResult):
    system: SystemInfo
    collection: CollectionManifest


def _optional(value: str | None) -> str | None:
    return sanitize_label(value) if value is not None else None


def _sanitize_driver(driver: DriverInfo) -> DriverInfo:
    """Device and provider names come from driver packages: untrusted text."""
    return driver.model_copy(
        update={
            "device_name": sanitize_label(driver.device_name),
            "device_class": sanitize_label(driver.device_class),
            "provider": sanitize_label(driver.provider),
            "version": sanitize_label(driver.version),
            "date": _optional(driver.date),
            "inf_name": _optional(driver.inf_name),
        }
    )


def _sanitize_system(info: SystemInfo) -> SystemInfo:
    gpus = [
        gpu.model_copy(
            update={
                "name": sanitize_label(gpu.name),
                "driver_version": sanitize_label(gpu.driver_version),
                "driver_date": _optional(gpu.driver_date),
            }
        )
        for gpu in info.gpus
    ]
    return info.model_copy(
        update={
            "os_caption": sanitize_label(info.os_caption),
            "os_build": sanitize_label(info.os_build),
            "gpus": gpus,
        }
    )


def _sanitize_dxdiag(summary: DxDiagSummary) -> DxDiagSummary:
    devices = [
        device.model_copy(
            update={
                "name": sanitize_label(device.name),
                "manufacturer": sanitize_label(device.manufacturer),
                "driver_version": sanitize_label(device.driver_version),
                "driver_date": _optional(device.driver_date),
                "feature_levels": [sanitize_label(level) for level in device.feature_levels],
            }
        )
        for device in summary.display_devices
    ]
    return summary.model_copy(
        update={
            "directx_version": sanitize_label(summary.directx_version),
            "display_devices": devices,
            "problems": [sanitize_text(problem) for problem in summary.problems],
        }
    )


def process_snapshot(
    collector: Collector, top_n: int = 15, sort_by: SortKey = "working_set"
) -> ProcessSnapshotResult:
    """Memory and disk state plus the top processes by working set, page faults or I/O."""
    params = ProcessSnapshotParams(top_n=top_n, sort_by=sort_by)
    snapshot, manifest = collect(collector, lambda: collector.process_snapshot(params.top_n))
    ranked = sorted(snapshot.top_processes, key=PROCESS_SORT_KEYS[params.sort_by], reverse=True)
    # Process names are chosen by whoever started the process: untrusted text.
    ranked = [p.model_copy(update={"name": sanitize_label(p.name)}) for p in ranked]
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
    drivers = [_sanitize_driver(d) for d in drivers]
    return DriverInventoryResult(device_classes=classes, drivers=drivers, collection=manifest)


def dxdiag_summary(collector: Collector) -> DxDiagResult:
    """DirectX version, display devices and any problems dxdiag reports (takes up to ~90 s)."""
    summary, manifest = collect(collector, collector.dxdiag)
    summary = _sanitize_dxdiag(summary)
    return DxDiagResult(dxdiag=summary, collection=manifest)


def system_info(collector: Collector) -> SystemInfoResult:
    """OS caption and build, GPUs with driver versions, and the local UTC offset."""
    info, manifest = collect(collector, collector.system_info)
    info = _sanitize_system(info)
    return SystemInfoResult(system=info, collection=manifest)
