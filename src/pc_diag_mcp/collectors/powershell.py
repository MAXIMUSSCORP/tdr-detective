"""Live collector: fixed PowerShell scripts over CIM / Get-WinEvent.

Every call runs one version-controlled script from ``pc_diag_mcp/ps`` as::

    powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File <script>

with an argument list (never a shell), parameters as JSON on stdin and a JSON envelope
(``{"ok": true, "data": ...}`` or ``{"ok": false, "kind": "unavailable"|"error", ...}``) on
stdout. Scripts only read; the single exception is dxdiag's temporary report file, which the
script deletes.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import platform
import secrets
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import IO, Any

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
    "sensors": ("lhm_http.ps1", "sensors_cim.ps1"),
    "drivers": ("drivers.ps1",),
    "system_info": ("system_info.ps1",),
    "dxdiag": ("dxdiag.ps1", "dxdiag_cleanup.ps1"),
}
COMMANDS_BY_METHOD: dict[str, tuple[str, ...]] = {
    "sensors": (
        f"GET {LHM_URL} (only if LibreHardwareMonitor's web server is running; under WSL "
        "this request is made by lhm_http.ps1 on the Windows side)",
        f"nvidia-smi --query-gpu={NVIDIA_SMI_QUERY} --format=csv,noheader,nounits",
    ),
}

Env = Mapping[str, str]
Runner = Callable[
    [Sequence[str], str | None, float, Env | None], "subprocess.CompletedProcess[str]"
]
HttpGet = Callable[[str, float], bytes]
Exists = Callable[[Path], bool]

MAX_STDOUT_BYTES = 32 * 1024 * 1024
MAX_STDERR_BYTES = 1024 * 1024
NVIDIA_SMI_MISSING = "nvidia-smi not found in System32, NVIDIA NVSMI or /usr/lib/wsl/lib"
WSLPATH = "/usr/bin/wslpath"
# Variables WSL interop needs to launch Windows executables from Linux.
_WSL_INTEROP_VARS = ("WSL_INTEROP", "WSL_DISTRO_NAME", "WSLENV")


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """Best effort: on Windows take the children (e.g. dxdiag) down too."""
    if sys.platform == "win32":
        root = os.environ.get("SYSTEMROOT", "C:\\Windows")
        taskkill = Path(root) / "System32" / "taskkill.exe"
        if taskkill.is_file():
            subprocess.run(
                [str(taskkill), "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=15,
                check=False,
            )
    proc.kill()


def run_process(
    argv: Sequence[str],
    stdin: str | None,
    timeout: float,
    env: Env | None = None,
    *,
    max_stdout: int = MAX_STDOUT_BYTES,
) -> subprocess.CompletedProcess[str]:
    """Like ``subprocess.run`` but with stdout capped: an oversized reply kills the process."""
    proc = subprocess.Popen(
        list(argv),
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env) if env is not None else None,
    )
    out, err = bytearray(), bytearray()
    overflow = threading.Event()

    def pump(stream: IO[bytes], buffer: bytearray, cap: int, fatal: bool) -> None:
        while chunk := stream.read(65536):
            if len(buffer) + len(chunk) > cap:
                if fatal:
                    overflow.set()
                    _kill_tree(proc)
                    return
                continue  # stderr: keep draining, stop storing
            buffer.extend(chunk)

    if proc.stdout is None or proc.stderr is None:
        raise CollectorError("subprocess pipes were not created")
    pumps = [
        threading.Thread(target=pump, args=(proc.stdout, out, max_stdout, True), daemon=True),
        threading.Thread(
            target=pump, args=(proc.stderr, err, MAX_STDERR_BYTES, False), daemon=True
        ),
    ]
    for thread in pumps:
        thread.start()
    if proc.stdin is not None:
        with contextlib.suppress(OSError):
            proc.stdin.write((stdin or "").encode("utf-8"))
            proc.stdin.close()
    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        proc.wait(timeout=15)
        raise
    finally:
        for thread in pumps:
            thread.join(timeout=15)
    if overflow.is_set():
        raise CollectorError(f"{Path(argv[0]).name} output exceeded {max_stdout // 2**20} MB")
    return subprocess.CompletedProcess(
        list(argv),
        returncode,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
    )


def find_windows_root(exists: Exists = Path.is_file, mounts: Path = Path("/mnt")) -> Path | None:
    """The Windows directory: %SystemRoot% natively, /mnt/<drive>/Windows under WSL."""
    if sys.platform == "win32":
        root = os.environ.get("SYSTEMROOT")
        return Path(root) if root else None
    if not running_under_wsl():
        return None
    drives = sorted(mounts.glob("[a-z]"), key=lambda m: (m.name != "c", m.name))
    for mount in drives:
        candidate = mount / "Windows"
        if exists(candidate / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"):
            return candidate
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Whatever listens on the port could redirect to /Sensor?action=Set or off-host."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def http_get_localhost(url: str, timeout: float) -> bytes:
    parts = urllib.parse.urlsplit(url)
    if (
        parts.scheme != "http"
        or parts.hostname != "127.0.0.1"
        or parts.path != "/data.json"
        or (parts.query or parts.username or parts.password)
    ):
        raise ValueError("only http://127.0.0.1:<port>/data.json may be fetched")
    # No proxy (stay on loopback) and no redirects (a 3xx surfaces as an HTTPError).
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
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
        wsl: bool | None = None,
        script_dir: Path = PS_DIR,
        windows_root: Path | None = None,
        exists: Exists = Path.is_file,
    ) -> None:
        self._timeout = timeout_s
        self._run = runner
        self._http_get = http_get
        self._wsl = running_under_wsl() if wsl is None else wsl
        self._script_dir = script_dir
        self._exists = exists
        self._windows_root = windows_root if windows_root is not None else find_windows_root(exists)
        self._script_paths: dict[str, str] = {}

    @property
    def source(self) -> CollectorSource:
        return "live"

    # ------------------------------------------------------------------ #
    # Invocation
    # ------------------------------------------------------------------ #

    def _powershell(self) -> str:
        # Only the system copy: never resolve through PATH, which another program could prepend.
        root = self._windows_root
        if root is not None:
            pinned = root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            if self._exists(pinned):
                return str(pinned)
        raise CollectorUnavailable(
            "powershell.exe", "not found at %SystemRoot%\\System32\\WindowsPowerShell\\v1.0"
        )

    def _nvidia_smi(self) -> str:
        candidates: list[Path] = []
        root = self._windows_root
        if root is not None:
            candidates.append(root / "System32" / "nvidia-smi.exe")
            nvsmi = ("Program Files", "NVIDIA Corporation", "NVSMI", "nvidia-smi.exe")
            if self._wsl:
                candidates.append(root.parent.joinpath(*nvsmi))  # /mnt/c/Program Files/...
            else:
                # Native Windows: Program Files on the Windows directory's drive, built with
                # Windows path rules so the result is the same on any OS (and %PROGRAMFILES%,
                # which 32-bit Python redirects to "Program Files (x86)", cannot steer it).
                candidates.append(Path(str(PureWindowsPath(root).parent.joinpath(*nvsmi))))
        if self._wsl:
            candidates.append(Path("/usr/lib/wsl/lib/nvidia-smi"))
        for candidate in candidates:
            if self._exists(candidate):
                return str(candidate)
        raise CollectorUnavailable("nvidia-smi", NVIDIA_SMI_MISSING)

    def _env(self) -> dict[str, str]:
        """A minimal environment, so nothing inherited (PATH, PSModulePath...) steers the child."""
        if self._wsl:
            env = {"PATH": "/usr/bin:/bin"}
            env.update({k: os.environ[k] for k in _WSL_INTEROP_VARS if k in os.environ})
            return env
        root = str(self._windows_root or os.environ.get("SYSTEMROOT", ""))
        env = {"SystemRoot": root, "windir": root}
        for name in ("SystemDrive", "TEMP", "TMP"):
            if os.environ.get(name):
                env[name] = os.environ[name]
        system32 = f"{root}\\System32"
        env["PATH"] = ";".join(
            [system32, root, f"{system32}\\Wbem", f"{system32}\\WindowsPowerShell\\v1.0"]
        )
        return env

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
            converted = self._run([WSLPATH, "-w", resolved], None, 10.0, self._env())
            if converted.returncode != 0 or not converted.stdout.strip():
                raise CollectorError(f"wslpath could not convert {resolved}")
            resolved = converted.stdout.strip()
        self._script_paths[name] = resolved
        return resolved

    def _invoke(self, name: str, params: Mapping[str, Any], timeout: float | None = None) -> Any:
        """Run one fixed script and return its ``data`` payload."""
        record_source(f"ps/{name}")
        # -ExecutionPolicy Bypass is process-scoped and only lets our own packaged scripts run.
        # Script integrity is not hash-checked on purpose: anyone able to modify these .ps1
        # files can equally modify the Python that launches them, so a hash adds nothing.
        argv = [self._powershell(), *POWERSHELL_FLAGS, self._script_path(name)]
        limit = timeout or self._timeout
        # ensure_ascii keeps stdin independent of the console code page.
        stdin = json.dumps(dict(params), ensure_ascii=True)
        try:
            proc = self._run(argv, stdin, limit, self._env())
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
        records: list[EventRecord] = []
        failures: list[str] = []
        for item in data:
            try:
                records.append(parse_event(str(item.get("xml") or ""), item.get("message")))
            except CollectorError as exc:
                failures.append(str(exc))
        if failures:
            # One corrupt record must not hide the rest, but losing all of them is an error.
            if not records:
                raise CollectorError(f"no event could be parsed: {failures[0]}")
            record_note(f"skipped {len(failures)} unparseable event(s): {failures[0]}")
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

    def _lhm_body(self) -> bytes:
        if self._wsl:
            # 127.0.0.1 inside WSL (NAT mode) is the Linux loopback; fetch on the Windows side.
            try:
                data = self._invoke("lhm_http.ps1", {})
            except CollectorUnavailable as exc:
                raise OSError(exc.reason) from exc
            body = data.get("body") if isinstance(data, dict) else None
            if not isinstance(body, str):
                raise ValueError("lhm_http.ps1 returned no body")
            return body.encode("utf-8")
        record_source(f"GET {LHM_URL}")
        return self._http_get(LHM_URL, 2.0)

    def _lhm_readings(self) -> list[SensorReading]:
        try:
            readings = parse_lhm_data_json(json.loads(self._lhm_body()))
        except (urllib.error.URLError, http.client.HTTPException, OSError, TimeoutError) as exc:
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
        exe = self._nvidia_smi()
        argv = [exe, f"--query-gpu={NVIDIA_SMI_QUERY}", "--format=csv,noheader,nounits"]
        record_source(" ".join(["nvidia-smi", *argv[1:]]))
        try:
            proc = self._run(argv, None, 15.0, self._env())
        except (subprocess.TimeoutExpired, OSError, CollectorError) as exc:
            raise CollectorUnavailable("nvidia-smi", f"nvidia-smi did not run: {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[:200]
            raise CollectorUnavailable("nvidia-smi", f"nvidia-smi failed: {detail}")
        readings = parse_nvidia_smi(proc.stdout)
        if not readings:
            raise CollectorUnavailable("nvidia-smi", "nvidia-smi produced no parsable readings")
        return readings

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
        token = secrets.token_hex(8)
        try:
            data = self._invoke(
                "dxdiag.ps1",
                {"wait_seconds": DXDIAG_WAIT_S, "token": token},
                timeout=DXDIAG_WAIT_S + 30,
            )
        except CollectorError:
            # Killing powershell.exe (e.g. on our timeout) can orphan dxdiag; stop it by token.
            with contextlib.suppress(CollectorError):
                self._invoke("dxdiag_cleanup.ps1", {"token": token}, timeout=30)
            raise
        if not isinstance(data, dict) or not isinstance(data.get("xml"), str):
            raise CollectorError("dxdiag.ps1 returned no XML")
        return parse_dxdiag_xml(data["xml"])

    def identity(self) -> dict[str, str]:
        """Host identifiers for the fixture scrubber (record.py only; not an MCP tool)."""
        data = self._invoke("identity.ps1", {})
        return {k: str(v) for k, v in (data or {}).items() if v}
