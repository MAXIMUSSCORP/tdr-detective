from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from pc_diag_mcp.collectors import ReplayCollector
from pc_diag_mcp.collectors.base import CollectorUnavailable
from pc_diag_mcp.models import Diagnosis, EvidenceKind, RunStats
from tdr_agent.evidence import build_evidence, build_incident, incident_id, load_frametimes

from .conftest import diagnosis_dict

REPO = Path(__file__).resolve().parents[2]
SYN = REPO / "evals" / "scenarios" / "syn-026"
REC = REPO / "evals" / "scenarios" / "rec-001"
TIMELINE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def diagnosis(start: str, end: str, occurred: str, title: str = "Black screen") -> Diagnosis:
    d: dict[str, Any] = diagnosis_dict(["gpu_driver_timeout"])
    d["window"] = {"start": start, "end": end}
    d["bug_report"]["occurred_at"] = occurred
    d["bug_report"]["title"] = title
    return Diagnosis.model_validate(d)


SYN_DIAG = diagnosis(
    "2026-10-15T01:30:00Z",
    "2026-10-15T02:00:00Z",
    "2026-10-15T01:43:08Z",
    "Display driver reset with AMD Radeon RX 6800 XT driver 31.0.24033.1003 on build 22631",
)


@pytest.fixture(scope="module")
def items() -> list[Any]:
    return build_evidence(SYN_DIAG, ReplayCollector(SYN), frametimes=load_frametimes(SYN))


def test_event_evidence_comes_from_signature_hits(items: list[Any]) -> None:
    events = [i for i in items if i.kind == EvidenceKind.EVENT]
    assert events
    assert all(i.id.startswith("ev:") for i in events)
    tdr = [i for i in events if "4101" in i.label]
    assert tdr and tdr[0].time == datetime(2026, 10, 15, 1, 43, 8, 203359, tzinfo=UTC)
    assert tdr[0].severity in ("warning", "error")
    assert all(SYN_DIAG.window.start <= i.time <= SYN_DIAG.window.end for i in events)


def test_sensor_series(items: list[Any]) -> None:
    sensors = [i for i in items if i.kind == EvidenceKind.SENSOR]
    assert sensors
    units = {i.unit for i in sensors}
    assert "°C" in units
    for unit in units:
        labels = {i.label for i in sensors if i.unit == unit}
        assert len(labels) == 1, (unit, labels)  # one series per unit, as the timeline charts it
    assert all(i.value is not None and i.id.startswith("sensor:") for i in sensors)


def test_frametime_samples(items: list[Any]) -> None:
    frames = [i for i in items if i.kind == EvidenceKind.FRAMETIME]
    assert 5 <= len(frames) <= 30
    assert all(i.unit == "ms" for i in frames)
    spike = max(frames, key=lambda i: i.value or 0)
    assert (spike.value or 0) > 2000
    assert spike.severity == "error"
    times = [i.time for i in frames]
    assert times == sorted(times)


def test_display_driver(items: list[Any]) -> None:
    drivers = [i for i in items if i.kind == EvidenceKind.DRIVER]
    assert [i.id for i in drivers] == ["driver:AMD Radeon RX 6800 XT"]
    assert "31.0.24033.1003" in drivers[0].label


def test_ids_unique_sorted_and_deterministic(items: list[Any]) -> None:
    ids = [i.id for i in items]
    assert len(ids) == len(set(ids))
    assert [(i.time, i.id) for i in items] == sorted((i.time, i.id) for i in items)
    again = build_evidence(SYN_DIAG, ReplayCollector(SYN), frametimes=load_frametimes(SYN))
    assert again == items


def test_frametimes_need_the_replay_anchor(tmp_path: Path) -> None:
    assert load_frametimes(tmp_path) is None
    assert load_frametimes(REC) is None  # no frametimes.json


def test_recorded_scenario_without_sensors() -> None:
    diag = diagnosis("2026-10-06T05:30:00Z", "2026-10-06T06:15:00Z", "2026-10-06T05:52:46Z")
    items = build_evidence(diag, ReplayCollector(REC))
    kinds = {i.kind for i in items}
    assert EvidenceKind.SENSOR not in kinds
    assert EvidenceKind.FRAMETIME not in kinds
    labels = " ".join(i.label for i in items)
    assert "Kernel-Power 41" in labels
    assert "driver:NVIDIA GeForce RTX 5070 Ti Laptop GPU" in {i.id for i in items}


class _NoDrivers(ReplayCollector):
    def drivers(self, device_classes: list[str]) -> Any:
        raise CollectorUnavailable("drivers.ps1", "WMI is broken")


def test_drivers_fall_back_to_system_info() -> None:
    items = build_evidence(SYN_DIAG, _NoDrivers(SYN))
    drivers = [i for i in items if i.kind == EvidenceKind.DRIVER]
    assert [i.id for i in drivers] == ["driver:AMD Radeon RX 6800 XT"]


def test_incident() -> None:
    created = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    run = RunStats(config="skill_tools", model="m", input_tokens=1, output_tokens=2, latency_ms=3)
    incident = build_incident(
        "Why did my screen go black?",
        SYN_DIAG,
        ReplayCollector(SYN),
        run=run,
        created_at=created,
        replay_dir=SYN,
    )
    assert incident.id == incident_id(SYN_DIAG)
    assert incident.id.endswith("-20261015T014308Z")
    assert incident.id.startswith("display-driver-reset-with-amd-radeon-rx-6800-xt")
    assert TIMELINE_ID.match(incident.id)
    assert incident.system.os_build == "22631.4317"
    assert incident.created_at == created
    assert incident.run == run
    assert any(i.kind == EvidenceKind.FRAMETIME for i in incident.evidence)


@pytest.mark.parametrize("title", ["", "!!!", "x" * 400, "Ünïcødé BSOD 0x9F"])
def test_incident_id_is_always_url_safe(title: str) -> None:
    diag = diagnosis("2026-10-15T01:30:00Z", "2026-10-15T02:00:00Z", "2026-10-15T01:43:08Z", title)
    assert TIMELINE_ID.match(incident_id(diag))
