from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pc_diag_mcp.collectors import CollectorError, CollectorUnavailable, ReplayCollector, collect
from pc_diag_mcp.models import (
    EventLevel,
    EventQuery,
    EventRecord,
    SystemInfo,
    TimeWindow,
)

NOW = datetime(2026, 10, 6, 6, 0, tzinfo=UTC)


def _event(record_id: int, minutes_before: float, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "record_id": record_id,
        "time": (NOW - timedelta(minutes=minutes_before)).isoformat(),
        "log": "System",
        "provider": "Display",
        "event_id": 4101,
        "level": "warning",
        "message": "",
        "data": {},
    }
    base.update(kw)
    return base


def write_replay(root: Path, **files: Any) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    scenario = {
        "id": "unit-test",
        "description": "unit",
        "now": NOW.isoformat(),
        "system": SystemInfo(
            os_caption="Windows 11", os_build="26200.1", gpus=[], utc_offset_minutes=-360
        ).model_dump(mode="json"),
    }
    (root / "scenario.json").write_text(json.dumps(scenario))
    for name, payload in files.items():
        (root / f"{name}.json").write_text(json.dumps(payload))
    return root


@pytest.fixture
def replay(tmp_path: Path) -> ReplayCollector:
    events = [
        _event(1, 50, provider="Microsoft-Windows-Kernel-Power", event_id=41, level="critical"),
        _event(2, 40),
        _event(
            3,
            30,
            log="Application",
            provider="Windows Error Reporting",
            event_id=1001,
            level="information",
        ),
        _event(4, 20, provider="display"),
        _event(5, 200),
    ]
    snapshot = {
        "taken_at": NOW.isoformat(),
        "memory": {
            "total_bytes": 10,
            "available_bytes": 5,
            "commit_used_bytes": 3,
            "commit_limit_bytes": 9,
        },
        "disks": [],
        "top_processes": [
            {
                "pid": i,
                "name": f"p{i}",
                "working_set_bytes": i,
                "private_bytes": 0,
                "page_faults": 100 - i,
                "read_bytes": 0,
                "write_bytes": 0,
                "cpu_seconds": 0.0,
            }
            for i in range(1, 11)
        ],
    }
    sensors = [
        {"taken_at": (NOW - timedelta(minutes=m)).isoformat(), "readings": [], "unavailable": []}
        for m in (30, 10, -10)
    ]
    drivers = [
        {"device_name": "GPU", "device_class": "DISPLAY", "provider": "NVIDIA", "version": "1"},
        {"device_name": "NIC", "device_class": "NET", "provider": "Intel", "version": "2"},
    ]
    dxdiag = {"directx_version": "DirectX 12", "display_devices": [], "problems": []}
    root = write_replay(
        tmp_path / "r",
        events=events,
        process_snapshot=snapshot,
        sensors=sensors,
        drivers=drivers,
        dxdiag=dxdiag,
    )
    return ReplayCollector(root)


def window(minutes: float = 60) -> TimeWindow:
    return TimeWindow(start=NOW - timedelta(minutes=minutes), end=NOW)


def ids(records: list[EventRecord]) -> list[int]:
    return [r.record_id for r in records]


def test_events_window_and_newest_first(replay: ReplayCollector) -> None:
    records, manifest = collect(replay, lambda: replay.events(EventQuery(window=window())))
    assert ids(records) == [4, 3, 2, 1]
    assert manifest.source == "replay" and manifest.scripts == ["replay:events.json"]


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"logs": ["Application"]}, [3]),
        ({"providers": ["Display"]}, [4, 2]),  # provider match is case-insensitive
        ({"event_ids": [41]}, [1]),
        ({"levels": [EventLevel.CRITICAL, EventLevel.INFORMATION]}, [3, 1]),
        ({"max_events": 2}, [4, 3]),
    ],
)
def test_events_filters(
    replay: ReplayCollector, kwargs: dict[str, Any], expected: list[int]
) -> None:
    assert ids(replay.events(EventQuery(window=window(), **kwargs))) == expected


def test_process_snapshot_union(replay: ReplayCollector) -> None:
    pids = {p.pid for p in replay.process_snapshot(2).top_processes}
    assert pids == {10, 9, 1, 2}


def test_sensors_window_and_now(replay: ReplayCollector) -> None:
    in_window = replay.sensors(window(20))
    assert len(in_window) == 1
    current, manifest = collect(replay, lambda: replay.sensors(None))
    assert [s.taken_at for s in current] == [NOW - timedelta(minutes=10)]
    assert manifest.notes


def test_drivers_filter_by_class(replay: ReplayCollector) -> None:
    assert [d.device_name for d in replay.drivers(["display"])] == ["GPU"]


def test_system_info_and_dxdiag(replay: ReplayCollector) -> None:
    assert replay.system_info().os_build == "26200.1"
    assert replay.dxdiag().directx_version == "DirectX 12"


def test_missing_optional_files_are_unavailable(tmp_path: Path) -> None:
    collector = ReplayCollector(write_replay(tmp_path / "bare", events=[]))
    for call in (
        lambda: collector.process_snapshot(5),
        lambda: collector.sensors(None),
        lambda: collector.drivers(["DISPLAY"]),
        collector.dxdiag,
    ):
        with pytest.raises(CollectorUnavailable, match="has no"):
            call()


def test_missing_required_or_invalid_files(tmp_path: Path) -> None:
    with pytest.raises(CollectorError, match="not found"):
        ReplayCollector(tmp_path / "nope")
    with pytest.raises(CollectorError, match=r"missing events\.json"):
        ReplayCollector(write_replay(tmp_path / "noevents"))
    with pytest.raises(CollectorError, match=r"invalid events\.json"):
        ReplayCollector(write_replay(tmp_path / "bad", events=[{"record_id": "x"}]))
