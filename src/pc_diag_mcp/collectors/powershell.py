"""Live collector: fixed PowerShell scripts over CIM / Get-WinEvent.

Every call runs one version-controlled script from ``pc_diag_mcp/ps`` as::

    powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File <script>

with an argument list (never a shell), parameters as JSON on stdin and a JSON envelope
(``{"ok": true, "data": ...}`` or ``{"ok": false, "kind": "unavailable"|"error", ...}``) on
stdout. Scripts only read; the single exception is dxdiag's temporary report file, which the
script deletes.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from pc_diag_mcp.collectors.base import (
    CollectorError,
    CollectorSource,
    CollectorUnavailable,
    record_note,
    record_query,
    record_source,
)
from pc_diag_mcp.collectors.eventxml import build_filter_xml, parse_event
from pc_diag_mcp.collectors.parsers import (
    NVIDIA_SMI_QUERY,
    aggregate_gpu_engines,
    gpu_vendor,
    parse_dxdiag_xml,
    parse_lhm_data_json,
    parse_nvidia_smi,
    parse_thermal_zones,
    top_process_union,
)
from pc_diag_mcp.models import (
    DiskState,
    DriverInfo,
    DxDiagSummary,
    EventQuery,
    EventRecord,
    GpuInfo,
    MemoryState,
    ProcessInfo,
    ProcessSnapshot,
    SensorReading,
    SensorSnapshot,
    SystemInfo,
    TimeWindow,
)

PS_DIR = Path(__file__).resolve().parent.parent / "ps"
POWERSHELL_FLAGS = ("-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File")
NO_EVENTS = "No events were found that match the specified selection criteria"

# Fixed, localhost-only, GET-only. /Sensor?action=Set on the same server writes values,
# so nothing but data.json is ever requested.
LHM_URL = "http://127.0.0.1:8085/data.json"
LHM_UNREACHABLE = "LibreHardwareMonitor web server not reachable on 127.0.0.1:8085"

DXDIAG_WAIT_S = 90

# Which scripts/commands each collector method runs, for describe_collection.
SCRIPTS_BY_METHOD: dict[str, tuple[str, ...]] = {
    "events": ("get_events.ps1",),
    "process_snapshot": ("process_snapshot.ps1",),
    "sensors": ("sensors_cim.ps1",),
    "drivers": ("drivers.ps1",),
    "system_info": ("system_info.ps1",),
    "dxdiag": ("dxdiag.ps1",),
}
COMMANDS_BY_METHOD: dict[str, tuple[str, ...]] = {
    "sensors": (
        f"GET {LHM_URL} (only if LibreHardwareMonitor's web server is running)",
        f"nvidia-smi --query-gpu={NVIDIA_SMI_QUERY} --format=csv,noheader,nounits",
    ),
}

Runner = Callable[[Sequence[str], str | None, float], "subprocess.CompletedProcess[str]"]
HttpGet = Callable[[str, float], bytes]
Which = Callable[[str], str | None]


def run_process(
    argv: Sequence[str], stdin: str | None, timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def http_get_localhost(url: str, timeout: float) -> bytes:
    if not url.startswith("http://127.0.0.1:"):
        raise ValueError("only localhost endpoints may be fetched")
    # An empty ProxyHandler keeps the request on the loopback interface.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, method="GET")  # noqa: S310 - fixed http://127.0.0.1 URL
    with opener.open(request, timeout=timeout) as response:
        body: bytes = response.read(16 * 1024 * 1024)
        return body


def running_under_wsl() -> bool:
    return sys.platform == "linux" and "microsoft" in platform.uname().release.lower()


_PROCESSES = TypeAdapter(list[ProcessInfo])
_DISKS = TypeAdapter(list[DiskState])
_DRIVERS = TypeAdapter(list[DriverInfo])


class PowerShellCollector:
    """Collects live telemetry from the local Windows host (or the WSL host's Windows side)."""

    def __init__(
        self,
        *,
        timeout_s: float = 60.0,
        runner: Runner = run_process,
        http_get: HttpGet = http_get_localhost,
        which: Which = shutil.which,
        wsl: bool | None = None,
        script_dir: Path = PS_DIR,
    ) -> None:
        self._timeout = timeout_s
        self._run = runner
        self._http_get = http_get
        self._which = which
        self._wsl = running_under_wsl() if wsl is None else wsl
        self._script_dir = script_dir
        self._script_paths: dict[str, str] = {}

    @property
    def source(self) -> CollectorSource:
        return "live"

    # ------------------------------------------------------------------ #
    # Invocation
    # ------------------------------------------------------------------ #

    def _powershell(self) -> str:
        exe = self._which("powershell.exe")
        if exe is None:
            raise CollectorUnavailable("powershell.exe", "not found on PATH")
        return exe

    def _script_path(self, name: str) -> str:
        cached = self._script_paths.get(name)
        if cached is not None:
            return cached
        path = self._script_dir / name
        if not path.is_file():
            raise CollectorError(f"missing collector script {name}")
        resolved = str(path)
        if self._wsl:
            # powershell.exe runs on the Windows side and needs a Windows (UNC) path.
            converted = self._run(["wslpath", "-w", resolved], None, 10.0)
            if converted.returncode != 0 or not converted.stdout.strip():
                raise CollectorError(f"wslpath could not convert {resolved}")
            resolved = converted.stdout.strip()
        self._script_paths[name] = resolved
        return resolved

    def _invoke(self, name: str, params: Mapping[str, Any], timeout: float | None = None) -> Any:
        """Run one fixed script and return its ``data`` payload."""
        record_source(f"ps/{name}")
        argv = [self._powershell(), *POWERSHELL_FLAGS, self._script_path(name)]
        limit = timeout or self._timeout
        # ensure_ascii keeps stdin independent of the console code page.
        stdin = json.dumps(dict(params), ensure_ascii=True)
        try:
            proc = self._run(argv, stdin, limit)
        except subprocess.TimeoutExpired as exc:
            raise CollectorError(f"{name} timed out after {limit:.0f} s") from exc
        except OSError as exc:
            raise CollectorUnavailable("powershell.exe", f"could not be started: {exc}") from exc

        stdout = proc.stdout.lstrip("﻿").strip()
        if not stdout and NO_EVENTS in proc.stderr:
            return []
        if proc.returncode != 0 and not stdout:
            raise CollectorError(f"{name} exited {proc.returncode}: {proc.stderr.strip()[:300]}")
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise CollectorError(f"{name} returned non-JSON output: {stdout[:200]!r}") from exc
        if not isinstance(envelope, dict) or "ok" not in envelope:
            raise CollectorError(f"{name} returned an unexpected payload")
        if not envelope["ok"]:
            reason = str(envelope.get("error") or "unknown error")
            if NO_EVENTS in reason:
                return []
            if envelope.get("kind") == "unavailable":
                raise CollectorUnavailable(name, f"{reason} (may require elevation, not requested)")
            raise CollectorError(f"{name} failed: {reason}")
        return envelope.get("data")

    # ------------------------------------------------------------------ #
    # Collector protocol
    # ------------------------------------------------------------------ #

    def events(self, query: EventQuery) -> list[EventRecord]:
        filters = [build_filter_xml(query, log) for log in query.logs]
        for filter_xml in filters:
            record_query(filter_xml)
        data = self._invoke(
            "get_events.ps1",
            {"filters": filters, "max_events": query.max_events},
            timeout=max(self._timeout, 120.0),
        )
        if not isinstance(data, list):
            raise CollectorError("get_events.ps1 returned a non-list payload")
        records = [parse_event(str(item.get("xml") or ""), item.get("message")) for item in data]
        records.sort(key=lambda r: (r.time, r.record_id), reverse=True)
        return records[: query.max_events]

    def process_snapshot(self, top_n: int) -> ProcessSnapshot:
        data = self._invoke("process_snapshot.ps1", {})
        try:
            processes = _PROCESSES.validate_python(data["processes"])
            return ProcessSnapshot(
                taken_at=data["taken_at"],
                memory=MemoryState.model_validate(data["memory"]),
                disks=_DISKS.validate_python(data["disks"]),
                top_processes=top_process_union(processes, top_n),
            )
        except (KeyError, TypeError, ValidationError) as exc:
            raise CollectorError(f"process_snapshot.ps1 returned malformed data: {exc}") from exc

    def sensors(self, window: TimeWindow | None) -> list[SensorSnapshot]:
        if window is not None:
            record_note(
                "live sensors have no history: returned one snapshot taken now; "
                "the requested window was ignored"
            )
        readings: list[SensorReading] = []
        unavailable: list[str] = []
        for backend in (self._lhm_readings, self._nvidia_smi_readings):
            try:
                readings.extend(backend())
            except CollectorUnavailable as exc:
                unavailable.append(exc.reason)
        try:
            readings.extend(self._cim_readings(unavailable))
        except CollectorError as exc:
            unavailable.append(f"thermal zone / GPU engine counters: {exc}")
        return [
            SensorSnapshot(taken_at=datetime.now(UTC), readings=readings, unavailable=unavailable)
        ]

    def _lhm_readings(self) -> list[SensorReading]:
        record_source(f"GET {LHM_URL}")
        try:
            body = self._http_get(LHM_URL, 2.0)
            readings = parse_lhm_data_json(json.loads(body))
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise CollectorUnavailable("librehardwaremonitor", LHM_UNREACHABLE) from exc
        except (ValueError, CollectorError) as exc:
            raise CollectorUnavailable(
                "librehardwaremonitor", f"LibreHardwareMonitor data.json unreadable: {exc}"
            ) from exc
        if not readings:
            raise CollectorUnavailable(
                "librehardwaremonitor", "LibreHardwareMonitor reported no supported sensors"
            )
        return readings

    def _nvidia_smi_readings(self) -> list[SensorReading]:
        exe = self._which("nvidia-smi") or self._which("nvidia-smi.exe")
        if exe is None:
            raise CollectorUnavailable("nvidia-smi", "nvidia-smi not found on PATH")
        argv = [exe, f"--query-gpu={NVIDIA_SMI_QUERY}", "--format=csv,noheader,nounits"]
        record_source(" ".join(["nvidia-smi", *argv[1:]]))
        try:
            proc = self._run(argv, None, 15.0)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise CollectorUnavailable("nvidia-smi", f"nvidia-smi did not run: {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[:200]
            raise CollectorUnavailable("nvidia-smi", f"nvidia-smi failed: {detail}")
        return parse_nvidia_smi(proc.stdout)

    def _cim_readings(self, unavailable: list[str]) -> list[SensorReading]:
        data = self._invoke("sensors_cim.ps1", {})
        readings: list[SensorReading] = []
        backends = (
            (
                "thermal",
                "ACPI thermal zones (Win32_PerfFormattedData_Counters_ThermalZoneInformation)",
                parse_thermal_zones,
            ),
            (
                "gpu_engine",
                "GPU engine counters (Win32_PerfFormattedData_GPUPerformanceCounters_GPUEngine)",
                aggregate_gpu_engines,
            ),
        )
        for key, label, parse in backends:
            section = data.get(key) if isinstance(data, dict) else None
            if not isinstance(section, dict) or not section.get("ok"):
                reason = section.get("error") if isinstance(section, dict) else "no data"
                unavailable.append(f"{label}: {reason}")
                continue
            parsed = parse(section.get("rows") or [])
            if not parsed:
                unavailable.append(f"{label}: no readings")
            readings.extend(parsed)
        return readings

    def drivers(self, device_classes: list[str]) -> list[DriverInfo]:
        data = self._invoke("drivers.ps1", {"device_classes": device_classes})
        try:
            return _DRIVERS.validate_python(data or [])
        except ValidationError as exc:
            raise CollectorError(f"drivers.ps1 returned malformed data: {exc}") from exc

    def system_info(self) -> SystemInfo:
        data = self._invoke("system_info.ps1", {})
        try:
            build = str(data["os_build"])
            if data.get("ubr") is not None:
                build = f"{build}.{data['ubr']}"
            gpus = [
                GpuInfo(
                    name=g["name"],
                    vendor=gpu_vendor(g.get("pci_vendor_id"), g.get("compatibility"), g["name"]),
                    driver_version=g.get("driver_version") or "",
                    driver_date=g.get("driver_date"),
                )
                for g in data.get("gpus") or []
            ]
            return SystemInfo(
                os_caption=data["os_caption"],
                os_build=build,
                gpus=gpus,
                utc_offset_minutes=int(data["utc_offset_minutes"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CollectorError(f"system_info.ps1 returned malformed data: {exc}") from exc

    def dxdiag(self) -> DxDiagSummary:
        data = self._invoke(
            "dxdiag.ps1", {"wait_seconds": DXDIAG_WAIT_S}, timeout=DXDIAG_WAIT_S + 30
        )
        if not isinstance(data, dict) or not isinstance(data.get("xml"), str):
            raise CollectorError("dxdiag.ps1 returned no XML")
        return parse_dxdiag_xml(data["xml"])

    def identity(self) -> dict[str, str]:
        """Host identifiers for the fixture scrubber (record.py only; not an MCP tool)."""
        data = self._invoke("identity.ps1", {})
        return {k: str(v) for k, v in (data or {}).items() if v}
