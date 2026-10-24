from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pc_diag_mcp.collectors import (
    CollectorError,
    CollectorUnavailable,
    PowerShellCollector,
    collect,
)
from pc_diag_mcp.collectors.powershell import (
    LHM_UNREACHABLE,
    LHM_URL,
    NO_EVENTS,
    PS_DIR,
    SCRIPTS_BY_METHOD,
    http_get_localhost,
)
from pc_diag_mcp.models import EventLevel, EventQuery, GpuVendor, TimeWindow

from .conftest import FakeRunner, envelope, no_lhm, which_all

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
WINDOW = TimeWindow(start=NOW - timedelta(days=120), end=NOW)


def make(runner: FakeRunner, **kwargs: object) -> PowerShellCollector:
    options: dict[str, object] = {
        "runner": runner,
        "which": which_all,
        "http_get": no_lhm,
        "wsl": False,
    }
    options.update(kwargs)
    return PowerShellCollector(**options)  # type: ignore[arg-type]


def test_invocation_uses_fixed_argument_list_and_json_stdin(runner: FakeRunner) -> None:
    collector = make(runner, timeout_s=12)
    collector.system_info()
    call = runner.calls[-1]
    assert call.argv == [
        "/win/powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(PS_DIR / "system_info.ps1"),
    ]
    assert json.loads(call.stdin or "") == {}
    assert call.timeout == 12


def test_wsl_converts_script_path_once(runner: FakeRunner) -> None:
    collector = make(runner, wsl=True)
    collector.system_info()
    collector.system_info()
    wslpath_calls = [c for c in runner.calls if c.argv[0] == "wslpath"]
    assert len(wslpath_calls) == 1
    assert wslpath_calls[0].argv[:2] == ["wslpath", "-w"]
    assert runner.calls[-1].argv[-1].startswith("\\\\wsl.localhost\\")


def test_wslpath_failure_is_a_collector_error(runner: FakeRunner) -> None:
    def broken(
        argv: list[str], stdin: str | None, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "nope")

    with pytest.raises(CollectorError, match="wslpath"):
        PowerShellCollector(runner=broken, which=which_all, wsl=True).system_info()


def test_events_passes_filter_xml_per_log_and_parses_samples(runner: FakeRunner) -> None:
    query = EventQuery(window=WINDOW, providers=["Display"], max_events=50)
    records, manifest = collect(make(runner), lambda: make(runner).events(query))
    params = runner.stdin_for("get_events.ps1")
    assert params["max_events"] == 50
    assert len(params["filters"]) == 2
    assert 'Path="System"' in params["filters"][0] and 'Path="Application"' in params["filters"][1]
    assert manifest.source == "live"
    assert manifest.scripts == ["ps/get_events.ps1"]
    assert manifest.queries == params["filters"]
    # newest first
    assert [r.time for r in records] == sorted((r.time for r in records), reverse=True)
    by_id = {(r.provider, r.event_id) for r in records}
    assert ("Microsoft-Windows-Kernel-Power", 41) in by_id
    kp = [r for r in records if r.event_id == 41]
    assert {r.data["BugcheckCode"] for r in kp} == {"0", "159"}
    assert all(r.level is EventLevel.CRITICAL for r in kp)
    lke = [r for r in records if "LiveKernelEvent" in r.message]
    assert lke and all(len(r.message) <= 600 for r in records)
    assert all("\n" not in r.message for r in records)


def test_events_truncates_to_max_events(runner: FakeRunner) -> None:
    query = EventQuery(window=WINDOW, logs=["System"], max_events=2)
    assert len(make(runner).events(query)) == 2


def test_no_events_message_is_an_empty_list(runner: FakeRunner) -> None:
    query = EventQuery(window=WINDOW, logs=["System"])
    runner.responses["get_events.ps1"] = json.dumps(
        {"ok": False, "kind": "error", "error": NO_EVENTS + "."}
    )
    assert make(runner).events(query) == []
    runner.responses["get_events.ps1"] = subprocess.CompletedProcess(
        [], 1, "", f"Get-WinEvent : {NO_EVENTS}."
    )
    assert make(runner).events(query) == []
    runner.responses["get_events.ps1"] = envelope([])
    assert make(runner).events(query) == []


@pytest.mark.parametrize(
    ("response", "error", "match"),
    [
        (subprocess.TimeoutExpired(["x"], 5), CollectorError, "timed out"),
        (FileNotFoundError("gone"), CollectorUnavailable, "could not be started"),
        (subprocess.CompletedProcess([], 1, "", "boom"), CollectorError, "exited 1: boom"),
        ("not json", CollectorError, "non-JSON"),
        ("[1, 2]", CollectorError, "unexpected payload"),
        (json.dumps({"ok": False, "kind": "error", "error": "bad"}), CollectorError, "failed: bad"),
        (
            json.dumps({"ok": False, "kind": "unavailable", "error": "Access is denied."}),
            CollectorUnavailable,
            "elevation",
        ),
        (envelope({"not": "a list"}), CollectorError, "non-list"),
    ],
)
def test_events_error_paths(
    runner: FakeRunner, response: object, error: type[Exception], match: str
) -> None:
    runner.responses["get_events.ps1"] = response  # type: ignore[assignment]
    with pytest.raises(error, match=match):
        make(runner).events(EventQuery(window=WINDOW, logs=["System"]))


def test_missing_powershell_is_unavailable(runner: FakeRunner) -> None:
    collector = make(runner, which=lambda name: None)
    with pytest.raises(CollectorUnavailable, match=r"powershell\.exe unavailable: not found"):
        collector.system_info()


def test_missing_script_is_an_error(runner: FakeRunner, tmp_path: Path) -> None:
    with pytest.raises(CollectorError, match="missing collector script"):
        make(runner, script_dir=tmp_path).system_info()


def test_process_snapshot_parses_sample(runner: FakeRunner) -> None:
    snapshot = make(runner).process_snapshot(2)
    assert snapshot.memory.total_bytes > snapshot.memory.available_bytes > 0
    assert snapshot.memory.commit_limit_bytes >= snapshot.memory.commit_used_bytes
    assert [d.name for d in snapshot.disks] == ["0 C:", "1 D:"]
    # union of the top 2 by three keys: between 2 and 6 processes
    assert 2 <= len(snapshot.top_processes) <= 6
    assert all(p.started is None or p.started.tzinfo is not None for p in snapshot.top_processes)


def test_process_snapshot_malformed(runner: FakeRunner) -> None:
    runner.responses["process_snapshot.ps1"] = envelope({"taken_at": "x"})
    with pytest.raises(CollectorError, match="malformed"):
        make(runner).process_snapshot(5)


def test_sensors_order_and_unavailable_reasons(runner: FakeRunner) -> None:
    collector = make(runner)
    snapshots, manifest = collect(collector, lambda: collector.sensors(WINDOW))
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.unavailable[0] == LHM_UNREACHABLE
    sources = [r.source for r in snapshot.readings]
    # backend order: nvidia-smi, then thermal zones, then GPU engines
    assert sources.index("nvidia-smi") < sources.index("cim:ThermalZoneInformation")
    assert sources.index("cim:ThermalZoneInformation") < sources.index("cim:GPUEngine")
    thermal = [r for r in snapshot.readings if r.source == "cim:ThermalZoneInformation"]
    assert all(0 < r.value < 120 and r.unit == "°C" for r in thermal)
    assert any("no history" in note for note in manifest.notes)
    assert f"GET {LHM_URL}" in manifest.scripts
    smi = runner.calls[[c.argv[0] for c in runner.calls].index("/usr/bin/nvidia-smi")]
    assert smi.argv[-1] == "--format=csv,noheader,nounits"
    assert smi.argv[1].startswith("--query-gpu=name,temperature.gpu")


def test_sensors_without_window_has_no_history_note(runner: FakeRunner) -> None:
    collector = make(runner)
    _, manifest = collect(collector, lambda: collector.sensors(None))
    assert not manifest.notes


def test_sensors_prefers_lhm_and_reports_every_failure(runner: FakeRunner) -> None:
    lhm = {
        "Text": "Sensor",
        "Children": [
            {
                "Text": "HOST",
                "Children": [
                    {
                        "Text": "NVIDIA GeForce RTX 5070 Ti Laptop GPU",
                        "HardwareId": "/gpu-nvidia/0",
                        "Children": [
                            {
                                "Text": "Temperatures",
                                "Children": [
                                    {
                                        "Text": "GPU Core",
                                        "SensorId": "/gpu-nvidia/0/temperature/0",
                                        "Type": "Temperature",
                                        "Value": "61,5 °C",
                                        "RawValue": "61,5 °C",
                                        "Children": [],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }
    runner.responses["sensors_cim.ps1"] = envelope(
        {
            "thermal": {"ok": False, "error": "Invalid class"},
            "gpu_engine": {"ok": True, "rows": []},
        }
    )
    collector = make(
        runner,
        http_get=lambda url, timeout: json.dumps(lhm).encode(),
        which=lambda name: "/win/powershell.exe" if name == "powershell.exe" else None,
    )
    snapshot = collector.sensors(None)[0]
    assert snapshot.readings[0].source == "librehardwaremonitor:http"
    assert snapshot.readings[0].value == 61.5
    assert snapshot.readings[0].hardware == "NVIDIA GeForce RTX 5070 Ti Laptop GPU"
    assert snapshot.unavailable == [
        "nvidia-smi not found on PATH",
        "ACPI thermal zones (Win32_PerfFormattedData_Counters_ThermalZoneInformation): "
        "Invalid class",
        "GPU engine counters (Win32_PerfFormattedData_GPUPerformanceCounters_GPUEngine): "
        "no readings",
    ]


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"not json", "unreadable"),
        (b"[]", "unreadable"),
        (b'{"Text": "Sensor", "Children": []}', "no supported sensors"),
    ],
)
def test_lhm_bad_payloads(runner: FakeRunner, body: bytes, reason: str) -> None:
    collector = make(runner, http_get=lambda url, timeout: body)
    assert reason in collector.sensors(None)[0].unavailable[0]


def test_nvidia_smi_failures_are_unavailable(runner: FakeRunner) -> None:
    runner.responses["nvidia-smi"] = subprocess.CompletedProcess([], 9, "", "NVML not found")
    assert "nvidia-smi failed: NVML not found" in make(runner).sensors(None)[0].unavailable
    runner.responses["nvidia-smi"] = subprocess.TimeoutExpired(["nvidia-smi"], 15)
    assert any("did not run" in u for u in make(runner).sensors(None)[0].unavailable)


def test_cim_sensor_script_failure_still_returns_snapshot(runner: FakeRunner) -> None:
    runner.responses["sensors_cim.ps1"] = "garbage"
    snapshot = make(runner).sensors(None)[0]
    assert any(u.startswith("thermal zone / GPU engine counters") for u in snapshot.unavailable)
    assert any(r.source == "nvidia-smi" for r in snapshot.readings)


def test_drivers_sends_classes_as_data(runner: FakeRunner) -> None:
    drivers = make(runner).drivers(["DISPLAY", "MEDIA"])
    assert runner.stdin_for("drivers.ps1") == {"device_classes": ["DISPLAY", "MEDIA"]}
    assert {d.device_class for d in drivers} <= {"DISPLAY", "MEDIA"}
    assert any(d.provider == "NVIDIA" for d in drivers)
    runner.responses["drivers.ps1"] = envelope([{"device_name": 1}])
    with pytest.raises(CollectorError, match="malformed"):
        make(runner).drivers(["DISPLAY"])


def test_system_info_parses_sample(runner: FakeRunner) -> None:
    info = make(runner).system_info()
    assert info.os_build.startswith("26200.")
    assert {g.vendor for g in info.gpus} == {GpuVendor.INTEL, GpuVendor.NVIDIA}
    assert isinstance(info.utc_offset_minutes, int)
    runner.responses["system_info.ps1"] = envelope({"os_caption": "x"})
    with pytest.raises(CollectorError, match="malformed"):
        make(runner).system_info()


def test_dxdiag_waits_and_parses(runner: FakeRunner) -> None:
    summary = make(runner).dxdiag()
    assert runner.stdin_for("dxdiag.ps1") == {"wait_seconds": 90}
    assert runner.calls[-1].timeout == 120
    assert summary.directx_version == "DirectX 12"
    assert any("NVIDIA" in d.name for d in summary.display_devices)
    runner.responses["dxdiag.ps1"] = envelope({})
    with pytest.raises(CollectorError, match="no XML"):
        make(runner).dxdiag()


def test_identity(runner: FakeRunner) -> None:
    assert make(runner).identity() == {"computer_name": "HOST", "user_name": "someone"}


def test_http_get_refuses_non_localhost() -> None:
    with pytest.raises(ValueError, match="only"):
        http_get_localhost("http://example.com/data.json", 1.0)


def test_every_declared_script_exists_and_is_read_only() -> None:
    forbidden = (
        "Set-ItemProperty",
        "New-ItemProperty",
        "Remove-ItemProperty",
        "Set-Cim",
        "Invoke-CimMethod",
        "Invoke-Expression",
        "Start-Process -Verb RunAs",
        "reg add",
    )
    for scripts in SCRIPTS_BY_METHOD.values():
        for name in scripts:
            text = (PS_DIR / name).read_text(encoding="utf-8")
            assert "ConvertTo-Json" in text
            for word in forbidden:
                assert word not in text, f"{name} contains {word}"
