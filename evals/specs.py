"""Compact specifications of the synthetic eval cases.

Each :class:`CaseSpec` describes one incident: the machine, the local time the user
refers to, the signal events that explain it (or deliberately do not), and the extra
telemetry the replay directory carries. ``evals.generate`` renders a spec into a replay
directory and embeds the signals in background noise.

Times inside signals are minutes relative to the incident (negative = before it).
Case ids are neutral (``syn-NNN``) so that neither the id nor the scenario metadata
hints at the answer; human-readable intent lives in ``tags`` and in the comments here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pc_diag_mcp.models import DriverInfo, GpuVendor, RootCauseCategory

C = RootCauseCategory

# --------------------------------------------------------------------------- #
# Hardware vocabulary
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Gpu:
    name: str
    vendor: GpuVendor
    service: str  # kernel display driver name as it appears in Display 4101
    manufacturer: str
    driver_version: str
    driver_date: str
    vram_mb: int
    pci_id: str
    hotspot_sensor: bool = True
    feature_levels: tuple[str, ...] = ("12_2", "12_1", "12_0", "11_1", "11_0")


@dataclass(frozen=True)
class Disk:
    name: str  # as reported by the PhysicalDisk counters, e.g. "0 C:"
    model: str
    kind: str  # "nvme" | "sata-ssd" | "hdd"
    port_driver: str  # "stornvme" | "storahci"
    raid_port: int
    pdo: str  # PDO name used by disk 153


@dataclass(frozen=True)
class Machine:
    hostname: str
    user: str
    os_caption: str
    os_build: str
    cpu: str
    cpu_vendor: str
    ram_gb: int
    pagefile_gb: float
    gpus: tuple[Gpu, ...]
    disks: tuple[Disk, ...]
    drivers: tuple[DriverInfo, ...]
    laptop: bool = False
    hdr_display: bool = False
    sensor_backend: bool = True
    # Machine quirks: background events that recur on this host regardless of the incident.
    live_dump_1b0_per_hour: float = 0.0
    whea17_per_hour: float = 0.0


@dataclass(frozen=True)
class App:
    exe: str
    version: str
    path: str
    timestamp: str  # PE header timestamp, hex without 0x


# --------------------------------------------------------------------------- #
# Signals (minutes relative to the incident)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Tdr:
    """Display 4101 (recovered TDR) plus the LiveKernelEvent report that follows it."""

    at: float
    gpu: int = 0
    live_dump: int | None = 0x141
    live_dump_delay_s: float = 40.0


@dataclass(frozen=True)
class LiveDump:
    """A standalone LiveKernelEvent (no Display 4101), e.g. 0x144 USB3 or 0x1b0."""

    at: float
    code: int


@dataclass(frozen=True)
class CpuError:
    apic_id: int
    error_type: str  # "Cache Hierarchy Error", "Bus/Interconnect Error", ...
    mca_bank: int
    mci_status: int


@dataclass(frozen=True)
class Crash:
    """A real bugcheck: the machine reboots ``reboot_s`` later."""

    at: float
    code: int
    params: tuple[int, int, int, int]
    reboot_s: float = 50.0
    sleep_in_progress: int = 0
    whea_fatal: CpuError | None = None  # WHEA 18 logged at the next boot


@dataclass(frozen=True)
class PowerCut:
    """The machine loses power or is switched off hard; no bugcheck."""

    at: float
    reboot_s: float
    power_button_ms: int = 0


@dataclass(frozen=True)
class Restart:
    """A clean restart (User32 1074, Kernel-Power 109, EventLog 6006)."""

    at: float
    by_update: bool = True


@dataclass(frozen=True)
class Sleep:
    at: float
    wake_at: float
    resumed: bool = True


@dataclass(frozen=True)
class WheaCorrectedCpu:
    at: float
    error: CpuError
    count: int = 1
    spread_min: float = 0.0


@dataclass(frozen=True)
class WheaPcie:
    at: float
    count: int
    spread_min: float
    bdf: str = "0x0:0x1:0x2"
    device: str = r"PCI\VEN_1022&DEV_14DB&SUBSYS_14531022&REV_00"
    vendor_id: str = "0x1022"
    device_id: str = "0x14db"


@dataclass(frozen=True)
class AppHang:
    at: float
    app: App


@dataclass(frozen=True)
class AppCrash:
    at: float
    app: App
    module: str
    module_version: str
    module_timestamp: str
    exception: str
    offset: str
    module_path: str


@dataclass(frozen=True)
class LowMemory:
    """Resource-Exhaustion-Detector 2004 naming the top virtual-memory consumers."""

    at: float
    consumers: tuple[tuple[str, int, int], ...]  # (process, pid, bytes)


@dataclass(frozen=True)
class StorageReset:
    at: float
    disk: int = 0


@dataclass(frozen=True)
class DiskRetry:
    at: float
    disk: int = 0
    count: int = 1
    spread_min: float = 0.0


@dataclass(frozen=True)
class BadBlock:
    at: float
    disk: int = 0
    count: int = 1
    spread_min: float = 0.0


@dataclass(frozen=True)
class PagingError:
    at: float
    disk: int = 0


@dataclass(frozen=True)
class ServiceInstall:
    at: float
    name: str
    image_path: str
    start_type: str = "auto start"


@dataclass(frozen=True)
class UpdateInstall:
    at: float
    title: str


@dataclass(frozen=True)
class DriverInstall:
    at: float
    inf: str
    device_instance: str


@dataclass(frozen=True)
class LogCleared:
    at: float


@dataclass(frozen=True)
class Noise:
    """Force one background event of ``kind`` at ``at`` (for deliberate red herrings)."""

    at: float
    kind: str


Signal = (
    Tdr
    | LiveDump
    | Crash
    | PowerCut
    | Restart
    | Sleep
    | WheaCorrectedCpu
    | WheaPcie
    | AppHang
    | AppCrash
    | LowMemory
    | StorageReset
    | DiskRetry
    | BadBlock
    | PagingError
    | ServiceInstall
    | UpdateInstall
    | DriverInstall
    | LogCleared
    | Noise
)

# --------------------------------------------------------------------------- #
# Telemetry beyond the event log
# --------------------------------------------------------------------------- #

Range = tuple[float, float]


@dataclass(frozen=True)
class SensorSeries:
    """Linearly interpolated (shaped by ``curve``) sensor readings with jitter."""

    start: float
    end: float
    interval_s: float = 60.0
    gpu_temp: Range = (70.0, 72.0)
    hotspot_delta: Range = (14.0, 15.0)
    gpu_clock: Range = (1900.0, 1900.0)
    gpu_load: Range = (97.0, 98.0)
    gpu_power: Range = (220.0, 225.0)
    gpu_fan: Range = (1500.0, 1550.0)
    cpu_temp: Range = (65.0, 68.0)
    cpu_load: Range = (35.0, 40.0)
    curve: float = 1.0  # <1 rises early then plateaus, >1 rises late


@dataclass(frozen=True)
class Proc:
    name: str
    working_set_gb: float
    private_gb: float
    page_faults: int
    read_mb: float
    write_mb: float
    cpu_s: float


@dataclass(frozen=True)
class DiskLoad:
    disk: int
    queue_length: float
    percent_busy: float
    read_mb_s: float
    write_mb_s: float


@dataclass(frozen=True)
class ProcessState:
    commit_used_gb: float
    available_gb: float
    top: tuple[Proc, ...] = ()
    disks: tuple[DiskLoad, ...] = ()


@dataclass(frozen=True)
class Frametimes:
    """A short frame-time capture. ``profile``: tdr | thermal | hang | hitch."""

    profile: str
    start: float  # minutes relative to the incident
    duration_s: float = 10.0
    base_ms: float = 8.3
    event_s: float = 5.0


@dataclass(frozen=True)
class CaseSpec:
    id: str
    machine: Machine
    utc_offset_minutes: int
    incident_local: str  # "YYYY-MM-DD HH:MM:SS" in the machine's local time
    boot_before_h: float  # first boot inside the dump window, hours before the incident
    now_after_min: float
    question: str
    primary: RootCauseCategory
    also_acceptable: tuple[RootCauseCategory, ...] = ()
    tags: tuple[str, ...] = ()
    signals: tuple[Signal, ...] = ()
    sensors: tuple[SensorSeries, ...] = ()
    process: ProcessState | None = None
    frametimes: Frametimes | None = None
    extra_drivers: tuple[DriverInfo, ...] = ()
    dxdiag_problems: tuple[str, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------- #
# Machines
# --------------------------------------------------------------------------- #


def _drv(
    name: str, cls: str, provider: str, version: str, date: str, inf: str, signed: bool = True
) -> DriverInfo:
    return DriverInfo(
        device_name=name,
        device_class=cls,
        provider=provider,
        version=version,
        date=date,
        inf_name=inf,
        signed=signed,
    )


RX6800XT = Gpu(
    "AMD Radeon RX 6800 XT",
    GpuVendor.AMD,
    "amdkmdag",
    "Advanced Micro Devices, Inc.",
    "31.0.24033.1003",
    "2024-03-07",
    16368,
    r"PCI\VEN_1002&DEV_73BF&SUBSYS_0E3A1002&REV_C1",
)
RX7900XTX = Gpu(
    "AMD Radeon RX 7900 XTX",
    GpuVendor.AMD,
    "amdkmdag",
    "Advanced Micro Devices, Inc.",
    "32.0.12011.1036",
    "2024-09-12",
    24560,
    r"PCI\VEN_1002&DEV_744C&SUBSYS_0E3B1002&REV_C8",
)
RTX3070 = Gpu(
    "NVIDIA GeForce RTX 3070",
    GpuVendor.NVIDIA,
    "nvlddmkm",
    "NVIDIA",
    "32.0.15.6094",
    "2024-08-14",
    8192,
    r"PCI\VEN_10DE&DEV_2484&SUBSYS_146B10DE&REV_A1",
)
GTX1660S = Gpu(
    "NVIDIA GeForce GTX 1660 SUPER",
    GpuVendor.NVIDIA,
    "nvlddmkm",
    "NVIDIA",
    "32.0.15.6094",
    "2024-08-14",
    6144,
    r"PCI\VEN_10DE&DEV_21C4&SUBSYS_C7581462&REV_A1",
    hotspot_sensor=False,
    feature_levels=("12_1", "12_0", "11_1", "11_0"),
)
RTX4070TI = Gpu(
    "NVIDIA GeForce RTX 4070 Ti",
    GpuVendor.NVIDIA,
    "nvlddmkm",
    "NVIDIA",
    "32.0.15.6109",
    "2024-09-17",
    12282,
    r"PCI\VEN_10DE&DEV_2782&SUBSYS_51151462&REV_A1",
)
RTX4080S = Gpu(
    "NVIDIA GeForce RTX 4080 SUPER",
    GpuVendor.NVIDIA,
    "nvlddmkm",
    "NVIDIA",
    "32.0.15.6094",
    "2024-08-14",
    16376,
    r"PCI\VEN_10DE&DEV_2702&SUBSYS_88F01043&REV_A1",
)
RTX4060M = Gpu(
    "NVIDIA GeForce RTX 4060 Laptop GPU",
    GpuVendor.NVIDIA,
    "nvlddmkm",
    "NVIDIA",
    "32.0.15.6094",
    "2024-08-14",
    8188,
    r"PCI\VEN_10DE&DEV_28A0&SUBSYS_3B9E17AA&REV_A1",
)
IRIS_XE = Gpu(
    "Intel(R) Iris(R) Xe Graphics",
    GpuVendor.INTEL,
    "igfx",
    "Intel Corporation",
    "31.0.101.5592",
    "2024-06-27",
    128,
    r"PCI\VEN_8086&DEV_46A6&SUBSYS_0B1D1028&REV_0C",
    hotspot_sensor=False,
)
IRIS_XE_HYBRID = Gpu(
    "Intel(R) Iris(R) Xe Graphics",
    GpuVendor.INTEL,
    "igfx",
    "Intel Corporation",
    "31.0.101.5333",
    "2024-03-01",
    128,
    r"PCI\VEN_8086&DEV_A7A0&SUBSYS_3B9E17AA&REV_04",
    hotspot_sensor=False,
)

_NVME0 = r"\Device\00000034"
SN770 = Disk("0 C:", "WD_BLACK SN770 1TB", "nvme", "stornvme", 0, _NVME0)
PRO980 = Disk("0 C:", "Samsung SSD 980 PRO 1TB", "nvme", "stornvme", 0, _NVME0)
PRO980_2T = Disk("0 C:", "Samsung SSD 980 PRO 2TB", "nvme", "stornvme", 0, _NVME0)
PRO990 = Disk("0 C:", "Samsung SSD 990 PRO 2TB", "nvme", "stornvme", 0, _NVME0)
KIOXIA = Disk("0 C:", "KIOXIA KBG50ZNV512G", "nvme", "stornvme", 0, _NVME0)
PC801 = Disk("0 C:", "SK hynix PC801 HFS001TEJ9X101N", "nvme", "stornvme", 0, _NVME0)
WD_HDD = Disk("1 D:", "WDC WD20EZBX-00AYRA0", "hdd", "storahci", 1, r"\Device\0000003a")
MX500 = Disk("0 C:", "CT500MX500SSD1", "sata-ssd", "storahci", 0, r"\Device\00000031")
SEAGATE_HDD = Disk("1 D:", "ST1000DM010-2EP102", "hdd", "storahci", 0, r"\Device\00000032")

_MS_NVME = _drv(
    "Standard NVM Express Controller",
    "SCSIAdapter",
    "Microsoft",
    "10.0.22621.4111",
    "2006-06-21",
    "stornvme.inf",
)
_MS_AHCI = _drv(
    "Standard SATA AHCI Controller",
    "HDC",
    "Microsoft",
    "10.0.19041.3636",
    "2006-06-21",
    "mshdc.inf",
)
_RT_AUDIO = _drv(
    "Realtek(R) Audio",
    "MEDIA",
    "Realtek Semiconductor Corp.",
    "6.0.9580.1",
    "2023-09-18",
    "oem31.inf",
)
_I225 = _drv(
    "Intel(R) Ethernet Controller I225-V", "Net", "Intel", "1.1.4.43", "2023-08-04", "oem12.inf"
)
_RT_LAN = _drv(
    "Realtek Gaming 2.5GbE Family Controller",
    "Net",
    "Realtek",
    "10.68.815.2023",
    "2023-08-15",
    "oem18.inf",
)
_AX211 = _drv(
    "Intel(R) Wi-Fi 6E AX211 160MHz", "Net", "Intel", "23.30.0.6", "2024-04-02", "oem44.inf"
)
_AMD_CHIPSET = _drv(
    "AMD GPIO Controller",
    "System",
    "Advanced Micro Devices, Inc",
    "2.2.0.130",
    "2023-02-13",
    "oem9.inf",
)
_AMD_PSP = _drv(
    "AMD PSP 11.0 Device",
    "SecurityDevices",
    "Advanced Micro Devices Inc.",
    "5.25.0.0",
    "2023-05-08",
    "oem15.inf",
)
_INTEL_ME = _drv(
    "Intel(R) Management Engine Interface #1",
    "System",
    "Intel",
    "2406.5.5.0",
    "2024-03-18",
    "oem7.inf",
)
_INTEL_SERIAL_IO = _drv(
    "Intel(R) Serial IO I2C Host Controller - 51E8",
    "System",
    "Intel Corporation",
    "30.100.2237.25",
    "2022-09-11",
    "oem22.inf",
)


def _gpu_driver(gpu: Gpu, inf: str) -> DriverInfo:
    return _drv(gpu.name, "Display", gpu.manufacturer, gpu.driver_version, gpu.driver_date, inf)


DESK_AMD_6800 = Machine(
    hostname="DESKTOP-K4R7Q2M",
    user="jordan",
    os_caption="Microsoft Windows 11 Pro",
    os_build="22631.4317",
    cpu="AMD Ryzen 7 5800X3D 8-Core Processor",
    cpu_vendor="amd",
    ram_gb=32,
    pagefile_gb=4.75,
    gpus=(RX6800XT,),
    disks=(PRO980, WD_HDD),
    drivers=(
        _gpu_driver(RX6800XT, "oem61.inf"),
        _RT_AUDIO,
        _I225,
        _MS_NVME,
        _AMD_CHIPSET,
        _AMD_PSP,
    ),
    hdr_display=True,
    live_dump_1b0_per_hour=0.3,
)
DESK_NV_3070 = Machine(
    hostname="DESKTOP-9VB2T6L",
    user="sam",
    os_caption="Microsoft Windows 11 Home",
    os_build="22631.4317",
    cpu="12th Gen Intel(R) Core(TM) i7-12700K",
    cpu_vendor="intel",
    ram_gb=32,
    pagefile_gb=4.75,
    gpus=(RTX3070,),
    disks=(SN770,),
    drivers=(_gpu_driver(RTX3070, "oem72.inf"), _RT_AUDIO, _I225, _MS_NVME, _INTEL_ME),
)
LAPTOP_INTEL = Machine(
    hostname="LAPTOP-3HQ8N1RD",
    user="priya",
    os_caption="Microsoft Windows 11 Home",
    os_build="22621.4317",
    cpu="12th Gen Intel(R) Core(TM) i5-1240P",
    cpu_vendor="intel",
    ram_gb=16,
    pagefile_gb=2.5,
    gpus=(IRIS_XE,),
    disks=(KIOXIA,),
    drivers=(
        _gpu_driver(IRIS_XE, "oem38.inf"),
        _RT_AUDIO,
        _AX211,
        _MS_NVME,
        _INTEL_ME,
        _INTEL_SERIAL_IO,
    ),
    laptop=True,
    sensor_backend=False,
)
DESK_NV_1660 = Machine(
    hostname="DESKTOP-2MX8P0C",
    user="alex",
    os_caption="Microsoft Windows 10 Pro",
    os_build="19045.5011",
    cpu="Intel(R) Core(TM) i5-9400F CPU @ 2.90GHz",
    cpu_vendor="intel",
    ram_gb=16,
    pagefile_gb=2.4,
    gpus=(GTX1660S,),
    disks=(MX500, SEAGATE_HDD),
    drivers=(_gpu_driver(GTX1660S, "oem54.inf"), _RT_AUDIO, _RT_LAN, _MS_AHCI, _INTEL_ME),
)
DESK_NV_4070TI = Machine(
    hostname="DESKTOP-7PL3W9X",
    user="chris",
    os_caption="Microsoft Windows 11 Pro",
    os_build="26100.2033",
    cpu="AMD Ryzen 9 7900X 12-Core Processor",
    cpu_vendor="amd",
    ram_gb=32,
    pagefile_gb=4.75,
    gpus=(RTX4070TI,),
    disks=(PRO990,),
    drivers=(_gpu_driver(RTX4070TI, "oem88.inf"), _RT_AUDIO, _RT_LAN, _MS_NVME, _AMD_CHIPSET),
    hdr_display=True,
)
DESK_AMD_7900 = Machine(
    hostname="DESKTOP-5RW2E8N",
    user="mika",
    os_caption="Microsoft Windows 11 Pro",
    os_build="26100.2033",
    cpu="AMD Ryzen 7 7800X3D 8-Core Processor",
    cpu_vendor="amd",
    ram_gb=32,
    pagefile_gb=4.75,
    gpus=(RX7900XTX,),
    disks=(PRO990,),
    drivers=(
        _gpu_driver(RX7900XTX, "oem47.inf"),
        _RT_AUDIO,
        _RT_LAN,
        _MS_NVME,
        _AMD_CHIPSET,
        _AMD_PSP,
    ),
    hdr_display=True,
    whea17_per_hour=0.3,
)
LAPTOP_NV = Machine(
    hostname="LAPTOP-6TJ2K5QF",
    user="dana",
    os_caption="Microsoft Windows 11 Home",
    os_build="22631.4317",
    cpu="13th Gen Intel(R) Core(TM) i7-13700H",
    cpu_vendor="intel",
    ram_gb=16,
    pagefile_gb=7.6,
    gpus=(RTX4060M, IRIS_XE_HYBRID),
    disks=(PC801,),
    drivers=(
        _gpu_driver(RTX4060M, "oem91.inf"),
        _gpu_driver(IRIS_XE_HYBRID, "oem40.inf"),
        _RT_AUDIO,
        _AX211,
        _MS_NVME,
        _INTEL_ME,
    ),
    laptop=True,
)
DESK_NV_4080 = Machine(
    hostname="DESKTOP-1QZ6M3V",
    user="taylor",
    os_caption="Microsoft Windows 11 Pro",
    os_build="22631.4317",
    cpu="13th Gen Intel(R) Core(TM) i9-13900K",
    cpu_vendor="intel",
    ram_gb=64,
    pagefile_gb=8.0,
    gpus=(RTX4080S,),
    disks=(PRO980_2T,),
    drivers=(_gpu_driver(RTX4080S, "oem66.inf"), _RT_AUDIO, _I225, _MS_NVME, _INTEL_ME),
)

# --------------------------------------------------------------------------- #
# Applications (games are fictional; Windows components are real)
# --------------------------------------------------------------------------- #

STARFORGE = App(
    "StarforgeOnline.exe",
    "1.14.2.0",
    r"D:\SteamLibrary\steamapps\common\Starforge Online\bin\StarforgeOnline.exe",
    "66f1a2b3",
)
RALLY = App(
    "RallyHorizonX.exe",
    "2.3.118.0",
    r"C:\Program Files (x86)\Steam\steamapps\common\Rally Horizon X\RallyHorizonX.exe",
    "6702c41e",
)
IRONCLAD = App(
    "IroncladTactics.exe",
    "0.9.77.4021",
    r"C:\Games\Ironclad Tactics\IroncladTactics.exe",
    "66c8d0f2",
)
EXPLORER = App(
    "explorer.exe",
    "10.0.22621.4317",
    r"C:\Windows\explorer.exe",
    "a0e4f7c1",
)

# --------------------------------------------------------------------------- #
# Cases. Three per category; ambiguous ones list extra acceptable categories.
# --------------------------------------------------------------------------- #

_SPECS: tuple[CaseSpec, ...] = (
    # ---- gpu_driver_timeout ------------------------------------------------
    # Recovered TDR on AMD while gaming; temperatures normal, so not thermal.
    CaseSpec(
        id="syn-026",
        machine=DESK_AMD_6800,
        utc_offset_minutes=-240,
        incident_local="2026-10-14 21:43:08",
        boot_before_h=3.2,
        now_after_min=25,
        question=(
            "My screen went black for a couple of seconds around 9:42 tonight while I was "
            "playing, then the game came back but it was choppy for a bit. What happened?"
        ),
        primary=C.GPU_DRIVER_TIMEOUT,
        tags=("gpu", "tdr", "frametimes"),
        signals=(Tdr(0.0, live_dump=0x117, live_dump_delay_s=52),),
        sensors=(
            SensorSeries(
                -45,
                2,
                gpu_temp=(70, 74),
                gpu_clock=(2310, 2250),
                gpu_power=(262, 281),
                gpu_fan=(1480, 1620),
            ),
        ),
        frametimes=Frametimes("tdr", start=-5 / 60, duration_s=10, base_ms=8.3, event_s=5.0),
    ),
    # Two recovered TDRs, then the reset fails: VIDEO_TDR_FAILURE (0x116) and reboot.
    CaseSpec(
        id="syn-001",
        machine=DESK_NV_3070,
        utc_offset_minutes=60,
        incident_local="2026-10-09 23:06:51",
        boot_before_h=2.4,
        now_after_min=545,
        question=(
            "PC rebooted by itself while gaming last night, a bit after 11. The screen had "
            "gone black a couple of times right before. Why?"
        ),
        primary=C.GPU_DRIVER_TIMEOUT,
        tags=("gpu", "tdr", "bugcheck:0x116", "crash-reboot"),
        signals=(
            Tdr(-14.2, live_dump=0x141),
            Tdr(-3.1, live_dump=0x141, live_dump_delay_s=25),
            Crash(
                0.0,
                0x116,
                (0xFFFFC30A7E4A2010, 0xFFFFF8064F6C9E40, 0xFFFFFFFFC000009A, 0x4),
                reboot_s=48,
            ),
        ),
    ),
    # Intel iGPU TDRs while watching video; an unrelated SearchHost crash nearby.
    CaseSpec(
        id="syn-018",
        machine=LAPTOP_INTEL,
        utc_offset_minutes=330,
        incident_local="2026-10-17 15:22:40",
        boot_before_h=4.0,
        now_after_min=70,
        question=(
            "Screen flickered black twice this afternoon while I was watching a video in Edge, "
            "around 3:20. Is my laptop dying?"
        ),
        primary=C.GPU_DRIVER_TIMEOUT,
        tags=("gpu", "tdr", "laptop", "red-herring"),
        signals=(
            Tdr(0.0, live_dump=0x141),
            Tdr(3.2, live_dump=0x141, live_dump_delay_s=31),
            Noise(-7.4, "app_crash"),
            Noise(1.1, "dcom"),
        ),
    ),
    # ---- gpu_thermal --------------------------------------------------------
    # Dusty GTX 1660 SUPER: climbs to its 83 C target, clocks fall, frame times worsen.
    CaseSpec(
        id="syn-015",
        machine=DESK_NV_1660,
        utc_offset_minutes=-300,
        incident_local="2026-10-11 20:15:00",
        boot_before_h=2.5,
        now_after_min=6,
        question=(
            "My game starts stuttering after about 20 minutes of playing and it got really bad "
            "around 8:15. Restarting the game doesn't help and nothing crashes."
        ),
        primary=C.GPU_THERMAL,
        tags=("gpu", "needs:sensors", "frametimes", "stutter"),
        sensors=(
            SensorSeries(
                -52,
                5,
                gpu_temp=(58, 83),
                gpu_clock=(1965, 1395),
                gpu_load=(97, 99),
                gpu_power=(124, 96),
                gpu_fan=(1450, 3150),
                cpu_temp=(62, 70),
                curve=0.55,
            ),
        ),
        frametimes=Frametimes("thermal", start=-0.1, duration_s=8, base_ms=11.0),
    ),
    # Junction temperature runs away on an RX 7900 XTX, then a recovered TDR.
    CaseSpec(
        id="syn-013",
        machine=DESK_AMD_7900,
        utc_offset_minutes=120,
        incident_local="2026-10-06 22:20:13",
        boot_before_h=3.0,
        now_after_min=12,
        question=(
            "The game froze for a moment then recovered around 10:20 pm, the screen flashed "
            "black. It has happened a few evenings now."
        ),
        primary=C.GPU_THERMAL,
        also_acceptable=(C.GPU_DRIVER_TIMEOUT,),
        tags=("gpu", "tdr", "needs:sensors", "red-herring"),
        signals=(
            Noise(-6.0, "app_crash"),
            Tdr(0.0, live_dump=0x117, live_dump_delay_s=44),
        ),
        sensors=(
            SensorSeries(
                -40,
                3,
                gpu_temp=(71, 93),
                hotspot_delta=(18, 21),
                gpu_clock=(2510, 2080),
                gpu_power=(342, 309),
                gpu_fan=(1800, 3300),
                cpu_temp=(72, 80),
                curve=1.6,
            ),
        ),
    ),
    # GPU fan reads 0 RPM under load; temperature climbs until the machine cuts out.
    CaseSpec(
        id="syn-012",
        machine=DESK_NV_4070TI,
        utc_offset_minutes=-420,
        incident_local="2026-10-19 18:51:37",
        boot_before_h=1.6,
        now_after_min=15,
        question=(
            "My PC just shut off by itself in the middle of a match around 6:50 and I had to "
            "turn it back on. No blue screen."
        ),
        primary=C.GPU_THERMAL,
        also_acceptable=(C.POWER_LOSS,),
        tags=("gpu", "needs:sensors", "crash-reboot"),
        signals=(PowerCut(0.0, reboot_s=190),),
        sensors=(
            SensorSeries(
                -38,
                -0.5,
                interval_s=30,
                gpu_temp=(64, 97),
                hotspot_delta=(14, 19),
                gpu_clock=(2730, 2190),
                gpu_power=(281, 236),
                gpu_fan=(0, 0),
                cpu_temp=(70, 79),
                curve=0.8,
            ),
        ),
    ),
    # ---- power_loss ---------------------------------------------------------
    # Overnight outage; a Defender platform update just before is the tempting suspect.
    CaseSpec(
        id="syn-003",
        machine=DESK_AMD_6800,
        utc_offset_minutes=60,
        incident_local="2026-10-04 03:12:26",
        boot_before_h=6.1,
        now_after_min=293,
        question=(
            "Came down this morning and my PC had restarted, it was sitting on the login screen. "
            "Did Windows Update do this overnight?"
        ),
        primary=C.POWER_LOSS,
        also_acceptable=(C.HARDWARE_ERROR, C.INSUFFICIENT_EVIDENCE),
        tags=("power", "red-herring", "overnight"),
        signals=(
            UpdateInstall(
                -14.0,
                "Microsoft Defender Antivirus Antimalware Platform Update - "
                "KB4052623 (Version 4.18.24090.11)",
            ),
            PowerCut(0.0, reboot_s=88),
        ),
    ),
    # Instant off under gaming load with normal temperatures; power draw spiking.
    CaseSpec(
        id="syn-014",
        machine=DESK_NV_4070TI,
        utc_offset_minutes=-240,
        incident_local="2026-10-12 21:15:49",
        boot_before_h=2.2,
        now_after_min=9,
        question=(
            "PC turned off instantly while playing around 9:15 pm, no blue screen, just black "
            "and then it was off. Third time this week, always in games."
        ),
        primary=C.POWER_LOSS,
        also_acceptable=(C.HARDWARE_ERROR,),
        tags=("power", "needs:sensors", "crash-reboot"),
        signals=(PowerCut(0.0, reboot_s=140),),
        sensors=(
            SensorSeries(
                -30,
                -0.2,
                interval_s=20,
                gpu_temp=(63, 67),
                gpu_clock=(2760, 2745),
                gpu_power=(255, 312),
                gpu_fan=(1350, 1420),
                cpu_temp=(68, 74),
                cpu_load=(45, 58),
                curve=3.0,
            ),
        ),
    ),
    # Sudden restart in a voice call; an earlier recovered TDR the same day is a decoy.
    CaseSpec(
        id="syn-017",
        machine=DESK_AMD_7900,
        utc_offset_minutes=660,
        incident_local="2026-10-15 19:31:02",
        boot_before_h=9.0,
        now_after_min=18,
        question=(
            "My computer just turned off and restarted around 7:30 while I was in a Discord call. "
            "Was it the graphics driver again? The screen went black earlier today too."
        ),
        primary=C.POWER_LOSS,
        also_acceptable=(C.HARDWARE_ERROR, C.INSUFFICIENT_EVIDENCE),
        tags=("power", "red-herring", "crash-reboot"),
        signals=(Tdr(-312.0, live_dump=0x117), PowerCut(0.0, reboot_s=75)),
    ),
    # ---- hardware_error -----------------------------------------------------
    # Corrected machine checks for hours, then a fatal one: WHEA_UNCORRECTABLE_ERROR.
    CaseSpec(
        id="syn-016",
        machine=DESK_NV_4080,
        utc_offset_minutes=-300,
        incident_local="2026-10-08 14:02:44",
        boot_before_h=5.0,
        now_after_min=20,
        question=(
            "Got a blue screen and restart around 2 pm while a game was compiling shaders. What "
            "caused it?"
        ),
        primary=C.HARDWARE_ERROR,
        tags=("cpu", "whea", "bugcheck:0x124", "crash-reboot", "red-herring"),
        signals=(
            Noise(-4.5, "app_crash"),
            WheaCorrectedCpu(
                -170,
                CpuError(16, "Cache Hierarchy Error", 0, 0x9C00000000010150),
                count=4,
                spread_min=150,
            ),
            Crash(
                0.0,
                0x124,
                (0x0, 0xFFFFB38C1E2F3028, 0xBE000000, 0x800400),
                reboot_s=46,
                whea_fatal=CpuError(16, "Cache Hierarchy Error", 0, 0xBE00000000800400),
            ),
        ),
    ),
    # Three different bugchecks in two days, no WHEA records: classic bad-RAM pattern,
    # but a misbehaving driver cannot be ruled out from the logs alone.
    CaseSpec(
        id="syn-020",
        machine=DESK_NV_1660,
        utc_offset_minutes=120,
        incident_local="2026-10-20 10:41:19",
        boot_before_h=27.0,
        now_after_min=14,
        question=(
            "Third blue screen in two days, and each time it shows a different error. "
            "Latest one was around 10:40 this morning."
        ),
        primary=C.HARDWARE_ERROR,
        also_acceptable=(C.KERNEL_DRIVER_FAULT,),
        tags=("memory-module", "bugcheck:varied", "crash-reboot"),
        signals=(
            Crash(-1530, 0x1A, (0x41790, 0xFFFFE50002A0E5D0, 0xFFFF, 0x0), reboot_s=55),
            Crash(-545, 0x50, (0xFFFF9E8D6C5A3FF8, 0x2, 0xFFFFF8034A6E21C3, 0x2), reboot_s=52),
            Crash(
                0.0, 0x3B, (0xC0000005, 0xFFFFF8034A8F17B2, 0xFFFFD5013C6EE8F0, 0x0), reboot_s=49
            ),
        ),
    ),
    # Ryzen idle reboot: WHEA 18 Bus/Interconnect error after a 0x124.
    CaseSpec(
        id="syn-024",
        machine=DESK_AMD_7900,
        utc_offset_minutes=-360,
        incident_local="2026-10-13 13:28:55",
        boot_before_h=4.5,
        now_after_min=40,
        question=(
            "PC randomly restarts when it's just sitting there doing nothing, it happened again "
            "around 1:30 this afternoon. Never while gaming."
        ),
        primary=C.HARDWARE_ERROR,
        tags=("cpu", "whea", "bugcheck:0x124", "idle", "crash-reboot", "red-herring"),
        signals=(
            Noise(-8.0, "whea17"),
            Crash(
                0.0,
                0x124,
                (0x0, 0xFFFF8D0F3B6E2028, 0xBAA00000, 0x108),
                reboot_s=44,
                whea_fatal=CpuError(0, "Bus/Interconnect Error", 5, 0xBAA0000000000108),
            ),
        ),
    ),
    # ---- kernel_driver_fault ------------------------------------------------
    # A third-party RGB kernel driver installed yesterday; two 0xD1 bugchecks since.
    CaseSpec(
        id="syn-027",
        machine=DESK_NV_3070,
        utc_offset_minutes=-240,
        incident_local="2026-10-16 21:40:31",
        boot_before_h=28.0,
        now_after_min=16,
        question=(
            "Blue screen again around 9:40 tonight. This started yesterday and I haven't "
            "changed anything."
        ),
        primary=C.KERNEL_DRIVER_FAULT,
        tags=("kernel-driver", "bugcheck:0xd1", "new-driver", "crash-reboot"),
        signals=(
            ServiceInstall(
                -1500,
                "LumaSync RGB Kernel Service",
                r"\??\C:\Program Files\LumaSync\drivers\lsrgbk64.sys",
            ),
            Crash(-312, 0xD1, (0x28, 0x2, 0x0, 0xFFFFF80562A31B4C), reboot_s=51),
            Crash(0.0, 0xD1, (0x28, 0x2, 0x0, 0xFFFFF80562A31B4C), reboot_s=50),
        ),
        extra_drivers=(
            _drv(
                "LumaSync RGB Controller",
                "System",
                "LumaSync Labs",
                "1.0.3.0",
                "2026-09-30",
                "oem97.inf",
            ),
        ),
    ),
    # Wi-Fi driver updated last night; DRIVER_POWER_STATE_FAILURE on resume from sleep.
    CaseSpec(
        id="syn-025",
        machine=LAPTOP_INTEL,
        utc_offset_minutes=-240,
        incident_local="2026-10-21 08:10:22",
        boot_before_h=12.0,
        now_after_min=11,
        question=(
            "Opened my laptop lid around 8:10 this morning and it showed a blue screen and "
            "restarted."
        ),
        primary=C.KERNEL_DRIVER_FAULT,
        tags=("kernel-driver", "bugcheck:0x9f", "sleep-resume", "laptop", "crash-reboot"),
        signals=(
            UpdateInstall(-640, "Intel Corporation - Net - 23.40.0.4"),
            DriverInstall(
                -639,
                "netwtw6e.inf",
                r"PCI\VEN_8086&DEV_51F1&SUBSYS_00948086&REV_01\3&11583659&0&A3",
            ),
            Sleep(-512, -0.4, resumed=False),
            Crash(
                0.0,
                0x9F,
                (0x3, 0xFFFFA58C3F2C6060, 0xFFFFF80772A7F750, 0xFFFFA58C41D7E8A0),
                reboot_s=58,
                sleep_in_progress=1,
            ),
        ),
        extra_drivers=(
            _drv(
                "Intel(R) Wi-Fi 6E AX211 160MHz",
                "Net",
                "Intel",
                "23.40.0.4",
                "2026-10-02",
                "oem52.inf",
            ),
        ),
    ),
    # VIDEO_MEMORY_MANAGEMENT_INTERNAL right after a GPU driver update: a driver fault in
    # the display stack, arguably also a GPU-driver problem.
    CaseSpec(
        id="syn-006",
        machine=DESK_NV_4070TI,
        utc_offset_minutes=-300,
        incident_local="2026-10-10 23:31:12",
        boot_before_h=22.0,
        now_after_min=13,
        question=(
            "Got a blue screen right as I alt-tabbed out of a game around 11:30 pm. "
            "I just updated my GPU driver yesterday."
        ),
        primary=C.KERNEL_DRIVER_FAULT,
        also_acceptable=(C.GPU_DRIVER_TIMEOUT,),
        tags=("gpu", "kernel-driver", "bugcheck:0x10e", "crash-reboot"),
        signals=(
            DriverInstall(
                -1260,
                "nv_dispig.inf",
                r"PCI\VEN_10DE&DEV_2782&SUBSYS_51151462&REV_A1\4&2b8b1c4d&0&0019",
            ),
            Crash(0.0, 0x10E, (0x1F, 0xFFFFD28B6D3E2000, 0x0, 0x0), reboot_s=47),
        ),
    ),
    # ---- memory_pressure ----------------------------------------------------
    # Commit exhausted on a 16 GB laptop; the game hangs and is killed.
    CaseSpec(
        id="syn-007",
        machine=LAPTOP_NV,
        utc_offset_minutes=-180,
        incident_local="2026-10-18 21:42:57",
        boot_before_h=5.5,
        now_after_min=6,
        question=(
            "My game froze completely around 9:42 and Windows said it's not responding, I had "
            "to kill it and start it again. The laptop is still sluggish."
        ),
        primary=C.MEMORY_PRESSURE,
        also_acceptable=(C.APPLICATION_HANG,),
        tags=("memory", "needs:process", "app-hang", "laptop", "red-herring"),
        signals=(
            Noise(-9.0, "live_dump_1b0"),
            LowMemory(
                -2.2,
                (
                    ("IroncladTactics.exe", 18244, 9_318_211_584),
                    ("chrome.exe", 11532, 3_841_736_704),
                    ("Discord.exe", 7712, 1_093_545_984),
                ),
            ),
            AppHang(0.0, IRONCLAD),
        ),
        process=ProcessState(
            commit_used_gb=21.7,
            available_gb=0.34,
            top=(
                Proc("IroncladTactics.exe", 4.8, 7.4, 22_310_458, 2240, 120, 310),
                Proc("chrome.exe", 1.9, 3.6, 14_202_331, 880, 1210, 910),
                Proc("Discord.exe", 0.6, 1.0, 3_100_220, 210, 160, 240),
                Proc("MsMpEng.exe", 0.3, 0.4, 2_210_004, 1500, 90, 620),
            ),
            disks=(DiskLoad(0, 3.8, 71, 41.0, 88.0),),
        ),
    ),
    # Ongoing: commit at the limit, heavy paging, no event at all.
    CaseSpec(
        id="syn-023",
        machine=DESK_NV_1660,
        utc_offset_minutes=540,
        incident_local="2026-10-07 22:05:10",
        boot_before_h=3.5,
        now_after_min=2,
        question=(
            "Right now everything is super laggy, my game hitches every few seconds and "
            "alt-tabbing takes forever."
        ),
        primary=C.MEMORY_PRESSURE,
        tags=("memory", "needs:process", "ongoing"),
        sensors=(
            SensorSeries(
                -20,
                2,
                gpu_temp=(66, 68),
                gpu_clock=(1905, 1890),
                gpu_power=(112, 118),
                gpu_fan=(1400, 1450),
                gpu_load=(55, 62),
            ),
        ),
        process=ProcessState(
            commit_used_gb=17.9,
            available_gb=0.21,
            top=(
                Proc("RallyHorizonX.exe", 4.1, 7.9, 48_202_911, 5120, 640, 2210),
                Proc("chrome.exe", 1.2, 2.9, 19_877_102, 1320, 1530, 1340),
                Proc("obs64.exe", 0.7, 1.4, 6_302_118, 120, 9820, 1750),
                Proc("Discord.exe", 0.4, 0.9, 2_811_330, 180, 140, 310),
                Proc("steamwebhelper.exe", 0.3, 0.6, 1_955_207, 95, 70, 160),
            ),
            disks=(DiskLoad(0, 6.5, 92, 58.0, 74.0),),
        ),
    ),
    # A leaking background app eats commit; the game crashes with a C++ exception.
    CaseSpec(
        id="syn-021",
        machine=DESK_NV_3070,
        utc_offset_minutes=540,
        incident_local="2026-10-22 16:05:33",
        boot_before_h=7.0,
        now_after_min=8,
        question=(
            "My game crashed to desktop around 4:05 this afternoon with no error message. That's "
            "twice today."
        ),
        primary=C.MEMORY_PRESSURE,
        tags=("memory", "needs:process", "app-crash"),
        signals=(
            LowMemory(
                -186,
                (
                    ("MediaIndexerPro.exe", 6604, 19_744_120_832),
                    ("StarforgeOnline.exe", 14120, 8_811_403_264),
                    ("chrome.exe", 9932, 2_310_520_832),
                ),
            ),
            AppCrash(
                -183,
                STARFORGE,
                "KERNELBASE.dll",
                "10.0.22621.4317",
                "a3c1d9e4",
                "0xe06d7363",
                "0x000000000005fb4c",
                r"C:\WINDOWS\System32\KERNELBASE.dll",
            ),
            LowMemory(
                -1.6,
                (
                    ("MediaIndexerPro.exe", 6604, 23_918_272_512),
                    ("StarforgeOnline.exe", 20388, 8_940_175_360),
                    ("chrome.exe", 9932, 2_402_811_904),
                ),
            ),
            AppCrash(
                0.0,
                STARFORGE,
                "KERNELBASE.dll",
                "10.0.22621.4317",
                "a3c1d9e4",
                "0xe06d7363",
                "0x000000000005fb4c",
                r"C:\WINDOWS\System32\KERNELBASE.dll",
            ),
        ),
        process=ProcessState(
            commit_used_gb=35.1,
            available_gb=0.9,
            top=(
                Proc("MediaIndexerPro.exe", 3.2, 23.4, 9_120_442, 18210, 2240, 5120),
                Proc("chrome.exe", 1.4, 2.4, 8_870_100, 640, 820, 780),
                Proc("steam.exe", 0.3, 0.4, 1_204_118, 210, 330, 120),
                Proc("Discord.exe", 0.4, 0.8, 2_010_550, 160, 120, 260),
            ),
        ),
    ),
    # ---- storage_io ---------------------------------------------------------
    # NVMe controller resets; the system disk queue is saturated right now.
    CaseSpec(
        id="syn-010",
        machine=DESK_AMD_6800,
        utc_offset_minutes=60,
        incident_local="2026-10-05 16:10:40",
        boot_before_h=5.0,
        now_after_min=27,
        question=(
            "The whole PC freezes for like 30 seconds then comes back. It happened around 4:10 "
            "and it's doing it again right now."
        ),
        primary=C.STORAGE_IO,
        tags=("storage", "needs:process", "needs:disk", "nvme", "red-herring"),
        signals=(
            Noise(-3.0, "live_dump_1b0"),
            StorageReset(-62.0),
            DiskRetry(-61.8, count=2, spread_min=0.3),
            StorageReset(-31.5),
            DiskRetry(-31.2),
            StorageReset(0.0),
            DiskRetry(0.2, count=3, spread_min=0.4),
            StorageReset(26.6),
            DiskRetry(26.8),
        ),
        process=ProcessState(
            commit_used_gb=14.2,
            available_gb=17.5,
            top=(
                Proc("StarforgeOnline.exe", 6.2, 7.0, 4_102_331, 2050, 310, 960),
                Proc("chrome.exe", 1.1, 1.6, 3_220_991, 310, 420, 410),
                Proc("explorer.exe", 0.2, 0.1, 1_002_448, 60, 40, 55),
            ),
            disks=(DiskLoad(0, 38.5, 100, 0.35, 0.1), DiskLoad(1, 0.0, 0.4, 0.0, 0.0)),
        ),
    ),
    # Old HDD with bad blocks and retries; the game lives on that disk.
    CaseSpec(
        id="syn-002",
        machine=DESK_NV_1660,
        utc_offset_minutes=-300,
        incident_local="2026-10-16 19:55:18",
        boot_before_h=3.0,
        now_after_min=12,
        question=(
            "The game froze on the loading screen for ages around 7:55 and then eventually loaded. "
            "It's been doing this a lot lately."
        ),
        primary=C.STORAGE_IO,
        tags=("storage", "needs:process", "needs:disk", "hdd", "red-herring"),
        signals=(
            Noise(-5.0, "app_crash"),
            DiskRetry(-150.0, disk=1, count=3, spread_min=90),
            BadBlock(-1.0, disk=1, count=2, spread_min=1.0),
            DiskRetry(-0.8, disk=1, count=4, spread_min=1.2),
            StorageReset(0.4, disk=1),
        ),
        process=ProcessState(
            commit_used_gb=9.8,
            available_gb=5.1,
            top=(
                Proc("RallyHorizonX.exe", 3.8, 5.2, 3_874_112, 9120, 220, 640),
                Proc("chrome.exe", 0.9, 1.3, 2_404_810, 210, 330, 290),
                Proc("Discord.exe", 0.4, 0.8, 1_330_202, 120, 90, 150),
            ),
            disks=(DiskLoad(0, 0.1, 3.0, 1.2, 0.8), DiskLoad(1, 9.4, 100, 1.2, 0.0)),
        ),
    ),
    # Paging I/O fails on the NVMe disk: KERNEL_DATA_INPAGE_ERROR.
    CaseSpec(
        id="syn-008",
        machine=LAPTOP_INTEL,
        utc_offset_minutes=330,
        incident_local="2026-10-09 13:15:09",
        boot_before_h=4.0,
        now_after_min=9,
        question=(
            "My laptop blue screened around 1:15 pm and restarted. It also froze for a bit right "
            "before."
        ),
        primary=C.STORAGE_IO,
        also_acceptable=(C.HARDWARE_ERROR,),
        tags=("storage", "bugcheck:0x7a", "crash-reboot", "laptop"),
        signals=(
            StorageReset(-2.1),
            DiskRetry(-1.6, count=2, spread_min=0.4),
            PagingError(-0.9),
            Crash(
                0.0,
                0x7A,
                (0xFFFFD88C4A0F5A10, 0xFFFFFFFFC000009D, 0x1A5B2C860, 0xFFFFF8061C2E1000),
                reboot_s=62,
            ),
        ),
    ),
    # ---- application_hang ---------------------------------------------------
    # The game hangs and is killed; GPU healthy, no display or kernel events.
    CaseSpec(
        id="syn-004",
        machine=DESK_NV_3070,
        utc_offset_minutes=-420,
        incident_local="2026-10-03 21:42:40",
        boot_before_h=2.8,
        now_after_min=7,
        question=(
            "My game froze around 9:42, the picture was stuck and the sound kept looping, and I "
            "had to close it with Task Manager."
        ),
        primary=C.APPLICATION_HANG,
        tags=("app-hang", "frametimes", "red-herring"),
        signals=(
            Noise(-4.0, "whea17"),
            AppHang(0.0, STARFORGE),
        ),
        sensors=(
            SensorSeries(
                -30,
                -0.2,
                gpu_temp=(67, 69),
                gpu_clock=(1905, 1890),
                gpu_power=(201, 214),
                gpu_fan=(1500, 1560),
            ),
        ),
        frametimes=Frametimes("hang", start=-50 / 60, duration_s=10, base_ms=9.0, event_s=6.0),
    ),
    # Explorer hangs; a recovered TDR six hours earlier is the decoy.
    CaseSpec(
        id="syn-011",
        machine=DESK_AMD_6800,
        utc_offset_minutes=-240,
        incident_local="2026-10-20 15:05:47",
        boot_before_h=8.0,
        now_after_min=22,
        question=(
            "Around 3 pm my taskbar and desktop froze and nothing responded for a while, then "
            "everything came back. Is it my graphics card again?"
        ),
        primary=C.APPLICATION_HANG,
        tags=("app-hang", "explorer", "red-herring"),
        signals=(Tdr(-371.0, live_dump=0x117), AppHang(0.0, EXPLORER)),
    ),
    # Repeated crash in the game's own module; WHEA 17 corrected PCIe errors are noise.
    CaseSpec(
        id="syn-009",
        machine=DESK_AMD_7900,
        utc_offset_minutes=120,
        incident_local="2026-10-02 23:20:06",
        boot_before_h=3.4,
        now_after_min=10,
        question=(
            "My game keeps crashing to desktop, the last one was around 11:20 pm. Event Viewer is "
            "full of WHEA warnings, is my PC failing?"
        ),
        primary=C.APPLICATION_HANG,
        tags=("app-crash", "red-herring", "whea17"),
        signals=(
            WheaPcie(-120, count=9, spread_min=115),
            AppCrash(
                -64.0,
                STARFORGE,
                "StarforgeEngine.dll",
                "1.14.2.0",
                "66f1a29c",
                "0xc0000005",
                "0x00000000013a2f47",
                r"D:\SteamLibrary\steamapps\common\Starforge Online\bin\StarforgeEngine.dll",
            ),
            WheaPcie(-3.0, count=2, spread_min=2.0),
            AppCrash(
                0.0,
                STARFORGE,
                "StarforgeEngine.dll",
                "1.14.2.0",
                "66f1a29c",
                "0xc0000005",
                "0x00000000013a2f47",
                r"D:\SteamLibrary\steamapps\common\Starforge Online\bin\StarforgeEngine.dll",
            ),
        ),
    ),
    # ---- insufficient_evidence ---------------------------------------------
    # A short black screen with nothing logged (monitor, cable or input switching).
    CaseSpec(
        id="syn-005",
        machine=DESK_NV_4070TI,
        utc_offset_minutes=-300,
        incident_local="2026-10-17 21:42:15",
        boot_before_h=4.2,
        now_after_min=20,
        question=(
            "My screen went black around 9:42 tonight for maybe five seconds and then came back "
            "by itself. What was it?"
        ),
        primary=C.INSUFFICIENT_EVIDENCE,
        tags=("black-screen", "no-signal", "red-herring"),
        signals=(Noise(-3.5, "app_crash"),),
    ),
    # The System log was cleared this morning; yesterday's crash left no usable trace.
    CaseSpec(
        id="syn-022",
        machine=DESK_NV_3070,
        utc_offset_minutes=-240,
        incident_local="2026-10-12 20:02:41",
        boot_before_h=3.0,
        now_after_min=880,
        question="Can you tell me why my PC crashed and restarted yesterday evening around 8?",
        primary=C.INSUFFICIENT_EVIDENCE,
        tags=("log-cleared", "crash-reboot"),
        signals=(
            Crash(
                0.0,
                0x116,
                (0xFFFFB10F2D8E0010, 0xFFFFF8027C6A1E40, 0xFFFFFFFFC000009A, 0x4),
                reboot_s=51,
            ),
            LogCleared(852.0),
        ),
    ),
    # One frame-time hitch; everything near it is routine noise.
    CaseSpec(
        id="syn-019",
        machine=DESK_AMD_6800,
        utc_offset_minutes=-240,
        incident_local="2026-10-22 20:00:12",
        boot_before_h=2.0,
        now_after_min=14,
        question=(
            "The game had one big stutter around 8 pm, just once. Is anything wrong with my PC?"
        ),
        primary=C.INSUFFICIENT_EVIDENCE,
        tags=("stutter", "red-herring", "frametimes"),
        signals=(Noise(-1.2, "app_crash"), Noise(0.3, "dcom"), Noise(0.6, "kgeneral16")),
        sensors=(
            SensorSeries(
                -25,
                3,
                gpu_temp=(68, 70),
                gpu_clock=(2300, 2290),
                gpu_power=(240, 252),
                gpu_fan=(1450, 1500),
            ),
        ),
        frametimes=Frametimes("hitch", start=-5 / 60, duration_s=10, base_ms=7.0, event_s=5.0),
    ),
)

SPECS: tuple[CaseSpec, ...] = tuple(sorted(_SPECS, key=lambda spec: spec.id))
