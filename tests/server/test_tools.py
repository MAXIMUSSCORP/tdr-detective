from __future__ import annotations

import json
import os
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from pc_diag_mcp.collectors import ReplayCollector
from pc_diag_mcp.collectors.base import Collector
from pc_diag_mcp.collectors.eventxml import parse_event
from pc_diag_mcp.models import (
    DriverInfo,
    DxDiagSummary,
    EventLevel,
    EventQuery,
    EventRecord,
    GpuVendor,
    MemoryState,
    ProcessInfo,
    ProcessSnapshot,
    SensorSnapshot,
    SystemInfo,
    TimeWindow,
)
from pc_diag_mcp.signatures import match_event
from pc_diag_mcp.tools import (
    DEVICE_CLASS_ALLOWLIST,
    QueryEventsResult,
    SavedEventsResult,
    describe_collection,
    driver_inventory,
    dxdiag_summary,
    list_tdr_incidents,
    process_snapshot,
    query_events,
    sensor_snapshot,
    system_info,
)
from pc_diag_mcp.tools.describe import TOOL_NAMES
from pc_diag_mcp.tools.events import cluster_hits

from .conftest import sample

T0 = datetime(2026, 10, 6, 5, 0, tzinfo=UTC)


def rec(record_id: int, minutes: float, provider: str, event_id: int, **kw: object) -> EventRecord:
    fields: dict[str, object] = {
        "record_id": record_id,
        "time": T0 + timedelta(minutes=minutes),
        "log": "System",
        "provider": provider,
        "event_id": event_id,
        "level": EventLevel.ERROR,
        "message": "",
        "data": {},
    }
    fields.update(kw)
    return EventRecord.model_validate(fields)


class StubCollector:
    """Records the queries it receives; serves canned data."""

    def __init__(self, events: list[EventRecord] | None = None) -> None:
        self.queries: list[EventQuery] = []
        self._events = events or []
        self.sensor_windows: list[TimeWindow | None] = []
        self.driver_classes: list[list[str]] = []

    @property
    def source(self) -> str:
        return "replay"

    def events(self, query: EventQuery) -> list[EventRecord]:
        self.queries.append(query)
        replay_like = [
            e
            for e in self._events
            if query.window.start <= e.time <= query.window.end
            and e.log in query.logs
            and (query.providers is None or e.provider in query.providers)
            and (query.event_ids is None or e.event_id in query.event_ids)
            and (query.levels is None or e.level in query.levels)
        ]
        return sorted(replay_like, key=lambda e: e.time, reverse=True)[: query.max_events]

    def process_snapshot(self, top_n: int) -> ProcessSnapshot:
        procs = [
            ProcessInfo(
                pid=i,
                name=f"p{i}",
                working_set_bytes=i * 10,
                private_bytes=0,
                page_faults=1000 - i,
                read_bytes=(i % 3) * 100,
                write_bytes=0,
                cpu_seconds=0,
            )
            for i in range(1, 30)
        ]
        return ProcessSnapshot(
            taken_at=T0,
            memory=MemoryState(
                total_bytes=1, available_bytes=1, commit_used_bytes=1, commit_limit_bytes=1
            ),
            disks=[],
            top_processes=procs,
        )

    def sensors(self, window: TimeWindow | None) -> list[SensorSnapshot]:
        self.sensor_windows.append(window)
        return [SensorSnapshot(taken_at=T0, readings=[])]

    def drivers(self, device_classes: list[str]) -> list[DriverInfo]:
        self.driver_classes.append(device_classes)
        return []

    def system_info(self) -> SystemInfo:
        return SystemInfo(os_caption="W", os_build="1", gpus=[], utc_offset_minutes=0)

    def dxdiag(self) -> DxDiagSummary:
        return DxDiagSummary(directx_version="DirectX 12", display_devices=[])


def test_stub_satisfies_protocol() -> None:
    assert isinstance(StubCollector(), Collector)


def sample_records() -> list[EventRecord]:
    return [parse_event(e["xml"], e["message"]) for e in sample("events_raw.json")]


def test_query_events_counts_and_validation() -> None:
    records = sample_records()
    stub = StubCollector(records)
    result = query_events(
        stub,
        "2026-01-01T00:00:00Z",  # type: ignore[arg-type]  # ISO strings, as the skill sends them
        "2026-10-23T00:00:00+00:00",  # type: ignore[arg-type]
        levels=["critical", "error", "warning"],  # type: ignore[list-item]
        max_events=100,
    )
    assert isinstance(result, QueryEventsResult)
    assert result.count == len(result.events) > 0
    kp = next(c for c in result.counts_by_provider_id if c.event_id == 41)
    assert kp.provider == "Microsoft-Windows-Kernel-Power" and kp.count == 3
    assert stub.queries[0].logs == ["System", "Application"]
    assert result.collection.source == "replay"

    for bad in (
        {"providers": ["x' or 1=1"]},
        {"event_ids": [70000]},
        {"max_events": 0},
        {"logs": ["Security"]},
        {"save_to": "disk"},
    ):
        with pytest.raises(ValidationError):
            query_events(stub, T0, T0 + timedelta(hours=1), **bad)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        query_events(stub, T0 + timedelta(hours=1), T0)
    with pytest.raises(ValidationError):
        query_events(stub, datetime(2026, 1, 1), datetime(2026, 1, 2))  # naive


def test_query_events_tempfile_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    records = sample_records()
    result = query_events(
        StubCollector(records),
        T0 - timedelta(days=200),
        T0 + timedelta(days=30),
        providers=["Windows Error Reporting"],
        event_ids=[1001],
        save_to="tempfile",
    )
    assert isinstance(result, SavedEventsResult)
    assert "events" not in result.model_dump()
    path = Path(result.path)
    assert path.parent == tmp_path / "tdr-detective"
    assert path.name.startswith("events-") and path.suffix == ".json"
    saved = [EventRecord.model_validate(e) for e in json.loads(path.read_text(encoding="utf-8"))]
    assert len(saved) == result.count == 3
    assert {r.provider for r in saved} == {"Windows Error Reporting"}
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700


def test_query_events_tempfile_explicit_dir(tmp_path: Path) -> None:
    result = query_events(
        StubCollector(), T0, T0 + timedelta(hours=1), save_to="tempfile", temp_dir=tmp_path
    )
    assert isinstance(result, SavedEventsResult)
    assert json.loads(Path(result.path).read_text(encoding="utf-8")) == []


def test_list_tdr_incidents_clusters_and_scans_two_queries() -> None:
    events = [
        # incident A: TDR storm then a crash, every gap <= 10 minutes
        rec(1, 0, "Display", 4101, level=EventLevel.WARNING, data={"param1": "nvlddmkm"}),
        rec(2, 4, "nvlddmkm", 153),
        rec(
            3,
            14,
            "Microsoft-Windows-Kernel-Power",
            41,
            level=EventLevel.CRITICAL,
            data={"BugcheckCode": "278"},
        ),
        # incident B: 30 minutes later, a corrected PCIe error and an Intel driver warning
        rec(4, 44, "Microsoft-Windows-WHEA-Logger", 17, level=EventLevel.WARNING),
        rec(5, 45, "igfxn", 9, level=EventLevel.WARNING),
        # noise
        rec(6, 46, "Display", 4127, level=EventLevel.INFORMATION),
        rec(7, 47, "amdkmdag", 1, level=EventLevel.INFORMATION),
        rec(
            8,
            48,
            "Windows Error Reporting",
            1001,
            log="Application",
            level=EventLevel.INFORMATION,
            data={"EventName": "APPCRASH", "P1": "x"},
        ),
    ]
    stub = StubCollector(events)
    result = list_tdr_incidents(stub, T0 - timedelta(hours=1), T0 + timedelta(hours=2))
    assert len(stub.queries) == 2
    assert stub.queries[1].levels == [EventLevel.CRITICAL, EventLevel.ERROR, EventLevel.WARNING]
    assert {"igfx", "igfxn", "amdkmdag"} <= set(stub.queries[1].providers or [])
    assert result.signals_total == 5
    assert [len(i.signals) for i in result.incidents] == [2, 3]  # newest first
    crash = result.incidents[1]
    assert crash.severity == "critical"
    assert crash.implicated_drivers == ["nvlddmkm"]
    assert crash.implicated_vendors == [GpuVendor.NVIDIA]
    assert crash.signals[0].signature_id == "kernel_power_41"  # most severe first
    assert "0x116" in crash.summary
    assert crash.window.start == T0 and crash.window.end == T0 + timedelta(minutes=14, seconds=1)
    assert crash.id == "inc-20261006T050000Z"
    hw = result.incidents[0]
    assert hw.implicated_vendors == [GpuVendor.INTEL]
    assert {c.signature_id for c in hw.signal_counts} == {"whea_17", "intel_igfx_warning"}


def test_cluster_gap_boundary() -> None:
    hits = [
        match_event(rec(i, m, "Display", 4101)) for i, m in enumerate([0, 10, 20.0001, 30], start=1)
    ]
    clusters = cluster_hits([h for h in hits if h is not None])
    assert [len(c) for c in clusters] == [2, 2]


def test_incidents_truncate_signals_and_note_caps() -> None:
    events = [
        rec(
            i,
            i * 0.1,
            "Windows Error Reporting",
            1001,
            log="Application",
            level=EventLevel.INFORMATION,
            data={"EventName": "LiveKernelEvent", "P1": "1b0"},
        )
        for i in range(1, 61)
    ]
    result = list_tdr_incidents(StubCollector(events), T0, T0 + timedelta(hours=1))
    (incident,) = result.incidents
    assert incident.signals_truncated and len(incident.signals) == 50
    assert incident.signal_counts[0].count == 60


def test_incidents_on_sample_events() -> None:
    result = list_tdr_incidents(
        StubCollector(sample_records()), T0 - timedelta(days=200), T0 + timedelta(days=30)
    )
    codes = {s.code for i in result.incidents for s in i.signals}
    assert "0x9F" in codes and "0x1B0" in codes
    assert all(s.signature_id != "display_tdr_4101" for i in result.incidents for s in i.signals)


def test_process_snapshot_sorting() -> None:
    stub = StubCollector()
    by_ws = process_snapshot(stub, top_n=3)
    assert [p.pid for p in by_ws.snapshot.top_processes] == [29, 28, 27]
    by_pf = process_snapshot(stub, top_n=2, sort_by="page_faults")
    assert [p.pid for p in by_pf.snapshot.top_processes] == [1, 2]
    by_io = process_snapshot(stub, top_n=1, sort_by="io")
    assert by_io.snapshot.top_processes[0].read_bytes == 200
    with pytest.raises(ValidationError):
        process_snapshot(stub, top_n=0)
    with pytest.raises(ValidationError):
        process_snapshot(stub, sort_by="cpu")  # type: ignore[arg-type]


def test_sensor_snapshot_window_rules() -> None:
    stub = StubCollector()
    sensor_snapshot(stub)
    sensor_snapshot(stub, T0, T0 + timedelta(minutes=5))
    assert stub.sensor_windows[0] is None
    assert stub.sensor_windows[1] == TimeWindow(start=T0, end=T0 + timedelta(minutes=5))
    with pytest.raises(ValidationError, match="both start and end"):
        sensor_snapshot(stub, start=T0)


def test_driver_inventory_allowlist() -> None:
    stub = StubCollector()
    assert driver_inventory(stub).device_classes == list(DEVICE_CLASS_ALLOWLIST)
    result = driver_inventory(stub, ["Display", "display", "net"])
    assert result.device_classes == ["DISPLAY", "NET"]
    assert stub.driver_classes[-1] == ["DISPLAY", "NET"]
    for bad in (["USB"], ["DISPLAY", "Keyboard"], [], ["DISPLAY'; Remove-Item"]):
        with pytest.raises(ValidationError):
            driver_inventory(stub, bad)


def test_dxdiag_and_system_info() -> None:
    stub = StubCollector()
    assert dxdiag_summary(stub).dxdiag.directx_version == "DirectX 12"
    info = system_info(stub)
    assert info.system.os_caption == "W" and info.collection.elapsed_ms >= 0


def test_tools_with_replay_collector(tmp_path: Path) -> None:
    from .test_replay import write_replay

    events = [e.model_dump(mode="json") for e in sample_records()]
    collector = ReplayCollector(write_replay(tmp_path / "r", events=events))
    result = list_tdr_incidents(collector, T0 - timedelta(days=200), T0 + timedelta(days=30))
    assert result.incidents and result.collection.scripts == ["replay:events.json"]
    assert system_info(collector).collection.scripts == ["replay:scenario.json"]


@pytest.mark.parametrize("tool", TOOL_NAMES)
def test_describe_collection_every_tool(tool: str) -> None:
    plan = describe_collection(tool)
    assert plan.tool == tool and plan.read_only
    assert plan.parameters_schema["type"] == "object"
    for script in plan.scripts:
        assert script.source.strip().startswith("#")
        assert script.command.startswith("powershell.exe -NoProfile -NonInteractive")


def test_describe_collection_details() -> None:
    events = describe_collection("query_events")
    assert [s.name for s in events.scripts] == ["get_events.ps1"]
    assert "Get-WinEvent -FilterXml" in events.scripts[0].source
    assert "save_to" in events.parameters_schema["properties"]
    sensors = describe_collection("sensor_snapshot")
    assert any("127.0.0.1:8085/data.json" in c for c in sensors.commands)
    assert any(c.startswith("nvidia-smi --query-gpu=") for c in sensors.commands)
    assert describe_collection("describe_collection").scripts == []
    with pytest.raises(ValueError, match="unknown tool"):
        describe_collection("format_disk")
