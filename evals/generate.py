r"""Deterministic synthetic scenario generator for the TDR Detective eval.

    python -m evals.generate            # write evals/scenarios/* and evals/cases.jsonl
    python -m evals.generate --check    # regenerate into a temp dir, exit 1 on drift

Each :class:`evals.specs.CaseSpec` becomes a replay directory in the ``ReplayScenario``
layout (``scenario.json``, ``events.json`` and optional telemetry files). All randomness
comes from ``random.Random`` seeded with ``SEED`` and the case id, so output is
byte-for-byte reproducible.

Recorded cases are not generated: their scrubbed captures live in ``scenarios/rec-*`` and
their labels in the hand-written ``recorded_cases.jsonl``. ``cases.jsonl`` lists the
synthetic cases followed by the recorded ones; generation and ``--check`` leave the
``rec-*`` directories alone.

Noise model
-----------
The signal events of a case are embedded in background events at rates meant to
resemble a lightly used Windows 10/11 home desktop. **These rates are estimates from
experience with typical consumer System/Application logs, not values fitted to a
measured corpus.** They are deliberately not inflated: real logs are often noisier
(chatty services, vendor agents, Kernel-PnP storms), so the token cost of the
``raw_dump`` baseline is if anything understated, and the skill gets no help from an
artificially large haystack. Rates are per hour of machine uptime (no events while
the machine is off or asleep), drawn as Poisson processes:

=====================================  ===========  =====================================
event                                  rate / hour  note
=====================================  ===========  =====================================
Service Control Manager 7036           3.0          start/stop pairs of trigger services
Service Control Manager 7040           0.15         BITS start type flips
DistributedCOM 10016                   0.6          well-known shell/broker CLSIDs
Kernel-General 16                      0.5          registry hive access-history flush
Kernel-General 1                       0.1          time sync
Display 4123 / 4127                    0.2          only on machines with an HDR display
WindowsUpdateClient 44/43/19           0.125        Defender security intelligence update
Security-SPP 16384 (Application)       0.3          Software Protection reschedule
RestartManager 10000/10001 (App)       0.25         installers and updaters
SecurityCenter 15 (Application)        0.08         Defender status refresh
VSS 8224 (Application)                 0.15         idle shutdown of the VSS service
Application Error 1000 + WER 1001      0.04         unrelated process crash (~1/day)
=====================================  ===========  =====================================

Every boot adds a burst: Kernel-General 12, Kernel-Boot 18/32/27/20/153,
Kernel-General 1, EventLog 6005/6009/6013, Winlogon 7001, 6 to 11 SCM 7036 starts,
1 to 3 DCOM 10016, Kernel-PnP 219 (60% of boots), and in Application Security-SPP
16394/16384, Desktop Window Manager 9027, Winlogon 6003 (50%) and SecurityCenter 15.
EventLog 6013 is also logged daily at 12:00 local time.

Two machine quirks are modeled on the host the demo runs on: recurring Application
LiveKernelEvent with P1 1b0 and recurring WHEA-Logger 17 (corrected PCIe AER).

Background events are not suppressed around the incident: unrelated crashes, live
dumps and corrected WHEA records can land next to it, as in real logs. The one exception
is HDR mode changes (Display 4123/4127), which are kept 20 minutes away from every signal
because a mode switch genuinely blanks the screen and would change the correct answer.
On top of the natural noise, specs place deliberate decoys (``Noise`` signals or
misleading earlier events) near the incident in cases of every kind; those cases are
tagged ``red-herring``, so "something odd right before the incident" is not a tell.

Message formats follow real rendered messages, collapsed to one line: Kernel-Power 41
``BugcheckCode`` is decimal; WER-SystemErrorReporting 1001 prints the bugcheck in hex;
LiveKernelEvent reports are Application "Windows Error Reporting" 1001 with an empty
fault bucket; 0x117/0x141 only ever appear as live dumps. Known approximations: the
exact text of Display 4123/4127 is paraphrased (the IDs and provider are real), and
EventData field names are included only where known; otherwise ``data`` is empty or
uses ``paramN`` names for unnamed insertion strings.
"""

from __future__ import annotations

import argparse
import hashlib
import random
import shutil
import sys
import tempfile
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from pydantic import TypeAdapter

from evals.schema import EvalCase, Expected, load_cases
from evals.specs import (
    SPECS,
    App,
    AppCrash,
    AppHang,
    BadBlock,
    CaseSpec,
    CpuError,
    Crash,
    DiskRetry,
    DriverInstall,
    Frametimes,
    LiveDump,
    LogCleared,
    LowMemory,
    Machine,
    Noise,
    PagingError,
    PowerCut,
    Restart,
    SensorSeries,
    ServiceInstall,
    Signal,
    Sleep,
    StorageReset,
    Tdr,
    UpdateInstall,
    WheaCorrectedCpu,
    WheaPcie,
)
from pc_diag_mcp.models import (
    DiskState,
    DisplayDevice,
    DriverInfo,
    DxDiagSummary,
    EventLevel,
    EventRecord,
    FrameTimeSample,
    GpuInfo,
    MemoryState,
    ProcessInfo,
    ProcessSnapshot,
    ReplayScenario,
    RootCauseCategory,
    SensorReading,
    SensorSnapshot,
    SystemInfo,
)

SEED = 20261023
EVALS_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVALS_DIR.parent
RECORDED_CASES = EVALS_DIR / "recorded_cases.jsonl"
RECORDED_PREFIX = "rec-"
GIB = 1024**3
MIB = 1024**2
PROTECTED_MIN = 20.0
SENSOR_SOURCE = "LibreHardwareMonitor"

NOISE_RATES_PER_HOUR: dict[str, float] = {
    "scm_pair": 1.5,  # each pair is two 7036 events
    "scm_7040": 0.15,
    "dcom": 0.6,
    "kgeneral16": 0.5,
    "kgeneral1": 0.1,
    "hdr": 0.2,
    "defender_update": 0.125,
    "spp": 0.3,
    "restart_manager": 0.25,
    "security_center": 0.08,
    "vss": 0.15,
    "app_crash": 0.04,
}
PROTECTED_KINDS = {"hdr"}

L = EventLevel


# --------------------------------------------------------------------------- #
# Small formatting helpers
# --------------------------------------------------------------------------- #


def _rng_for(*parts: object) -> random.Random:
    digest = hashlib.sha256(":".join(str(p) for p in (SEED, *parts)).encode()).digest()
    # Seeded PRNG on purpose: synthetic data must be reproducible, nothing here is secret.
    return random.Random(int.from_bytes(digest[:8], "big"))  # noqa: S311


def _systime(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond:06d}000Z"


def _hex16(value: int) -> str:
    return f"0x{value:016x}"


def _local_dmy(t: datetime) -> str:
    hour = t.hour % 12 or 12
    ampm = "AM" if t.hour < 12 else "PM"
    return f"{hour}:{t.minute:02d}:{t.second:02d} {ampm} on {t.month}/{t.day}/{t.year}"


_SERVICES = (
    "Windows Update",
    "Background Intelligent Transfer Service",
    "Windows Modules Installer",
    "Delivery Optimization",
    "WinHTTP Web Proxy Auto-Discovery Service",
    "Device Setup Manager",
    "Microsoft Store Install Service",
    "Update Orchestrator Service",
    "Windows Error Reporting Service",
    "Diagnostic System Host",
    "Software Protection",
    "Microsoft Edge Update Service (edgeupdate)",
    "Capability Access Manager Service",
    "Windows Event Log",
    "Network Setup Service",
)
_BOOT_SERVICES = (
    "Windows Push Notifications User Service_{s}",
    "Clipboard User Service_{s}",
    "Connected Devices Platform User Service_{s}",
    "Network List Service",
    "Storage Service",
    "Radio Management Service",
    "Windows Image Acquisition (WIA)",
    "Diagnostic Service Host",
    "Program Compatibility Assistant Service",
    "Bluetooth Support Service",
    "Touch Keyboard and Handwriting Panel Service",
    "AppX Deployment Service (AppXSVC)",
)
_DCOM = (
    ("{2593F8B9-4EAF-457C-B68A-50F6B8EA6B54}", "{15C20B67-12E7-4BB6-92BB-7AFF07997402}", "user"),
    ("{C2F03A33-21F5-47FA-B4BB-156362A2F239}", "{316CDED5-E4AE-4B15-9113-7055D84DCC97}", "local"),
    ("{6B3B8D23-FA8D-40B9-8DBD-B950333E2C52}", "{4839DDB7-58C2-48F5-8283-E1D1807D0D7D}", "local"),
)
_HIVES = (
    r"\??\C:\Users\{u}\ntuser.dat",
    r"\??\C:\Users\{u}\AppData\Local\Microsoft\Windows\UsrClass.dat",
    r"\??\C:\Windows\AppCompat\Programs\Amcache.hve",
    r"\??\C:\Users\{u}\AppData\Local\Packages\Microsoft.Windows.Search_cw5n1h2txyewy\Settings\settings.dat",
    r"\??\C:\ProgramData\Microsoft\Windows\AppRepository\Packages\Microsoft.Windows.ShellExperienceHost_10.0.22621.4111_neutral_neutral_cw5n1h2txyewy\ActivationStore.dat",
)


@dataclass(frozen=True)
class _NoiseCrash:
    exe: str
    version: str
    timestamp: str
    module: str
    module_version: str
    module_timestamp: str
    exception: str
    offset: str
    path: str
    module_path: str
    package: str
    app_id: str


def _noise_crashes(machine: Machine) -> tuple[_NoiseCrash, ...]:
    os_ver = f"10.0.{machine.os_build}"
    user = machine.user
    return (
        _NoiseCrash(
            "SearchHost.exe",
            "2124.33803.0.0",
            "66d8f2a1",
            "KERNELBASE.dll",
            os_ver,
            "10c46e71",
            "0xc000027b",
            "0x000000000010b0e6",
            r"C:\Windows\SystemApps\MicrosoftWindows.Client.CBS_cw5n1h2txyewy\SearchHost.exe",
            r"C:\WINDOWS\System32\KERNELBASE.dll",
            "MicrosoftWindows.Client.CBS_1000.22700.1034.0_x64__cw5n1h2txyewy",
            "CortanaUI",
        ),
        _NoiseCrash(
            "StartMenuExperienceHost.exe",
            os_ver,
            "7b3e19a0",
            "Windows.UI.Xaml.dll",
            os_ver,
            "5e1fd2c3",
            "0xc000027b",
            "0x0000000000870e3d",
            r"C:\Windows\SystemApps\Microsoft.Windows.StartMenuExperienceHost_cw5n1h2txyewy\StartMenuExperienceHost.exe",
            r"C:\Windows\System32\Windows.UI.Xaml.dll",
            "Microsoft.Windows.StartMenuExperienceHost_10.0.22621.4111_neutral_neutral_cw5n1h2txyewy",
            "App",
        ),
        _NoiseCrash(
            "steamwebhelper.exe",
            "8.82.93.75",
            "66b0c4d1",
            "libcef.dll",
            "120.0.6099.234",
            "65a1f0e2",
            "0x80000003",
            "0x0000000003b6a1f2",
            r"C:\Program Files (x86)\Steam\bin\cef\cef.win7x64\steamwebhelper.exe",
            r"C:\Program Files (x86)\Steam\bin\cef\cef.win7x64\libcef.dll",
            "",
            "",
        ),
        _NoiseCrash(
            "OneDrive.exe",
            "24.180.908.2",
            "66e2b7c4",
            "ntdll.dll",
            os_ver,
            "9d4b2a11",
            "0xc0000409",
            "0x00000000000a3c5e",
            rf"C:\Users\{user}\AppData\Local\Microsoft\OneDrive\OneDrive.exe",
            r"C:\WINDOWS\SYSTEM32\ntdll.dll",
            "",
            "",
        ),
        _NoiseCrash(
            "PhoneExperienceHost.exe",
            "1.24082.97.0",
            "66c3e5a8",
            "KERNELBASE.dll",
            os_ver,
            "10c46e71",
            "0xe0434352",
            "0x000000000005fb4c",
            r"C:\Program "
            r"Files\WindowsApps\Microsoft.YourPhone_1.24082.97.0_x64__8wekyb3d8bbwe\PhoneExperienceHost.exe",
            r"C:\WINDOWS\System32\KERNELBASE.dll",
            "Microsoft.YourPhone_1.24082.97.0_x64__8wekyb3d8bbwe",
            "App",
        ),
    )


# --------------------------------------------------------------------------- #
# Event builder
# --------------------------------------------------------------------------- #


@dataclass
class _Raw:
    time: datetime
    log: str
    provider: str
    event_id: int
    level: EventLevel
    message: str
    data: dict[str, str]
    noise: bool
    seq: int


@dataclass(frozen=True)
class Bundle:
    case: EvalCase
    scenario: ReplayScenario
    incident: datetime
    events: list[EventRecord]
    noise_count: int
    sensors: list[SensorSnapshot] | None
    process: ProcessSnapshot
    drivers: list[DriverInfo]
    dxdiag: DxDiagSummary
    frametimes: list[FrameTimeSample] | None


@dataclass
class _Builder:
    spec: CaseSpec
    rng: random.Random
    incident: datetime
    now: datetime
    tz: timezone
    raws: list[_Raw] = field(default_factory=list)
    defender_version: int = 0
    intervals: list[tuple[datetime, datetime]] = field(default_factory=list)
    boots: list[datetime] = field(default_factory=list)

    @property
    def m(self) -> Machine:
        return self.spec.machine

    def at(self, minutes: float) -> datetime:
        return self.incident + timedelta(minutes=minutes)

    def add(
        self,
        t: datetime,
        log: str,
        provider: str,
        event_id: int,
        level: EventLevel,
        message: str,
        data: dict[str, str] | None = None,
        *,
        noise: bool,
    ) -> None:
        self.raws.append(
            _Raw(t, log, provider, event_id, level, message, data or {}, noise, len(self.raws))
        )

    def guid(self) -> str:
        return str(uuid.UUID(int=self.rng.getrandbits(128), version=4))

    def jitter(self, t: datetime, max_s: float) -> datetime:
        return t + timedelta(seconds=self.rng.uniform(0.0, max_s))

    @property
    def user_sid(self) -> str:
        h = int(hashlib.sha256(self.m.hostname.encode()).hexdigest(), 16)
        parts = [(h >> s) % 4_000_000_000 for s in (0, 40, 80)]
        return f"S-1-5-21-{parts[0]}-{parts[1]}-{parts[2]}-1001"

    @property
    def svc_suffix(self) -> str:
        return hashlib.sha256(self.m.user.encode()).hexdigest()[:5]

    # ---- background noise ------------------------------------------------

    def noise_event(self, kind: str, t: datetime, *, noise: bool = True) -> None:
        _NOISE_RENDERERS[kind](self, t, noise)

    def poisson(self, rate: float, start: datetime, end: datetime) -> list[datetime]:
        times: list[datetime] = []
        if rate <= 0:
            return times
        t = start
        while True:
            t += timedelta(hours=self.rng.expovariate(rate))
            if t >= end:
                return times
            times.append(t)

    def background(self) -> None:
        rates = dict(NOISE_RATES_PER_HOUR)
        if not self.m.hdr_display:
            rates["hdr"] = 0.0
        rates["live_dump_1b0"] = self.m.live_dump_1b0_per_hour
        rates["whea17"] = self.m.whea17_per_hour
        anchors = [self.incident] + [self.at(sig.at) for sig in self.spec.signals]
        for start, end in self.intervals:
            for kind, rate in rates.items():
                for t in self.poisson(rate, start, end):
                    near = any(abs((t - a).total_seconds()) < PROTECTED_MIN * 60 for a in anchors)
                    if kind in PROTECTED_KINDS and near:
                        continue
                    self.noise_event(kind, t)
            self.daily_uptime(start, end)

    def daily_uptime(self, start: datetime, end: datetime) -> None:
        local = start.astimezone(self.tz)
        noon = local.replace(hour=12, minute=0, second=0, microsecond=0)
        if noon < local:
            noon += timedelta(days=1)
        while noon.astimezone(UTC) < end:
            t = noon.astimezone(UTC)
            last_boot = max(b for b in self.boots if b <= t)
            uptime = int((t - last_boot).total_seconds())
            self.add(
                t,
                "System",
                "EventLog",
                6013,
                L.INFORMATION,
                f"The system uptime is {uptime} seconds.",
                noise=True,
            )
            noon += timedelta(days=1)

    # ---- boot / shutdown -------------------------------------------------

    def boot(
        self,
        t: datetime,
        clean_before: bool,
        crash: Crash | PowerCut | None,
        crashed_at: datetime | None,
    ) -> None:
        sys_add = self._sys
        sys_add(
            t,
            "Microsoft-Windows-Kernel-General",
            12,
            L.INFORMATION,
            f"The operating system started at system time {_systime(t)}.",
            {"StartTime": _systime(t)},
        )
        tb = t + timedelta(seconds=0.4)
        sys_add(
            tb,
            "Microsoft-Windows-Kernel-Boot",
            18,
            L.INFORMATION,
            "There are 0x1 boot options on this system.",
        )
        sys_add(
            tb,
            "Microsoft-Windows-Kernel-Boot",
            32,
            L.INFORMATION,
            "The bootmgr spent 0 ms waiting for user input.",
        )
        sys_add(
            tb,
            "Microsoft-Windows-Kernel-Boot",
            27,
            L.INFORMATION,
            "The boot type was 0x0.",
            {"BootType": "0"},
        )
        good = "true" if clean_before else "false"
        sys_add(
            tb,
            "Microsoft-Windows-Kernel-Boot",
            20,
            L.INFORMATION,
            f"The last shutdown's success status was {good}. The last boot's success status was "
            "true.",
            {"LastShutdownGood": good, "LastBootGood": "true"},
        )
        sys_add(
            tb,
            "Microsoft-Windows-Kernel-Boot",
            153,
            L.INFORMATION,
            "Virtualization-based security (policies: 0) is disabled.",
        )
        t1 = t + timedelta(seconds=1.6)
        old = t1 - timedelta(seconds=self.rng.uniform(0.5, 3.0))
        sys_add(
            t1,
            "Microsoft-Windows-Kernel-General",
            1,
            L.INFORMATION,
            f"The system time has changed to {_systime(t1)} from {_systime(old)}. "
            "Change Reason: System time synchronized with the hardware clock.",
            {"NewTime": _systime(t1), "OldTime": _systime(old), "Reason": "2"},
        )
        if crash is not None and crashed_at is not None:
            self.unexpected_shutdown_records(t, crash, crashed_at)
        tl = t + timedelta(seconds=14)
        major = self.m.os_build.split(".")[0]
        sys_add(tl, "EventLog", 6005, L.INFORMATION, "The Event log service was started.")
        sys_add(
            tl,
            "EventLog",
            6009,
            L.INFORMATION,
            f"Microsoft (R) Windows (R) 10.00. {major} Multiprocessor Free.",
        )
        sys_add(tl, "EventLog", 6013, L.INFORMATION, "The system uptime is 14 seconds.")
        if self.rng.random() < 0.6:
            dev = "SWD\\WPDBUSENUM\\{" + self.guid() + "}#0000000000100000"
            sys_add(
                self.jitter(t + timedelta(seconds=18), 6),
                "Microsoft-Windows-Kernel-PnP",
                219,
                L.WARNING,
                f"The driver \\Driver\\WUDFRd failed to load for the device {dev}.",
                {
                    "DriverNameLength": "14",
                    "DriverName": "\\Driver\\WUDFRd",
                    "Status": "0xC0000365",
                    "FailureName": "\\Driver\\WUDFRd",
                    "Version": "0",
                },
            )
        logon = t + timedelta(seconds=self.rng.uniform(30, 55))
        sys_add(
            logon,
            "Microsoft-Windows-Winlogon",
            7001,
            L.INFORMATION,
            "User Logon Notification for Customer Experience Improvement Program",
            {"TSId": "1", "UserSid": self.user_sid},
        )
        for name in self.rng.sample(_BOOT_SERVICES, self.rng.randint(6, 11)):
            svc = name.format(s=self.svc_suffix)
            self._scm_state(self.jitter(t + timedelta(seconds=20), 100), svc, "running", noise=True)
        for _ in range(self.rng.randint(1, 3)):
            self._dcom(self.jitter(logon, 40), noise=True)
        app = self._app
        app(
            self.jitter(t + timedelta(seconds=28), 4),
            "Microsoft-Windows-Security-SPP",
            16394,
            L.INFORMATION,
            "Offline downlevel migration succeeded.",
        )
        self._spp(self.jitter(t + timedelta(seconds=33), 4), noise=True)
        app(
            self.jitter(logon, 5),
            "Desktop Window Manager",
            9027,
            L.INFORMATION,
            "The Desktop Window Manager has registered the session port.",
        )
        if self.rng.random() < 0.5:
            app(
                self.jitter(logon, 8),
                "Microsoft-Windows-Winlogon",
                6003,
                L.WARNING,
                "The winlogon notification subscriber <SessionEnv> was unavailable to handle a "
                "critical notification event.",
                {"SubscriberName": "SessionEnv"},
            )
        self._security_center(self.jitter(logon + timedelta(seconds=15), 20), noise=True)

    def unexpected_shutdown_records(
        self, boot: datetime, crash: Crash | PowerCut, crashed_at: datetime
    ) -> None:
        code = crash.code if isinstance(crash, Crash) else 0
        params = crash.params if isinstance(crash, Crash) else (0, 0, 0, 0)
        data = {
            "BugcheckCode": str(code),
            "BugcheckParameter1": hex(params[0]),
            "BugcheckParameter2": hex(params[1]),
            "BugcheckParameter3": hex(params[2]),
            "BugcheckParameter4": hex(params[3]),
            "SleepInProgress": str(crash.sleep_in_progress if isinstance(crash, Crash) else 0),
            "PowerButtonTimestamp": str(
                crash.power_button_ms if isinstance(crash, PowerCut) else 0
            ),
            "BootAppStatus": "0",
            "Checkpoint": "0",
            "ConnectedStandbyInProgress": "false",
            "SystemSleepTransitionsToOn": "0",
            "CsEntryScenarioInstanceId": "0",
            "BugcheckInfoFromEFI": "true" if code else "false",
            "CheckpointStatus": "0",
            "CsEntryScenarioInstanceIdV2": "0",
            "LongPowerButtonPressDetected": "false",
        }
        self._sys(
            boot + timedelta(seconds=2.9),
            "Microsoft-Windows-Kernel-Power",
            41,
            L.CRITICAL,
            "The system has rebooted without cleanly shutting down first. This error could be "
            "caused "
            "if the system stopped responding, crashed, or lost power unexpectedly.",
            data,
            noise=False,
        )
        last = crashed_at.astimezone(self.tz)
        self._sys(
            boot + timedelta(seconds=14),
            "EventLog",
            6008,
            L.ERROR,
            f"The previous system shutdown at {_local_dmy(last)} was unexpected.",
            noise=False,
        )
        if isinstance(crash, Crash) and crash.whea_fatal is not None:
            self._whea_cpu(boot + timedelta(seconds=5.2), crash.whea_fatal, fatal=True)
        if isinstance(crash, Crash):
            local = crashed_at.astimezone(self.tz)
            dump = rf"C:\WINDOWS\Minidump\{local:%m%d%y}-{self.rng.randint(5000, 19999)}-01.dmp"
            first = f"0x{code:08x} ({', '.join(_hex16(p) for p in params)})"
            report = self.guid()
            self._sys(
                self.jitter(boot + timedelta(seconds=70), 40),
                "Microsoft-Windows-WER-SystemErrorReporting",
                1001,
                L.ERROR,
                f"The computer has rebooted from a bugcheck. The bugcheck was: {first}. "
                f"A dump was saved in: {dump}. Report Id: {report}.",
                {"param1": first, "param2": dump, "param3": report},
                noise=False,
            )

    def clean_shutdown(self, t: datetime, by_update: bool) -> None:
        host = self.m.hostname
        if by_update:
            msg = (
                f"The process C:\\WINDOWS\\uus\\AMD64\\MoUsoCoreWorker.exe ({host}) has initiated "
                "the restart "
                f"of computer {host} on behalf of user NT AUTHORITY\\SYSTEM for the following "
                "reason: "
                "Operating System: Service pack (Planned) Reason Code: 0x80020010 Shutdown Type: "
                "restart Comment:"
            )
        else:
            msg = (
                f"The process C:\\WINDOWS\\system32\\shutdown.exe ({host}) has initiated the "
                "restart of computer "
                f"{host} on behalf of user {host}\\{self.m.user} for the following reason: Other "
                "(Unplanned) "
                "Reason Code: 0x0 Shutdown Type: restart Comment:"
            )
        self._sys(t - timedelta(seconds=21), "User32", 1074, L.INFORMATION, msg, noise=False)
        self._sys(
            t - timedelta(seconds=4),
            "Microsoft-Windows-Kernel-Power",
            109,
            L.INFORMATION,
            "The kernel power manager has initiated a shutdown transition.",
            {"ShutdownActionType": "5", "ShutdownEventCode": "0", "ShutdownReason": "0"},
            noise=False,
        )
        self._sys(
            t - timedelta(seconds=2),
            "EventLog",
            6006,
            L.INFORMATION,
            "The Event log service was stopped.",
            noise=False,
        )
        self._sys(
            t,
            "Microsoft-Windows-Kernel-General",
            13,
            L.INFORMATION,
            f"The operating system is shutting down at system time {_systime(t)}.",
            {"StopTime": _systime(t)},
            noise=False,
        )

    # ---- record helpers --------------------------------------------------

    def _sys(
        self,
        t: datetime,
        provider: str,
        eid: int,
        level: EventLevel,
        msg: str,
        data: dict[str, str] | None = None,
        *,
        noise: bool = True,
    ) -> None:
        self.add(t, "System", provider, eid, level, msg, data, noise=noise)

    def _app(
        self,
        t: datetime,
        provider: str,
        eid: int,
        level: EventLevel,
        msg: str,
        data: dict[str, str] | None = None,
        *,
        noise: bool = True,
    ) -> None:
        self.add(t, "Application", provider, eid, level, msg, data, noise=noise)

    def _scm_state(self, t: datetime, service: str, state: str, *, noise: bool) -> None:
        self._sys(
            t,
            "Service Control Manager",
            7036,
            L.INFORMATION,
            f"The {service} service entered the {state} state.",
            {"param1": service, "param2": state},
            noise=noise,
        )

    def _dcom(self, t: datetime, *, noise: bool) -> None:
        clsid, appid, who = self.rng.choice(_DCOM)
        if who == "user":
            domain, user, sid = self.m.hostname, self.m.user, self.user_sid
        else:
            domain, user, sid = "NT AUTHORITY", "LOCAL SERVICE", "S-1-5-19"
        self._sys(
            t,
            "Microsoft-Windows-DistributedCOM",
            10016,
            L.WARNING,
            "The application-specific permission settings do not grant Local Activation "
            "permission for "
            f"the COM Server application with CLSID {clsid} and APPID {appid} to the user "
            f"{domain}\\{user} "
            f"SID ({sid}) from address LocalHost (Using LRPC) running in the application container "
            "Unavailable SID (Unavailable). This security permission can be modified using the "
            "Component "
            "Services administrative tool.",
            {
                "param1": "application-specific",
                "param2": "Local",
                "param3": "Activation",
                "param4": clsid,
                "param5": appid,
                "param6": domain,
                "param7": user,
                "param8": sid,
                "param9": "LocalHost (Using LRPC)",
                "param10": "Unavailable",
                "param11": "Unavailable",
            },
            noise=noise,
        )

    def _spp(self, t: datetime, *, noise: bool) -> None:
        when = (t + timedelta(hours=self.rng.uniform(4, 30))).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._app(
            t,
            "Microsoft-Windows-Security-SPP",
            16384,
            L.INFORMATION,
            f"Successfully scheduled Software Protection service for re-start at {when}. Reason: "
            "RulesEngine.",
            {"param1": when, "param2": "RulesEngine"},
            noise=noise,
        )

    def _security_center(self, t: datetime, *, noise: bool) -> None:
        self._app(
            t,
            "SecurityCenter",
            15,
            L.INFORMATION,
            "Updated Windows Defender status successfully to SECURITY_PRODUCT_STATE_ON.",
            {"param1": "Windows Defender", "param2": "SECURITY_PRODUCT_STATE_ON"},
            noise=noise,
        )

    def _wu(self, t: datetime, title: str, *, noise: bool) -> None:
        data = {
            "updateTitle": title,
            "updateGuid": "{" + self.guid() + "}",
            "updateRevisionNumber": "200",
            "serviceGuid": "{9482F4B4-E343-43B6-B170-9A65BC822C77}",
        }
        self._sys(
            t,
            "Microsoft-Windows-WindowsUpdateClient",
            44,
            L.INFORMATION,
            "Windows Update started downloading an update.",
            data,
            noise=noise,
        )
        t43 = t + timedelta(seconds=self.rng.uniform(15, 40))
        self._sys(
            t43,
            "Microsoft-Windows-WindowsUpdateClient",
            43,
            L.INFORMATION,
            f"Installation Started: Windows has started installing the following update: {title}",
            data,
            noise=noise,
        )
        t19 = t43 + timedelta(seconds=self.rng.uniform(50, 160))
        self._sys(
            t19,
            "Microsoft-Windows-WindowsUpdateClient",
            19,
            L.INFORMATION,
            "Installation Successful: Windows successfully installed the following update: "
            f"{title}",
            data,
            noise=noise,
        )

    def _live_dump(self, t: datetime, code: int, *, noise: bool) -> None:
        p1 = f"{code:x}"
        local = t.astimezone(self.tz)
        folder = "USBHUB3" if code == 0x144 else "WATCHDOG"
        dump = rf"\\?\C:\WINDOWS\LiveKernelReports\{folder}\{folder}-{local:%Y%m%d-%H%M}.dmp"
        report = self.guid()
        tmp = f"{self.rng.getrandbits(32):08X}"
        archive = (
            rf"\\?\C:\ProgramData\Microsoft\Windows\WER\ReportArchive\Kernel_{p1}_"
            f"{self.rng.getrandbits(160):040x}_00000000_cab_{report.replace('-', '')}"
        )
        p2 = f"{0xFFFF800000000000 | self.rng.getrandbits(40) << 4:x}"
        p3 = f"{0xFFFFF80000000000 | self.rng.getrandbits(36) << 4:x}"
        p5 = f"{self.rng.randint(0x1000, 0x4FFF):x}"
        build = "_".join(["10", "0", self.m.os_build.split(".")[0]])
        wer_temp = rf"\\?\C:\ProgramData\Microsoft\Windows\WER\Temp\WER.{tmp}.tmp"
        attached = f"{dump} {wer_temp}.WERInternalMetadata.xml {wer_temp}.xml"
        params = {
            "P1": p1,
            "P2": p2,
            "P3": p3,
            "P4": "0",
            "P5": p5,
            "P6": build,
            "P7": "0_0",
            "P8": "256_1",
            "P9": "",
            "P10": "",
        }
        sig = " ".join(f"{k}: {v}" if v else f"{k}:" for k, v in params.items())
        msg = (
            f"Fault bucket , type 0 Event Name: LiveKernelEvent Response: Not available Cab Id: 0 "
            f"Problem signature: {sig} Attached files: {attached} These files may be available "
            f"here: {archive} "
            f"Analysis symbol: Rechecking for solution: 0 Report Id: {report} Report Status: "
            "268435456 "
            "Hashed bucket: Cab Guid: 0"
        )
        data = {
            "Bucket": "",
            "BucketType": "0",
            "EventName": "LiveKernelEvent",
            "Response": "Not available",
            "CabId": "0",
            **params,
            "AttachedFiles": attached,
            "StorePath": archive,
            "AnalysisSymbol": "",
            "Rechecking": "0",
            "ReportId": report,
            "ReportStatus": "268435456",
            "HashedBucket": "",
            "CabGuid": "0",
        }
        self._app(t, "Windows Error Reporting", 1001, L.INFORMATION, msg, data, noise=noise)

    def _wer_app(
        self, t: datetime, event: str, bucket_type: int, sig: list[str], report: str, *, noise: bool
    ) -> None:
        bucket = str(self.rng.getrandbits(62))
        hashed = f"{self.rng.getrandbits(128):032x}"
        params = {f"P{i + 1}": (sig[i] if i < len(sig) else "") for i in range(10)}
        text = " ".join(f"{k}: {v}" if v else f"{k}:" for k, v in params.items())
        digest, salt = self.rng.getrandbits(160), self.rng.getrandbits(32)
        store = (
            rf"\\?\C:\ProgramData\Microsoft\Windows\WER\ReportArchive\{event}_{sig[0]}_"
            f"{digest:040x}_{salt:08x}_{report.replace('-', '')}"
        )
        msg = (
            f"Fault bucket {bucket}, type {bucket_type} Event Name: {event} Response: Not "
            "available Cab Id: 0 "
            f"Problem signature: {text} Attached files: These files may be available here: {store} "
            f"Analysis symbol: Rechecking for solution: 0 Report Id: {report} Report Status: "
            "268435456 "
            f"Hashed bucket: {hashed} Cab Guid: 0"
        )
        data = {
            "Bucket": bucket,
            "BucketType": str(bucket_type),
            "EventName": event,
            "Response": "Not available",
            "CabId": "0",
            **params,
            "StorePath": store,
            "ReportId": report,
            "ReportStatus": "268435456",
            "HashedBucket": hashed,
            "CabGuid": "0",
        }
        self._app(t, "Windows Error Reporting", 1001, L.INFORMATION, msg, data, noise=noise)

    def _app_crash(self, t: datetime, c: _NoiseCrash, *, noise: bool) -> None:
        pid = self.rng.randint(0x400, 0x9FFF)
        start = 0x1DB0000_00000000 + self.rng.getrandbits(48)
        report = self.guid()
        self._app(
            t,
            "Application Error",
            1000,
            L.ERROR,
            f"Faulting application name: {c.exe}, version: {c.version}, time stamp: "
            f"0x{c.timestamp} "
            f"Faulting module name: {c.module}, version: {c.module_version}, time stamp: "
            f"0x{c.module_timestamp} "
            f"Exception code: {c.exception} Fault offset: {c.offset} Faulting process id: "
            f"0x{pid:X} "
            f"Faulting application start time: 0x{start:X} Faulting application path: {c.path} "
            f"Faulting module path: {c.module_path} Report Id: {report} "
            f"Faulting package full name: {c.package} Faulting package-relative application ID: "
            f"{c.app_id}",
            {
                "AppName": c.exe,
                "AppVersion": c.version,
                "AppTimeStamp": c.timestamp,
                "ModuleName": c.module,
                "ModuleVersion": c.module_version,
                "ModuleTimeStamp": c.module_timestamp,
                "ExceptionCode": c.exception.removeprefix("0x"),
                "FaultingOffset": c.offset.removeprefix("0x"),
                "ProcessId": f"0x{pid:x}",
                "ProcessCreationTime": f"0x{start:x}",
                "AppPath": c.path,
                "ModulePath": c.module_path,
                "IntegratorReportId": report,
                "PackageFullName": c.package,
                "PackageRelativeAppId": c.app_id,
            },
            noise=noise,
        )
        sig = [
            c.exe,
            c.version,
            c.timestamp,
            c.module,
            c.module_version,
            c.module_timestamp,
            c.exception.removeprefix("0x"),
            c.offset.removeprefix("0x"),
        ]
        self._wer_app(
            self.jitter(t + timedelta(seconds=1), 4), "APPCRASH", 4, sig, report, noise=noise
        )

    def _whea_cpu(self, t: datetime, err: CpuError, *, fatal: bool) -> None:
        if fatal:
            msg = (
                "A fatal hardware error has occurred. Reported by component: Processor Core Error "
                "Source: "
                f"Machine Check Exception Error Type: {err.error_type} Processor APIC ID: "
                f"{err.apic_id} "
                "The details view of this entry contains further information."
            )
            eid, level, source = 18, L.ERROR, "3"
        else:
            msg = (
                "A corrected hardware error has occurred. Reported by component: Processor Core "
                "Error Source: "
                f"Corrected Machine Check Error Type: {err.error_type} Processor APIC ID: "
                f"{err.apic_id} "
                "The details view of this entry contains further information."
            )
            eid, level, source = 19, L.WARNING, "1"
        self._sys(
            t,
            "Microsoft-Windows-WHEA-Logger",
            eid,
            level,
            msg,
            {
                "ErrorSource": source,
                "ApicId": str(err.apic_id),
                "MCABank": str(err.mca_bank),
                "MciStat": _hex16(err.mci_status),
                "MciAddr": _hex16(0),
                "MciMisc": _hex16(0),
            },
            noise=False,
        )

    def _whea_pcie(self, t: datetime, sig: WheaPcie, *, noise: bool) -> None:
        bus, dev, fn = sig.bdf.split(":")
        self._sys(
            t,
            "Microsoft-Windows-WHEA-Logger",
            17,
            L.WARNING,
            "A corrected hardware error has occurred. Component: PCI Express Root Port Error "
            "Source: "
            f"Advanced Error Reporting (PCI Express) Primary Bus:Device:Function: {sig.bdf} "
            f"Secondary Bus:Device:Function: 0x0:0x0:0x0 Primary Device Name:{sig.device} "
            "Secondary Device Name:",
            {
                "ErrorSource": "4",
                "PortType": "4",
                "Bus": bus,
                "Device": dev,
                "Function": fn,
                "VendorID": sig.vendor_id,
                "DeviceID": sig.device_id,
                "PrimaryDeviceName": sig.device,
            },
            noise=noise,
        )

    # ---- signals ---------------------------------------------------------

    def signal(self, sig: Signal) -> None:
        t = self.jitter(self.at(sig.at), 0.9)
        m = self.m
        if isinstance(sig, Tdr):
            gpu = m.gpus[sig.gpu]
            self._sys(
                t,
                "Display",
                4101,
                L.WARNING,
                f"Display driver {gpu.service} stopped responding and has successfully recovered.",
                {"param1": gpu.service, "param2": ""},
                noise=False,
            )
            if gpu.service == "nvlddmkm":
                self._sys(
                    t - timedelta(seconds=1.2),
                    "nvlddmkm",
                    153,
                    L.ERROR,
                    "The description for Event ID 153 from source nvlddmkm cannot be found. "
                    "Either the component "
                    "that raises this event is not installed on your local computer or the "
                    "installation is "
                    "corrupted. You can install or repair the component on the local computer. If "
                    "the event "
                    "originated on another computer, the display information had to be saved with "
                    "the event. "
                    "The following information was included with the event: \\Device\\Video3 "
                    "Resetting TDR "
                    "occurred on GPUID:100 The message resource is present but the message was "
                    "not found in "
                    "the message table",
                    {"param1": "\\Device\\Video3", "param2": "Resetting TDR occurred on GPUID:100"},
                    noise=False,
                )
            if sig.live_dump is not None:
                self._live_dump(
                    t + timedelta(seconds=sig.live_dump_delay_s), sig.live_dump, noise=False
                )
        elif isinstance(sig, LiveDump):
            self._live_dump(t, sig.code, noise=False)
        elif isinstance(sig, WheaCorrectedCpu):
            for i in range(sig.count):
                offset = sig.spread_min * (i / max(sig.count - 1, 1))
                self._whea_cpu(self.jitter(self.at(sig.at + offset), 30), sig.error, fatal=False)
        elif isinstance(sig, WheaPcie):
            for _ in range(sig.count):
                self._whea_pcie(self.jitter(self.at(sig.at), sig.spread_min * 60), sig, noise=False)
        elif isinstance(sig, AppHang):
            self._app_hang(t, sig.app)
        elif isinstance(sig, AppCrash):
            crash = _NoiseCrash(
                sig.app.exe,
                sig.app.version,
                sig.app.timestamp,
                sig.module,
                sig.module_version,
                sig.module_timestamp,
                sig.exception,
                sig.offset,
                sig.app.path,
                sig.module_path,
                "",
                "",
            )
            self._app_crash(t, crash, noise=False)
        elif isinstance(sig, LowMemory):
            parts = [f"{name} ({pid}) consumed {size} bytes" for name, pid, size in sig.consumers]
            listing = ", ".join(parts[:-1]) + f", and {parts[-1]}" if len(parts) > 1 else parts[0]
            self._sys(
                t,
                "Microsoft-Windows-Resource-Exhaustion-Detector",
                2004,
                L.WARNING,
                "Windows successfully diagnosed a low virtual memory condition. The following "
                "programs "
                f"consumed the most virtual memory: {listing}.",
                noise=False,
            )
        elif isinstance(sig, StorageReset):
            disk = m.disks[sig.disk]
            port = f"\\Device\\RaidPort{disk.raid_port}"
            self._sys(
                t,
                disk.port_driver,
                129,
                L.WARNING,
                f"Reset to device, {port}, was issued.",
                {"param1": port},
                noise=False,
            )
        elif isinstance(sig, DiskRetry):
            disk = m.disks[sig.disk]
            for _ in range(sig.count):
                lba = self.rng.randint(0x100000, 0x7FFFFFFF)
                self._sys(
                    self.jitter(t, sig.spread_min * 60),
                    "disk",
                    153,
                    L.WARNING,
                    f"The IO operation at logical block address 0x{lba:x} for Disk "
                    f"{self._disk_no(sig.disk)} "
                    f"(PDO name: {disk.pdo}) was retried.",
                    noise=False,
                )
        elif isinstance(sig, BadBlock):
            n = self._disk_no(sig.disk)
            for _ in range(sig.count):
                self._sys(
                    self.jitter(t, sig.spread_min * 60),
                    "disk",
                    7,
                    L.ERROR,
                    f"The device, \\Device\\Harddisk{n}\\DR{n}, has a bad block.",
                    noise=False,
                )
        elif isinstance(sig, PagingError):
            n = self._disk_no(sig.disk)
            self._sys(
                t,
                "disk",
                51,
                L.WARNING,
                f"An error was detected on device \\Device\\Harddisk{n}\\DR{n} during a paging "
                "operation.",
                noise=False,
            )
        elif isinstance(sig, ServiceInstall):
            self._sys(
                t,
                "Service Control Manager",
                7045,
                L.INFORMATION,
                f"A service was installed in the system. Service Name: {sig.name} Service File "
                "Name: "
                f"{sig.image_path} Service Type: kernel mode driver Service Start Type: "
                f"{sig.start_type} "
                "Service Account:",
                {
                    "ServiceName": sig.name,
                    "ImagePath": sig.image_path,
                    "ServiceType": "kernel mode driver",
                    "StartType": sig.start_type,
                    "AccountName": "",
                },
                noise=False,
            )
        elif isinstance(sig, UpdateInstall):
            self._wu(t, sig.title, noise=False)
        elif isinstance(sig, DriverInstall):
            folder = f"{sig.inf}_amd64_{self.rng.getrandbits(64):016x}"
            self._sys(
                t,
                "Microsoft-Windows-UserPnp",
                20001,
                L.INFORMATION,
                f"Driver Management concluded the process to install driver {folder}\\{sig.inf} "
                "for Device "
                f"Instance ID {sig.device_instance} with the following status: 0x0.",
                {
                    "DriverName": f"{folder}\\{sig.inf}",
                    "DeviceInstanceID": sig.device_instance,
                    "InstallStatus": "0x0",
                },
                noise=False,
            )
        elif isinstance(sig, Noise):
            self.noise_event(sig.kind, t, noise=True)
        elif isinstance(sig, Crash | PowerCut | Restart | Sleep | LogCleared):
            pass  # handled by the session timeline
        else:  # pragma: no cover - exhaustive over Signal
            raise TypeError(f"unhandled signal {sig!r}")

    def _disk_no(self, index: int) -> int:
        return int(self.m.disks[index].name.split()[0])

    def _app_hang(self, t: datetime, app: App) -> None:
        pid = self.rng.randint(0x400, 0x9FFF)
        start = 0x1DB0000_00000000 + self.rng.getrandbits(48)
        report = self.guid()
        hang_type = "Unknown" if app.exe == "explorer.exe" else "Quiesce"
        self._app(
            t,
            "Application Hang",
            1002,
            L.ERROR,
            f"The program {app.exe} version {app.version} stopped interacting with Windows and "
            "was closed. "
            "To see if more information about the problem is available, check the problem history "
            "in the "
            f"Security and Maintenance control panel. Process ID: {pid:x} Start Time: {start:016x} "
            f"Termination Time: 4294967295 Application Path: {app.path} Report Id: {report} "
            "Faulting package full name: Faulting package-relative application ID: Hang type: "
            f"{hang_type}",
            {
                "AppName": app.exe,
                "AppVersion": app.version,
                "ProcessID": f"{pid:x}",
                "StartTime": f"{start:016x}",
                "TerminationTime": "4294967295",
                "ExeFileName": app.path,
                "ReportID": report,
                "FaultingPackageFullName": "",
                "FaultingPackageRelativeAppId": "",
                "HangType": hang_type,
            },
            noise=False,
        )
        sig = [app.exe, app.version, app.timestamp, f"{self.rng.getrandbits(16):04x}", "134217728"]
        self._wer_app(
            self.jitter(t + timedelta(seconds=2), 5), "AppHangB1", 5, sig, report, noise=False
        )


# ---- noise renderers --------------------------------------------------------


def _n_scm_pair(b: _Builder, t: datetime, noise: bool) -> None:
    svc = b.rng.choice(_SERVICES)
    b._scm_state(t, svc, "running", noise=noise)
    b._scm_state(t + timedelta(seconds=b.rng.uniform(30, 600)), svc, "stopped", noise=noise)


def _n_scm_7040(b: _Builder, t: datetime, noise: bool) -> None:
    old, new = b.rng.choice((("auto start", "demand start"), ("demand start", "auto start")))
    b._sys(
        t,
        "Service Control Manager",
        7040,
        L.INFORMATION,
        "The start type of the Background Intelligent Transfer Service service was changed from "
        f"{old} to {new}.",
        {
            "param1": "Background Intelligent Transfer Service",
            "param2": old,
            "param3": new,
            "param4": "BITS",
        },
        noise=noise,
    )


def _n_dcom(b: _Builder, t: datetime, noise: bool) -> None:
    b._dcom(t, noise=noise)


def _n_kgeneral16(b: _Builder, t: datetime, noise: bool) -> None:
    hive = b.rng.choice(_HIVES).format(u=b.m.user)
    keys, pages = b.rng.randint(1, 1800), b.rng.randint(1, 120)
    b._sys(
        t,
        "Microsoft-Windows-Kernel-General",
        16,
        L.INFORMATION,
        f"The access history in hive {hive} was cleared updating {keys} keys and creating {pages} "
        "modified pages.",
        {
            "HiveNameLength": str(len(hive)),
            "HiveName": hive,
            "KeysUpdated": str(keys),
            "DirtyPages": str(pages),
        },
        noise=noise,
    )


def _n_kgeneral1(b: _Builder, t: datetime, noise: bool) -> None:
    old = t - timedelta(milliseconds=b.rng.uniform(1, 900))
    b._sys(
        t,
        "Microsoft-Windows-Kernel-General",
        1,
        L.INFORMATION,
        f"The system time has changed to {_systime(t)} from {_systime(old)}. "
        "Change Reason: An application or system component changed the time.",
        {"NewTime": _systime(t), "OldTime": _systime(old), "Reason": "1"},
        noise=noise,
    )


def _n_hdr(b: _Builder, t: datetime, noise: bool) -> None:
    on = b.rng.random() < 0.5
    eid, state = (4123, "on") if on else (4127, "off")
    b._sys(
        t,
        "Display",
        eid,
        L.INFORMATION,
        f"Advanced color (HDR) was turned {state} for display \\\\.\\DISPLAY1.",
        {"param1": "\\\\.\\DISPLAY1"},
        noise=noise,
    )


def _n_defender_update(b: _Builder, t: datetime, noise: bool) -> None:
    b.defender_version += b.rng.randint(1, 3)
    version = f"1.419.{318 + b.defender_version}.0"
    title = (
        "Security Intelligence Update for Microsoft Defender Antivirus - KB2267602 (Version "
        f"{version}) "
        "- Current Channel (Broad)"
    )
    b._wu(t, title, noise=noise)


def _n_spp(b: _Builder, t: datetime, noise: bool) -> None:
    b._spp(t, noise=noise)


def _n_restart_manager(b: _Builder, t: datetime, noise: bool) -> None:
    session = b.rng.randint(0, 3)
    b._app(
        t,
        "Microsoft-Windows-RestartManager",
        10000,
        L.INFORMATION,
        f"Starting session {session} - {_systime(t)}.",
        {"RmSessionId": str(session), "UTCStartTime": _systime(t)},
        noise=noise,
    )
    b._app(
        t + timedelta(seconds=b.rng.uniform(4, 70)),
        "Microsoft-Windows-RestartManager",
        10001,
        L.INFORMATION,
        f"Ending session {session} started {_systime(t)}.",
        {"RmSessionId": str(session), "UTCStartTime": _systime(t)},
        noise=noise,
    )


def _n_security_center(b: _Builder, t: datetime, noise: bool) -> None:
    b._security_center(t, noise=noise)


def _n_vss(b: _Builder, t: datetime, noise: bool) -> None:
    b._app(
        t,
        "VSS",
        8224,
        L.INFORMATION,
        "The VSS service is shutting down due to idle timeout.",
        noise=noise,
    )


def _n_app_crash(b: _Builder, t: datetime, noise: bool) -> None:
    b._app_crash(t, b.rng.choice(_noise_crashes(b.m)), noise=noise)


def _n_live_dump_1b0(b: _Builder, t: datetime, noise: bool) -> None:
    b._live_dump(t, 0x1B0, noise=noise)


def _n_whea17(b: _Builder, t: datetime, noise: bool) -> None:
    b._whea_pcie(t, WheaPcie(0, 1, 0), noise=noise)


_NOISE_RENDERERS: dict[str, Callable[[_Builder, datetime, bool], None]] = {
    "scm_pair": _n_scm_pair,
    "scm_7040": _n_scm_7040,
    "dcom": _n_dcom,
    "kgeneral16": _n_kgeneral16,
    "kgeneral1": _n_kgeneral1,
    "hdr": _n_hdr,
    "defender_update": _n_defender_update,
    "spp": _n_spp,
    "restart_manager": _n_restart_manager,
    "security_center": _n_security_center,
    "vss": _n_vss,
    "app_crash": _n_app_crash,
    "live_dump_1b0": _n_live_dump_1b0,
    "whea17": _n_whea17,
}


# --------------------------------------------------------------------------- #
# Sessions, telemetry and assembly
# --------------------------------------------------------------------------- #


def _timeline(b: _Builder) -> None:
    """Lay out boots, sleeps and shutdowns; fill ``b.intervals`` with awake periods."""
    spec = b.spec
    breaks = sorted(
        (s for s in spec.signals if isinstance(s, Crash | PowerCut | Restart | Sleep)),
        key=lambda s: s.at,
    )
    on_start = b.incident - timedelta(hours=spec.boot_before_h)
    b.boots.append(on_start)
    pending: list[tuple[datetime, bool, Crash | PowerCut | None, datetime | None]] = [
        (on_start, True, None, None)
    ]
    for sig in breaks:
        t = b.jitter(b.at(sig.at), 0.9)
        b.intervals.append((on_start, t))
        if isinstance(sig, Sleep):
            b._sys(
                t,
                "Microsoft-Windows-Kernel-Power",
                42,
                L.INFORMATION,
                "The system is entering sleep. Sleep Reason: Button or Lid",
                noise=False,
            )
            wake = b.at(sig.wake_at)
            if sig.resumed:
                b._sys(
                    wake + timedelta(seconds=1),
                    "Microsoft-Windows-Kernel-Power",
                    107,
                    L.INFORMATION,
                    "The system has resumed from sleep.",
                    noise=False,
                )
                b._sys(
                    wake + timedelta(seconds=6),
                    "Microsoft-Windows-Power-Troubleshooter",
                    1,
                    L.INFORMATION,
                    f"The system has returned from a low power state. Sleep Time: {_systime(t)} "
                    f"Wake Time: {_systime(wake)} Wake Source: Power Button",
                    noise=False,
                )
            on_start = wake
            continue
        if isinstance(sig, Restart):
            b.clean_shutdown(t, sig.by_update)
            boot = t + timedelta(seconds=75)
            pending.append((boot, True, None, None))
        else:
            boot = t + timedelta(seconds=sig.reboot_s)
            pending.append((boot, False, sig, t))
        b.boots.append(boot)
        on_start = boot
    b.intervals.append((on_start, b.now))
    for boot, clean, crash, crashed_at in pending:
        b.boot(boot, clean, crash, crashed_at)


def _interp(r: tuple[float, float], frac: float) -> float:
    return r[0] + (r[1] - r[0]) * frac


def _readings(b: _Builder, values: dict[str, float]) -> list[SensorReading]:
    gpu = b.m.gpus[0]
    cpu_sensor = "Core (Tctl/Tdie)" if b.m.cpu_vendor == "amd" else "CPU Package"
    out = [
        SensorReading(
            hardware=gpu.name,
            sensor="GPU Core",
            kind="temperature",
            value=round(values["gpu_temp"], 1),
            unit="°C",
            source=SENSOR_SOURCE,
        )
    ]
    if gpu.hotspot_sensor:
        out.append(
            SensorReading(
                hardware=gpu.name,
                sensor="GPU Hot Spot",
                kind="temperature",
                value=round(values["gpu_temp"] + values["hotspot_delta"], 1),
                unit="°C",
                source=SENSOR_SOURCE,
            )
        )
    out += [
        SensorReading(
            hardware=gpu.name,
            sensor="GPU Core",
            kind="clock",
            value=round(values["gpu_clock"]),
            unit="MHz",
            source=SENSOR_SOURCE,
        ),
        SensorReading(
            hardware=gpu.name,
            sensor="GPU Core",
            kind="load",
            value=round(values["gpu_load"], 1),
            unit="%",
            source=SENSOR_SOURCE,
        ),
        SensorReading(
            hardware=gpu.name,
            sensor="GPU Package",
            kind="power",
            value=round(values["gpu_power"], 1),
            unit="W",
            source=SENSOR_SOURCE,
        ),
        SensorReading(
            hardware=gpu.name,
            sensor="GPU Fan",
            kind="fan",
            value=round(values["gpu_fan"]),
            unit="RPM",
            source=SENSOR_SOURCE,
        ),
        SensorReading(
            hardware=b.m.cpu,
            sensor=cpu_sensor,
            kind="temperature",
            value=round(values["cpu_temp"], 1),
            unit="°C",
            source=SENSOR_SOURCE,
        ),
        SensorReading(
            hardware=b.m.cpu,
            sensor="CPU Total",
            kind="load",
            value=round(values["cpu_load"], 1),
            unit="%",
            source=SENSOR_SOURCE,
        ),
    ]
    return out


def _series(b: _Builder, s: SensorSeries) -> list[SensorSnapshot]:
    snaps: list[SensorSnapshot] = []
    span = (s.end - s.start) * 60
    steps = max(1, int(span // s.interval_s))
    noise = {
        "gpu_temp": 0.6,
        "hotspot_delta": 0.4,
        "gpu_clock": 12,
        "gpu_load": 0.8,
        "gpu_power": 4.0,
        "gpu_fan": 25,
        "cpu_temp": 1.0,
        "cpu_load": 4.0,
    }
    for i in range(steps + 1):
        frac = (i / steps) ** s.curve
        values = {
            key: max(
                0.0,
                _interp(getattr(s, key), frac)
                + (b.rng.gauss(0, sd) if getattr(s, key)[1] else 0.0),
            )
            for key, sd in noise.items()
        }
        values["gpu_load"] = min(values["gpu_load"], 100.0)
        values["cpu_load"] = min(values["cpu_load"], 100.0)
        taken = b.at(s.start) + timedelta(seconds=i * s.interval_s)
        snaps.append(SensorSnapshot(taken_at=taken, readings=_readings(b, values)))
    return snaps


def _sensors(b: _Builder) -> list[SensorSnapshot]:
    if not b.m.sensor_backend:
        return [
            SensorSnapshot(
                taken_at=b.now,
                readings=[],
                unavailable=[f"{SENSOR_SOURCE}: web server not reachable at http://localhost:8085"],
            )
        ]
    snaps = [snap for s in b.spec.sensors for snap in _series(b, s)]
    last_end = max((s.end for s in b.spec.sensors), default=None)
    if last_end is None or last_end < b.spec.now_after_min - 10:
        idle_power = (4.0, 9.0) if b.m.laptop else (14.0, 32.0)
        for k in (2, 1, 0):
            values = {
                "gpu_temp": b.rng.uniform(38, 46),
                "hotspot_delta": b.rng.uniform(7, 10),
                "gpu_clock": b.rng.uniform(210, 350),
                "gpu_load": b.rng.uniform(1, 6),
                "gpu_power": b.rng.uniform(*idle_power),
                "gpu_fan": 0.0 if not b.m.laptop else 2100.0,
                "cpu_temp": b.rng.uniform(40, 48),
                "cpu_load": b.rng.uniform(3, 9),
            }
            snaps.append(
                SensorSnapshot(
                    taken_at=b.now - timedelta(seconds=30 * k), readings=_readings(b, values)
                )
            )
    return sorted(snaps, key=lambda s: s.taken_at)


_BACKGROUND_PROCS = (
    ("explorer.exe", 0.18, 0.11),
    ("dwm.exe", 0.12, 0.25),
    ("MsMpEng.exe", 0.22, 0.31),
    ("svchost.exe", 0.06, 0.04),
    ("SearchHost.exe", 0.15, 0.09),
)
_USER_PROCS = (
    ("chrome.exe", 0.8, 1.3),
    ("Discord.exe", 0.35, 0.7),
    ("steamwebhelper.exe", 0.25, 0.45),
    ("Spotify.exe", 0.2, 0.3),
    ("OneDrive.exe", 0.08, 0.06),
)


def _process(b: _Builder) -> ProcessSnapshot:
    spec, m, rng = b.spec, b.m, b.rng
    total = m.ram_gb * GIB
    limit = int((m.ram_gb + m.pagefile_gb) * GIB)
    if spec.process:
        commit, available = (
            int(spec.process.commit_used_gb * GIB),
            int(spec.process.available_gb * GIB),
        )
    else:
        commit, available = (
            int(limit * rng.uniform(0.38, 0.55)),
            int(total * rng.uniform(0.45, 0.65)),
        )
    last_boot = max(t for t in b.boots if t <= b.now)
    uptime_s = (b.now - last_boot).total_seconds()

    def started() -> datetime:
        return last_boot + timedelta(seconds=rng.uniform(20, max(30.0, uptime_s * 0.9)))

    procs: list[ProcessInfo] = []
    pid_pool = rng.sample(range(1000, 30000, 4), 16)
    named = list(spec.process.top) if spec.process else []
    for i, p in enumerate(named):
        procs.append(
            ProcessInfo(
                pid=pid_pool[i],
                name=p.name,
                working_set_bytes=int(p.working_set_gb * GIB),
                private_bytes=int(p.private_gb * GIB),
                page_faults=p.page_faults,
                read_bytes=int(p.read_mb * MIB),
                write_bytes=int(p.write_mb * MIB),
                cpu_seconds=p.cpu_s,
                started=started(),
            )
        )
    extra = list(_BACKGROUND_PROCS) + ([] if named else rng.sample(_USER_PROCS, 3))
    have = {p.name for p in procs}
    for j, (name, ws, priv) in enumerate(extra, start=len(procs)):
        if name in have:
            continue
        scale = rng.uniform(0.8, 1.3)
        procs.append(
            ProcessInfo(
                pid=pid_pool[j],
                name=name,
                working_set_bytes=int(ws * scale * GIB),
                private_bytes=int(priv * scale * GIB),
                page_faults=rng.randint(150_000, 4_500_000),
                read_bytes=int(rng.uniform(20, 900) * MIB),
                write_bytes=int(rng.uniform(5, 400) * MIB),
                cpu_seconds=round(rng.uniform(3, 400), 1),
                started=started(),
            )
        )
    procs.sort(key=lambda p: p.private_bytes, reverse=True)
    loads = {d.disk: d for d in spec.process.disks} if spec.process else {}
    disks = []
    for i, d in enumerate(m.disks):
        if i in loads:
            ld = loads[i]
            disks.append(
                DiskState(
                    name=d.name,
                    queue_length=ld.queue_length,
                    percent_busy=ld.percent_busy,
                    read_bytes_per_sec=ld.read_mb_s * MIB,
                    write_bytes_per_sec=ld.write_mb_s * MIB,
                )
            )
        else:
            disks.append(
                DiskState(
                    name=d.name,
                    queue_length=round(rng.uniform(0.0, 0.2), 2),
                    percent_busy=round(rng.uniform(0.5, 6.0), 1),
                    read_bytes_per_sec=round(rng.uniform(0.1, 3.0) * MIB),
                    write_bytes_per_sec=round(rng.uniform(0.2, 4.0) * MIB),
                )
            )
    return ProcessSnapshot(
        taken_at=b.now,
        memory=MemoryState(
            total_bytes=total,
            available_bytes=available,
            commit_used_bytes=commit,
            commit_limit_bytes=limit,
        ),
        disks=disks,
        top_processes=procs[:10],
    )


def _drivers(spec: CaseSpec) -> list[DriverInfo]:
    by_name = {d.device_name: d for d in spec.machine.drivers}
    for d in spec.extra_drivers:
        by_name[d.device_name] = d
    return list(by_name.values())


def _dxdiag(spec: CaseSpec) -> DxDiagSummary:
    return DxDiagSummary(
        directx_version="DirectX 12",
        display_devices=[
            DisplayDevice(
                name=g.name,
                manufacturer=g.manufacturer,
                driver_version=g.driver_version,
                driver_date=g.driver_date,
                dedicated_memory_mb=g.vram_mb,
                feature_levels=list(g.feature_levels),
            )
            for g in spec.machine.gpus
        ],
        problems=list(spec.dxdiag_problems),
    )


def _frametimes(b: _Builder, f: Frametimes) -> list[FrameTimeSample]:
    rng = b.rng
    out: list[FrameTimeSample] = []
    t = 0.0
    end = f.duration_s * 1000
    event = f.event_s * 1000
    recovering = 0
    spiked = False
    next_spike = rng.uniform(500, 1200)
    while t < end:
        base = f.base_ms
        busy_ratio = 0.82
        if f.profile == "thermal":
            base = f.base_ms * (1 + 0.7 * t / end)
            busy_ratio = 0.97
        ft = max(1.0, rng.gauss(base, base * 0.08))
        if f.profile == "tdr" and not spiked and t >= event:
            ft, spiked, recovering = rng.uniform(2050, 2400), True, 40
        elif f.profile == "tdr" and recovering:
            ft = base * (1 + 2.0 * recovering / 40) + rng.uniform(0, 3)
            recovering -= 1
        elif f.profile == "hitch" and not spiked and t >= event:
            ft, spiked = rng.uniform(80, 92), True
        elif f.profile == "thermal" and t >= next_spike:
            ft = rng.uniform(35, 60)
            next_spike = t + rng.uniform(500, 1400)
        elif f.profile == "hang" and t >= event:
            break
        out.append(
            FrameTimeSample(
                time_offset_ms=round(t, 2),
                frame_time_ms=round(ft, 2),
                gpu_busy_ms=round(ft * busy_ratio * rng.uniform(0.95, 1.0), 2),
            )
        )
        t += ft
    return out


def _description(b: _Builder, window_start: datetime) -> str:
    m = b.m
    form = "Laptop" if m.laptop else "Desktop"
    gpus = " + ".join(g.name for g in m.gpus)
    text = (
        f"Synthetic replay {b.spec.id}: {form} ({m.cpu}; {gpus}; {m.ram_gb} GB RAM), "
        f"{m.os_caption} "
        f"build {m.os_build}. Event window {window_start:%Y-%m-%dT%H:%M:%SZ} to "
        f"{b.now:%Y-%m-%dT%H:%M:%SZ}."
    )
    if b.spec.frametimes:
        start = b.at(b.spec.frametimes.start)
        text += f" Frame-time capture (frametimes.json) starts at {start:%Y-%m-%dT%H:%M:%S.%f}Z."
    return text


def _incident_utc(spec: CaseSpec) -> tuple[datetime, timezone]:
    tz = timezone(timedelta(minutes=spec.utc_offset_minutes))
    local = datetime.strptime(spec.incident_local, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
    return local.astimezone(UTC), tz


def build(spec: CaseSpec, split: str = "dev") -> Bundle:
    """Render one spec into validated contract objects (no I/O)."""
    incident, tz = _incident_utc(spec)
    now = incident + timedelta(minutes=spec.now_after_min)
    b = _Builder(spec=spec, rng=_rng_for(spec.id), incident=incident, now=now, tz=tz)
    _timeline(b)
    b.background()
    for sig in spec.signals:
        b.signal(sig)

    raws = [r for r in b.raws if r.time <= now]
    for sig in spec.signals:
        if isinstance(sig, LogCleared):
            cleared = b.at(sig.at)
            raws = [r for r in raws if r.log != "System" or r.time >= cleared]
            raws.append(
                _Raw(
                    cleared,
                    "System",
                    "Microsoft-Windows-Eventlog",
                    104,
                    L.INFORMATION,
                    "The System log file was cleared.",
                    {
                        "SubjectUserName": spec.machine.user,
                        "SubjectDomainName": spec.machine.hostname,
                        "Channel": "System",
                        "BackupPath": "",
                    },
                    False,
                    len(b.raws),
                )
            )
    if any(not r.noise and r.time > now for r in b.raws):
        raise ValueError(f"{spec.id}: a signal event falls after 'now'")
    raws.sort(key=lambda r: (r.time, r.seq))

    counters = {
        "System": b.rng.randint(18_000, 90_000),
        "Application": b.rng.randint(9_000, 60_000),
    }
    events = []
    for r in raws:
        counters[r.log] += 1
        events.append(
            EventRecord(
                record_id=counters[r.log],
                time=r.time,
                log=r.log,
                provider=r.provider,
                event_id=r.event_id,
                level=r.level,
                message=r.message,
                data=r.data,
            )
        )

    window_start = b.incident - timedelta(hours=spec.boot_before_h)
    scenario = ReplayScenario(
        id=spec.id,
        description=_description(b, window_start),
        now=now,
        system=SystemInfo(
            os_caption=spec.machine.os_caption,
            os_build=spec.machine.os_build,
            gpus=[
                GpuInfo(
                    name=g.name,
                    vendor=g.vendor,
                    driver_version=g.driver_version,
                    driver_date=g.driver_date,
                )
                for g in spec.machine.gpus
            ],
            utc_offset_minutes=spec.utc_offset_minutes,
        ),
    )
    acceptable = [spec.primary, *spec.also_acceptable]
    tags = [*spec.tags, f"vendor:{spec.machine.gpus[0].vendor.value}"]
    if len(acceptable) > 1:
        tags.append("ambiguous")
    case = EvalCase(
        id=spec.id,
        split=split,
        question=spec.question,
        scenario=f"evals/scenarios/{spec.id}",
        expected=Expected(primary=spec.primary, acceptable=acceptable),
        tags=tags,
        source="synthetic",
    )
    return Bundle(
        case=case,
        scenario=scenario,
        incident=incident,
        events=events,
        noise_count=sum(r.noise for r in raws),
        sensors=_sensors(b),
        process=_process(b),
        drivers=_drivers(spec),
        dxdiag=_dxdiag(spec),
        frametimes=_frametimes(b, spec.frametimes) if spec.frametimes else None,
    )


def assign_splits(specs: tuple[CaseSpec, ...]) -> dict[str, str]:
    """About a third of each primary category goes to ``test`` (at least one when n >= 2)."""
    splits: dict[str, str] = {}
    for category in RootCauseCategory:
        ids = sorted(s.id for s in specs if s.primary == category)
        _rng_for("split", category.value).shuffle(ids)
        n_test = max(1, round(len(ids) / 3)) if len(ids) >= 2 else 0
        for i, case_id in enumerate(ids):
            splits[case_id] = "test" if i < n_test else "dev"
    return splits


# --------------------------------------------------------------------------- #
# Writing and checking
# --------------------------------------------------------------------------- #


def _json_lines(items: list[bytes]) -> bytes:
    return b"[\n" + b",\n".join(items) + b"\n]\n"


def _write_bundle(directory: Path, bundle: Bundle) -> None:
    directory.mkdir(parents=True)
    (directory / "scenario.json").write_bytes(
        bundle.scenario.model_dump_json(indent=2).encode() + b"\n"
    )
    (directory / "events.json").write_bytes(
        _json_lines([e.model_dump_json().encode() for e in bundle.events])
    )
    (directory / "process_snapshot.json").write_bytes(
        bundle.process.model_dump_json(indent=2).encode() + b"\n"
    )
    (directory / "drivers.json").write_bytes(
        TypeAdapter(list[DriverInfo]).dump_json(bundle.drivers, indent=2) + b"\n"
    )
    (directory / "dxdiag.json").write_bytes(
        bundle.dxdiag.model_dump_json(indent=2).encode() + b"\n"
    )
    if bundle.sensors is not None:
        (directory / "sensors.json").write_bytes(
            _json_lines([s.model_dump_json().encode() for s in bundle.sensors])
        )
    if bundle.frametimes is not None:
        (directory / "frametimes.json").write_bytes(
            _json_lines([f.model_dump_json().encode() for f in bundle.frametimes])
        )


def load_recorded(path: Path = RECORDED_CASES) -> list[EvalCase]:
    """The hand-labelled recorded cases; each must point at its committed ``rec-*`` replay."""
    if not path.exists():
        return []
    cases = load_cases(path)
    for case in cases:
        problems = []
        if not case.id.startswith(RECORDED_PREFIX):
            problems.append(f"id must start with {RECORDED_PREFIX!r}")
        if case.source != "recorded":
            problems.append("source must be 'recorded'")
        if case.split != "test":
            problems.append("split must be 'test'")
        if case.scenario != f"evals/scenarios/{case.id}":
            problems.append(f"scenario must be evals/scenarios/{case.id}")
        scenario_file = REPO_ROOT / case.scenario / "scenario.json"
        if not scenario_file.is_file():
            problems.append(f"{scenario_file.relative_to(REPO_ROOT)} is missing")
        elif ReplayScenario.model_validate_json(scenario_file.read_bytes()).id != case.id:
            problems.append("scenario.json id does not match the case id")
        if problems:
            raise ValueError(f"{path.name}: case {case.id!r}: {'; '.join(problems)}")
    return cases


def _is_recorded(path: Path) -> bool:
    return path.name.startswith(RECORDED_PREFIX)


def generate(root: Path, recorded: Path = RECORDED_CASES) -> list[EvalCase]:
    """Write ``root/scenarios/<syn id>/`` and ``root/cases.jsonl``; ``rec-*`` dirs are kept."""
    scenarios = root / "scenarios"
    scenarios.mkdir(parents=True, exist_ok=True)
    for stale in scenarios.iterdir():
        if _is_recorded(stale):
            continue
        if stale.is_dir():
            shutil.rmtree(stale)
        else:
            stale.unlink()
    splits = assign_splits(SPECS)
    cases: list[EvalCase] = []
    for spec in SPECS:
        bundle = build(spec, splits[spec.id])
        _write_bundle(scenarios / spec.id, bundle)
        cases.append(bundle.case)
    cases += load_recorded(recorded)
    duplicates = sorted(i for i, n in Counter(c.id for c in cases).items() if n > 1)
    if duplicates:
        raise ValueError(f"duplicate case ids: {duplicates}")
    (root / "cases.jsonl").write_bytes(
        b"".join(c.model_dump_json().encode() + b"\n" for c in cases)
    )
    return cases


def _tree(root: Path) -> dict[str, bytes]:
    files = {}
    if (root / "cases.jsonl").exists():
        files["cases.jsonl"] = (root / "cases.jsonl").read_bytes()
    scenarios = root / "scenarios"
    if scenarios.exists():
        for p in sorted(scenarios.rglob("*")):
            relative = p.relative_to(scenarios)
            if p.is_file() and not relative.parts[0].startswith(RECORDED_PREFIX):
                files[p.relative_to(root).as_posix()] = p.read_bytes()
    return files


def check(root: Path, recorded: Path = RECORDED_CASES) -> list[str]:
    """Regenerate into a temp dir and list every difference against ``root``.

    Recorded ``rec-*`` scenario directories are not generated and are not compared.
    """
    with tempfile.TemporaryDirectory(prefix="evals-check-") as tmp:
        generate(Path(tmp), recorded)
        expected = _tree(Path(tmp))
    actual = _tree(root)
    drift = [f"missing: {p}" for p in sorted(expected.keys() - actual.keys())]
    drift += [f"unexpected: {p}" for p in sorted(actual.keys() - expected.keys())]
    drift += [
        f"changed: {p}" for p in sorted(expected.keys() & actual.keys()) if expected[p] != actual[p]
    ]
    return drift


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the synthetic eval scenarios.")
    parser.add_argument(
        "--check", action="store_true", help="verify committed output instead of writing"
    )
    parser.add_argument(
        "--root", type=Path, default=EVALS_DIR, help="output directory (default: evals/)"
    )
    args = parser.parse_args(argv)
    if args.check:
        drift = check(args.root)
        if drift:
            print("generated eval data is stale; run: python -m evals.generate", file=sys.stderr)
            for line in drift:
                print(f"  {line}", file=sys.stderr)
            return 1
        print(f"{len(SPECS)} scenarios and {len(load_recorded())} recorded cases up to date")
        return 0
    cases = generate(args.root)
    print(f"wrote {len(SPECS)} scenarios and {len(cases)} cases to cases.jsonl under {args.root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
