"""Crash, TDR and hardware-error signatures.

Meanings are deliberately conservative. Sources (see the team's verification notes):

* Display 4101 message template, Kernel-Power 41 field semantics, bugcheck 0x116/0x117/0x141
  and the TDR limits come from Microsoft documentation (confirmed).
* WER 1001 split (System = real bugcheck, Application LiveKernelEvent = live dump, no reboot)
  is from Microsoft Q&A and the provider manifest (likely).
* nvlddmkm 13/153 and the WHEA-Logger ID labels are community/manifest-derived (likely);
  nvlddmkm 14 has no documented meaning and is labelled generically.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

from pc_diag_mcp.models import EventLevel, EventRecord, GpuVendor, UtcDatetime

Severity = Literal["critical", "high", "medium", "low"]
SEVERITY_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2, "critical": 3}

_LEVEL_RANK = {
    EventLevel.VERBOSE: 0,
    EventLevel.INFORMATION: 1,
    EventLevel.WARNING: 2,
    EventLevel.ERROR: 3,
    EventLevel.CRITICAL: 4,
}

# Names from Microsoft's bug check code reference. Unknown codes are shown as hex only.
BUGCHECK_NAMES: dict[int, str] = {
    0x0A: "IRQL_NOT_LESS_OR_EQUAL",
    0x1A: "MEMORY_MANAGEMENT",
    0x1E: "KMODE_EXCEPTION_NOT_HANDLED",
    0x3B: "SYSTEM_SERVICE_EXCEPTION",
    0x50: "PAGE_FAULT_IN_NONPAGED_AREA",
    0x7A: "KERNEL_DATA_INPAGE_ERROR",
    0x7E: "SYSTEM_THREAD_EXCEPTION_NOT_HANDLED",
    0x9F: "DRIVER_POWER_STATE_FAILURE",
    0xA0: "INTERNAL_POWER_ERROR",
    0xD1: "DRIVER_IRQL_NOT_LESS_OR_EQUAL",
    0xEA: "THREAD_STUCK_IN_DEVICE_DRIVER",
    0xEF: "CRITICAL_PROCESS_DIED",
    0x101: "CLOCK_WATCHDOG_TIMEOUT",
    0x10E: "VIDEO_MEMORY_MANAGEMENT_INTERNAL",
    0x113: "VIDEO_DXGKRNL_FATAL_ERROR",
    0x116: "VIDEO_TDR_FAILURE",
    0x119: "VIDEO_SCHEDULER_INTERNAL_ERROR",
    0x124: "WHEA_UNCORRECTABLE_ERROR",
    0x133: "DPC_WATCHDOG_VIOLATION",
    0x139: "KERNEL_SECURITY_CHECK_FAILURE",
}

_BUGCHECK_NOTES: dict[int, str] = {
    0x116: (
        "an attempt to reset the display driver and recover from a timeout failed, "
        "or more than 5 TDRs occurred within 60 s"
    ),
}

_BUGCHECK_9F_PARAM1: dict[int, str] = {
    0x3: "a device object blocked an IRP for too long",
    0x4: "the power state transition timed out waiting to synchronize with the PnP subsystem",
}


@dataclass(frozen=True)
class LiveKernelCode:
    name: str
    meaning: str
    severity: Severity


# Live dumps never reboot the machine: 0x117/0x141 "can never be used for a real bug check".
LIVE_KERNEL_CODES: dict[int, LiveKernelCode] = {
    0x117: LiveKernelCode(
        "VIDEO_TDR_TIMEOUT_DETECTED",
        "the display driver timed out and was recovered (TDR)",
        "high",
    ),
    0x141: LiveKernelCode(
        "VIDEO_ENGINE_TIMEOUT_DETECTED",
        "a GPU engine timed out and was recovered",
        "high",
    ),
    0x142: LiveKernelCode(
        "VIDEO_TDR_APPLICATION_BLOCKED",
        "an application was blocked from using the GPU after a timeout",
        "medium",
    ),
    0x1B0: LiveKernelCode(
        "VIDEO_MINIPORT_FAILED_LIVEDUMP",
        "the display miniport driver reported a failure and a live dump was taken",
        "high",
    ),
    0x193: LiveKernelCode(
        "VIDEO_DXGKRNL_LIVEDUMP",
        "the DirectX graphics kernel requested a live dump",
        "medium",
    ),
    0x1A1: LiveKernelCode(
        "WIN32K_CALLOUT_WATCHDOG_LIVEDUMP",
        "a call into win32k did not return in time",
        "medium",
    ),
    0x144: LiveKernelCode(
        "BUGCODE_USB3_DRIVER",
        "the USB 3 driver stack reported an error",
        "medium",
    ),
}

_DRIVER_VENDORS: tuple[tuple[str, GpuVendor], ...] = (
    ("nvlddmkm", GpuVendor.NVIDIA),
    ("amdkmd", GpuVendor.AMD),
    ("atikmd", GpuVendor.AMD),
    ("atikmp", GpuVendor.AMD),
    ("igfx", GpuVendor.INTEL),
    ("igdkmd", GpuVendor.INTEL),
)


def vendor_for_driver(driver: str | None) -> GpuVendor | None:
    if not driver:
        return None
    lowered = driver.lower()
    for prefix, vendor in _DRIVER_VENDORS:
        if lowered.startswith(prefix):
            return vendor
    return None


def format_bugcheck(code: int) -> str:
    return f"0x{code:X}"


def describe_bugcheck(code: int) -> str:
    name = BUGCHECK_NAMES.get(code)
    text = f"{format_bugcheck(code)} ({name})" if name else format_bugcheck(code)
    note = _BUGCHECK_NOTES.get(code)
    return f"{text}: {note}" if note else text


def decode_kernel_power_bugcheck(data: dict[str, str]) -> int | None:
    """Kernel-Power 41 stores ``BugcheckCode`` as a *decimal* integer (159 == 0x9F)."""
    raw = data.get("BugcheckCode", "").strip()
    try:
        return int(raw, 10)
    except ValueError:
        return None


_HEX = re.compile(r"0x([0-9a-fA-F]+)")
_BUGCHECK_IN_MESSAGE = re.compile(r"bugcheck was:\s*0x([0-9a-fA-F]+)", re.IGNORECASE)
_PARAMS_IN_DATA = re.compile(r"\(\s*0x([0-9a-fA-F]+)")


def parse_wer_bugcheck(record: EventRecord) -> int | None:
    """Hex bugcheck code from a System WER-SystemErrorReporting 1001 event."""
    param1 = record.data.get("param1", "")
    match = _HEX.match(param1.strip())
    if match is None:
        match = _BUGCHECK_IN_MESSAGE.search(record.message)
    return int(match.group(1), 16) if match else None


def _bugcheck_parameter1(record: EventRecord) -> int | None:
    raw = record.data.get("BugcheckParameter1")
    if raw:
        match = _HEX.match(raw.strip())
        return int(match.group(1), 16) if match else None
    match = _PARAMS_IN_DATA.search(record.data.get("param1", ""))
    return int(match.group(1), 16) if match else None


_LIVE_KERNEL_NAME = re.compile(r"Event Name:\s*LiveKernelEvent\b", re.IGNORECASE)
_P1_IN_MESSAGE = re.compile(r"\bP1:\s*([0-9a-fA-F]+)\b")


def parse_live_kernel_event_code(record: EventRecord) -> int | None:
    """P1 (hex) of an Application-log WER 1001 ``LiveKernelEvent``; None for other WER reports."""
    if record.data.get("EventName"):
        if record.data["EventName"] != "LiveKernelEvent":
            return None
        raw: str | None = record.data.get("P1", "").strip()
    elif _LIVE_KERNEL_NAME.search(record.message):
        match = _P1_IN_MESSAGE.search(record.message)
        raw = match.group(1) if match else None
    else:
        return None
    try:
        return int(raw, 16) if raw else None
    except ValueError:
        return -1  # a LiveKernelEvent whose P1 is not hex: still a signal, code unknown


class SignatureHit(BaseModel):
    """One event recognised by a signature."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    signature_id: str
    record_id: int
    time: UtcDatetime
    log: str
    provider: str
    event_id: int
    meaning: str
    severity: Severity
    detail: str
    code: str | None = None
    driver: str | None = None
    vendor: GpuVendor | None = None
    device: str | None = None


@dataclass(frozen=True)
class _Decoded:
    detail: str
    code: str | None = None
    severity: Severity | None = None
    driver: str | None = None
    device: str | None = None


Decoder = Callable[[EventRecord], _Decoded | None]


@dataclass(frozen=True)
class Signature:
    id: str
    log: str
    providers: tuple[str, ...]
    meaning: str
    severity: Severity
    event_ids: frozenset[int] | None = None
    min_level: EventLevel | None = None
    provider_prefix: bool = False
    vendor: GpuVendor | None = None
    decode: Decoder | None = None

    def matches(self, record: EventRecord) -> bool:
        if record.log.lower() != self.log.lower():
            return False
        provider = record.provider.lower()
        names = [p.lower() for p in self.providers]
        if self.provider_prefix:
            if not any(provider.startswith(p) for p in names):
                return False
        elif provider not in names:
            return False
        if self.event_ids is not None and record.event_id not in self.event_ids:
            return False
        return self.min_level is None or _LEVEL_RANK[record.level] >= _LEVEL_RANK[self.min_level]


_DISPLAY_DRIVER = re.compile(r"Display driver (\S+) stopped responding", re.IGNORECASE)


def _decode_display_tdr(record: EventRecord) -> _Decoded:
    driver = record.data.get("param1", "").strip() or None
    if driver is None:
        match = _DISPLAY_DRIVER.search(record.message)
        driver = match.group(1) if match else None
    return _Decoded(detail=f"driver {driver or 'unknown'} recovered from a timeout", driver=driver)


def _decode_kernel_power(record: EventRecord) -> _Decoded:
    code = decode_kernel_power_bugcheck(record.data)
    parts: list[str] = []
    if code:
        parts.append(f"bugcheck {describe_bugcheck(code)} was recorded")
        if code == 0x9F:
            parts.extend(_param1_note(record))
    else:
        parts.append(
            "no bugcheck recorded: an all-zero BugcheckCode suggests power or hardware loss, "
            "a hard hang, or a failed dump write"
        )
    if record.data.get("PowerButtonTimestamp", "0").strip() not in {"", "0"}:
        parts.append("the power button was held (PowerButtonTimestamp is non-zero)")
    sleep = record.data.get("SleepInProgress", "0").strip()
    if sleep not in {"", "0", "false"}:
        parts.append(f"a sleep transition was in progress (SleepInProgress={sleep})")
    return _Decoded(detail="; ".join(parts), code=format_bugcheck(code) if code else None)


def _param1_note(record: EventRecord) -> list[str]:
    param1 = _bugcheck_parameter1(record)
    if param1 is None:
        return []
    meaning = _BUGCHECK_9F_PARAM1.get(param1)
    suffix = f": {meaning}" if meaning else ""
    return [f"Parameter 1 = {format_bugcheck(param1)}{suffix}"]


def _decode_system_bugcheck(record: EventRecord) -> _Decoded:
    code = parse_wer_bugcheck(record)
    if code is None:
        return _Decoded(detail="bugcheck code not present in the event")
    parts = [f"bugcheck {describe_bugcheck(code)}"]
    if code == 0x9F:
        parts.extend(_param1_note(record))
    return _Decoded(detail="; ".join(parts), code=format_bugcheck(code))


def _decode_live_kernel(record: EventRecord) -> _Decoded | None:
    code = parse_live_kernel_event_code(record)
    if code is None:
        return None
    known = LIVE_KERNEL_CODES.get(code)
    if known is None:
        shown = format_bugcheck(code) if code >= 0 else "unknown"
        return _Decoded(
            detail=f"unrecognised live kernel event code {shown}",
            code=shown if code >= 0 else None,
            severity="low",
        )
    return _Decoded(
        detail=f"{format_bugcheck(code)} {known.name}: {known.meaning}",
        code=format_bugcheck(code),
        severity=known.severity,
    )


def _decode_whea(record: EventRecord) -> _Decoded:
    device = record.data.get("PrimaryDeviceName", "").strip() or None
    source = record.data.get("ErrorSource", "").strip()
    detail = f"error source {source}" if source else "hardware error record"
    if device:
        detail += f", device {device}"
    return _Decoded(detail=detail, device=device)


def _decode_nvidia(record: EventRecord) -> _Decoded:
    return _Decoded(detail=record.message or "no rendered message", driver="nvlddmkm")


def _decode_vendor_driver(record: EventRecord) -> _Decoded:
    return _Decoded(
        detail=f"{record.level.value} event {record.event_id}: {record.message}".strip(),
        driver=record.provider,
    )


_WHEA = "Microsoft-Windows-WHEA-Logger"
_INTEL_PROVIDERS = ("igfx",)

SIGNATURES: tuple[Signature, ...] = (
    Signature(
        id="display_tdr_4101",
        log="System",
        providers=("Display",),
        event_ids=frozenset({4101}),
        meaning="Display driver stopped responding and has successfully recovered (TDR).",
        severity="high",
        decode=_decode_display_tdr,
    ),
    Signature(
        id="nvlddmkm_13",
        log="System",
        providers=("nvlddmkm",),
        event_ids=frozenset({13}),
        meaning="NVIDIA kernel driver reported a graphics exception.",
        severity="high",
        vendor=GpuVendor.NVIDIA,
        decode=_decode_nvidia,
    ),
    Signature(
        id="nvlddmkm_14",
        log="System",
        providers=("nvlddmkm",),
        event_ids=frozenset({14}),
        meaning="NVIDIA kernel driver error event (meaning undocumented).",
        severity="medium",
        vendor=GpuVendor.NVIDIA,
        decode=_decode_nvidia,
    ),
    Signature(
        id="nvlddmkm_153",
        log="System",
        providers=("nvlddmkm",),
        event_ids=frozenset({153}),
        meaning="NVIDIA kernel driver reported a GPU reset or TDR.",
        severity="high",
        vendor=GpuVendor.NVIDIA,
        decode=_decode_nvidia,
    ),
    Signature(
        id="amd_kmd_warning",
        log="System",
        providers=("amdkmdag", "amdkmdap"),
        min_level=EventLevel.WARNING,
        meaning="AMD kernel-mode display driver logged a warning or error.",
        severity="medium",
        vendor=GpuVendor.AMD,
        decode=_decode_vendor_driver,
    ),
    Signature(
        id="intel_igfx_warning",
        log="System",
        providers=_INTEL_PROVIDERS,
        provider_prefix=True,
        min_level=EventLevel.WARNING,
        meaning="Intel graphics driver logged a warning or error.",
        severity="medium",
        vendor=GpuVendor.INTEL,
        decode=_decode_vendor_driver,
    ),
    Signature(
        id="wer_live_kernel_event",
        log="Application",
        providers=("Windows Error Reporting",),
        event_ids=frozenset({1001}),
        meaning="Live kernel report (LiveKernelEvent): a live dump was taken; no reboot.",
        severity="medium",
        decode=_decode_live_kernel,
    ),
    Signature(
        id="wer_system_bugcheck",
        log="System",
        providers=("Microsoft-Windows-WER-SystemErrorReporting",),
        event_ids=frozenset({1001}),
        meaning="The computer rebooted from a bugcheck (blue screen); a dump was saved.",
        severity="critical",
        decode=_decode_system_bugcheck,
    ),
    Signature(
        id="kernel_power_41",
        log="System",
        providers=("Microsoft-Windows-Kernel-Power",),
        event_ids=frozenset({41}),
        meaning="The system rebooted without cleanly shutting down first.",
        severity="critical",
        decode=_decode_kernel_power,
    ),
    Signature(
        id="whea_1",
        log="System",
        providers=(_WHEA,),
        event_ids=frozenset({1}),
        meaning="WHEA raw hardware error record (the ID alone does not give the severity).",
        severity="high",
        decode=_decode_whea,
    ),
    Signature(
        id="whea_17",
        log="System",
        providers=(_WHEA,),
        event_ids=frozenset({17}),
        meaning="Corrected PCI Express (AER) hardware error.",
        severity="medium",
        decode=_decode_whea,
    ),
    Signature(
        id="whea_18",
        log="System",
        providers=(_WHEA,),
        event_ids=frozenset({18}),
        meaning="Fatal (uncorrected) machine check error.",
        severity="critical",
        decode=_decode_whea,
    ),
    Signature(
        id="whea_19",
        log="System",
        providers=(_WHEA,),
        event_ids=frozenset({19}),
        meaning="Corrected machine check error.",
        severity="medium",
        decode=_decode_whea,
    ),
    Signature(
        id="whea_46",
        log="System",
        providers=(_WHEA,),
        event_ids=frozenset({46}),
        meaning="Uncorrected memory error.",
        severity="critical",
        decode=_decode_whea,
    ),
    Signature(
        id="whea_47",
        log="System",
        providers=(_WHEA,),
        event_ids=frozenset({47}),
        meaning="Corrected memory error.",
        severity="medium",
        decode=_decode_whea,
    ),
)

# Intel's provider names are matched by prefix, but the Event Log can only be asked for exact
# names, so these are the variants requested when scanning.
INTEL_QUERY_PROVIDERS: tuple[str, ...] = ("igfx", "igfxn", "igfxnd")


def match_event(record: EventRecord) -> SignatureHit | None:
    for signature in SIGNATURES:
        if not signature.matches(record):
            continue
        decoded = signature.decode(record) if signature.decode else _Decoded(detail="")
        if decoded is None:
            continue
        driver = decoded.driver
        return SignatureHit(
            signature_id=signature.id,
            record_id=record.record_id,
            time=record.time,
            log=record.log,
            provider=record.provider,
            event_id=record.event_id,
            meaning=signature.meaning,
            severity=decoded.severity or signature.severity,
            detail=decoded.detail,
            code=decoded.code,
            driver=driver,
            vendor=signature.vendor or vendor_for_driver(driver),
            device=decoded.device,
        )
    return None
