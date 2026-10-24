from __future__ import annotations

import pytest

from pc_diag_mcp.collectors.base import CollectorError
from pc_diag_mcp.collectors.parsers import (
    aggregate_gpu_engines,
    gpu_vendor,
    parse_dxdiag_xml,
    parse_lhm_data_json,
    parse_nvidia_smi,
    parse_thermal_zones,
    top_process_union,
)
from pc_diag_mcp.models import GpuVendor, ProcessInfo

from .conftest import sample


def test_nvidia_smi_sample_skips_not_available_fields() -> None:
    readings = parse_nvidia_smi(sample("nvidia_smi_raw.json")["stdout"])
    by_sensor = {r.sensor: r for r in readings}
    assert by_sensor["temperature.gpu"].unit == "°C"
    assert by_sensor["power.draw"].kind == "power"
    assert "fan.speed" not in by_sensor  # [N/A] on this laptop
    assert all(r.hardware.startswith("NVIDIA") for r in readings)


def test_nvidia_smi_ignores_malformed_rows() -> None:
    assert parse_nvidia_smi("garbage\n\n") == []
    two_gpus = "A, 40, 1, 2, 3, 4, 5.5, 30\nB, 50, [Not Supported], 2, 3, 4, 6, 31\n"
    readings = parse_nvidia_smi(two_gpus)
    assert {r.hardware for r in readings} == {"A", "B"}
    assert len([r for r in readings if r.hardware == "B"]) == 6


def test_thermal_zones_convert_kelvin() -> None:
    rows = [
        {"name": "\\_TZ.THRM", "kelvin": 333, "kelvin_tenths": 3332},
        {"name": "\\_TZ.Z1", "kelvin": 300, "kelvin_tenths": 0},
        {"name": "\\_TZ.OFF", "kelvin": 0, "kelvin_tenths": 0},
    ]
    readings = parse_thermal_zones(rows)
    assert [(r.sensor, r.value) for r in readings] == [("\\_TZ.THRM", 60.1), ("\\_TZ.Z1", 26.9)]
    assert all(
        r.kind == "temperature"
        for r in parse_thermal_zones(sample("sensors_cim_raw.json")["thermal"]["rows"])
    )


def test_gpu_engines_aggregate_per_adapter_and_type() -> None:
    luid = "luid_0x00000000_0x0001492A"
    rows = [
        {"name": f"pid_1_{luid}_phys_0_eng_0_engtype_3D", "utilization": 30},
        {"name": f"pid_2_{luid}_phys_0_eng_0_engtype_3D", "utilization": 25},
        {"name": f"pid_2_{luid}_phys_0_eng_1_engtype_Copy", "utilization": 5},
        {"name": f"pid_3_{luid}_phys_0_eng_2_engtype_Copy", "utilization": 9},
        {"name": f"pid_3_{luid}_phys_0_eng_3_engtype_3D", "utilization": 80},
        {"name": f"pid_3_{luid}_phys_0_eng_3_engtype_3D", "utilization": 70},
        {"name": f"pid_3_{luid}_phys_0_eng_4_engtype_", "utilization": 99},
        {"name": "garbage", "utilization": 50},
    ]
    readings = {r.sensor: r.value for r in aggregate_gpu_engines(rows)}
    # engine 3 sums to 150 -> clamped at 100; Copy is the busiest Copy engine.
    assert readings == {"3D engine utilization": 100.0, "Copy engine utilization": 9.0}
    assert aggregate_gpu_engines(sample("sensors_cim_raw.json")["gpu_engine"]["rows"])


def test_lhm_parse_rejects_non_objects_and_skips_unknown_types() -> None:
    with pytest.raises(CollectorError):
        parse_lhm_data_json([])
    tree = {
        "Text": "root",
        "Children": [
            {"Text": "x", "SensorId": "/a/data/0", "Type": "Data", "Value": "3 GB"},
            {"Text": "y", "SensorId": "/a/fan/0", "Type": "Fan", "Value": "1200 RPM"},
            {"Text": "z", "SensorId": "/a/load/0", "Type": "Load", "Value": "-"},
            "not a node",
        ],
    }
    readings = parse_lhm_data_json(tree)
    assert [(r.sensor, r.kind, r.value, r.unit) for r in readings] == [("y", "fan", 1200.0, "RPM")]


@pytest.mark.parametrize(
    ("vendor_id", "names", "expected"),
    [
        ("10DE", (), GpuVendor.NVIDIA),
        ("1002", (), GpuVendor.AMD),
        ("8086", (), GpuVendor.INTEL),
        (None, ("NVIDIA",), GpuVendor.NVIDIA),
        (None, ("Advanced Micro Devices, Inc.",), GpuVendor.AMD),
        (None, ("Intel Corporation",), GpuVendor.INTEL),
        ("1414", ("Microsoft Basic Display Adapter",), GpuVendor.OTHER),
    ],
)
def test_gpu_vendor(vendor_id: str | None, names: tuple[str, ...], expected: GpuVendor) -> None:
    assert gpu_vendor(vendor_id, *names) is expected


def test_dxdiag_sample() -> None:
    summary = parse_dxdiag_xml(sample("dxdiag_raw.json")["xml"])
    assert summary.directx_version == "DirectX 12"
    assert len(summary.display_devices) >= 2
    nvidia = next(d for d in summary.display_devices if "NVIDIA" in d.name)
    assert nvidia.driver_version
    assert nvidia.driver_date is None and nvidia.dedicated_memory_mb is None  # "Unknown", "n/a"
    assert summary.problems == []


def test_dxdiag_problems_and_memory() -> None:
    xml = (
        "﻿<DxDiag><SystemInformation><DirectXVersion>DirectX 12</DirectXVersion>"
        "<MachineName>SECRET</MachineName></SystemInformation>"
        "<DxDiagNotes><DisplayTab>No problems found.</DisplayTab>"
        "<SoundTab>Driver is not WHQL signed.</SoundTab></DxDiagNotes>"
        "<DisplayDevices><DisplayDevice><CardName>GPU</CardName><Manufacturer>M</Manufacturer>"
        "<DriverVersion>1.2</DriverVersion><DriverDate>1/2/2026</DriverDate>"
        "<DedicatedMemory>7957 MB</DedicatedMemory><FeatureLevels>12_2,12_1, 12_0</FeatureLevels>"
        "<Notes>Display hung.</Notes></DisplayDevice></DisplayDevices></DxDiag>"
    )
    summary = parse_dxdiag_xml(xml)
    device = summary.display_devices[0]
    assert device.dedicated_memory_mb == 7957
    assert device.feature_levels == ["12_2", "12_1", "12_0"]
    assert device.driver_date == "1/2/2026"
    assert summary.problems == ["GPU: Display hung.", "SoundTab: Driver is not WHQL signed."]
    assert "SECRET" not in summary.model_dump_json()


def test_dxdiag_rejects_bad_xml() -> None:
    with pytest.raises(CollectorError):
        parse_dxdiag_xml("<DxDiag>")
    assert parse_dxdiag_xml("<DxDiag/>").directx_version == "unknown"


def _proc(pid: int, ws: int, pf: int, io: int) -> ProcessInfo:
    return ProcessInfo(
        pid=pid,
        name=f"p{pid}",
        working_set_bytes=ws,
        private_bytes=0,
        page_faults=pf,
        read_bytes=io,
        write_bytes=0,
        cpu_seconds=0.0,
    )


def test_top_process_union_keeps_leaders_of_each_key() -> None:
    procs = [_proc(1, 100, 1, 1), _proc(2, 1, 100, 1), _proc(3, 1, 1, 100), _proc(4, 50, 50, 50)]
    assert [p.pid for p in top_process_union(procs, 1)] == [1, 2, 3]
    assert len(top_process_union(procs, 10)) == 4
