"""Live smoke test against the real Windows host. Runs only where powershell.exe exists."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pc_diag_mcp.collectors import CollectorUnavailable, PowerShellCollector, collect
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
from pc_diag_mcp.tools import list_tdr_incidents

from .conftest import has_powershell

pytestmark = [
    pytest.mark.windows,
    pytest.mark.skipif(not has_powershell(), reason="powershell.exe is not on PATH"),
]


@pytest.fixture(scope="module")
def collector() -> PowerShellCollector:
    return PowerShellCollector()


def window(days: int) -> TimeWindow:
    now = datetime.now(UTC)
    return TimeWindow(start=now - timedelta(days=days), end=now)


def test_live_events(collector: PowerShellCollector) -> None:
    records, manifest = collect(
        collector, lambda: collector.events(EventQuery(window=window(7), max_events=50))
    )
    assert all(isinstance(r, EventRecord) for r in records)
    assert len(records) <= 50
    assert manifest.source == "live" and manifest.queries


def test_live_events_no_match_is_empty(collector: PowerShellCollector) -> None:
    query = EventQuery(window=window(1), logs=["System"], providers=["No-Such-Provider-TDR"])
    assert collector.events(query) == []


def test_live_process_snapshot(collector: PowerShellCollector) -> None:
    snapshot = collector.process_snapshot(5)
    assert isinstance(snapshot, ProcessSnapshot)
    assert snapshot.top_processes and snapshot.memory.total_bytes > 0


def test_live_sensors(collector: PowerShellCollector) -> None:
    snapshots = collector.sensors(None)
    assert len(snapshots) == 1 and isinstance(snapshots[0], SensorSnapshot)
    assert snapshots[0].readings or snapshots[0].unavailable


def test_live_drivers(collector: PowerShellCollector) -> None:
    drivers = collector.drivers(["DISPLAY"])
    assert drivers and all(isinstance(d, DriverInfo) for d in drivers)


def test_live_system_info(collector: PowerShellCollector) -> None:
    info = collector.system_info()
    assert isinstance(info, SystemInfo) and info.os_build


def test_live_dxdiag(collector: PowerShellCollector) -> None:
    # A Windows Server CI runner may not offer dxdiag; reporting it unavailable is correct.
    try:
        summary = collector.dxdiag()
    except CollectorUnavailable as exc:
        assert exc.reason
        return
    assert isinstance(summary, DxDiagSummary)


def test_live_incident_scan(collector: PowerShellCollector) -> None:
    result = list_tdr_incidents(collector, *window(30).model_dump().values())
    assert result.collection.source == "live"
