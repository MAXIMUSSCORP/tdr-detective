from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pc_diag_mcp.models import EventLevel, EventRecord, GpuVendor
from pc_diag_mcp.signatures import (
    SIGNATURES,
    decode_kernel_power_bugcheck,
    format_bugcheck,
    match_event,
    parse_live_kernel_event_code,
    parse_wer_bugcheck,
    vendor_for_driver,
)

T = datetime(2026, 10, 6, 5, 53, tzinfo=UTC)


def ev(
    provider: str,
    event_id: int,
    *,
    log: str = "System",
    level: EventLevel = EventLevel.ERROR,
    message: str = "",
    data: dict[str, str] | None = None,
) -> EventRecord:
    return EventRecord(
        record_id=1,
        time=T,
        log=log,
        provider=provider,
        event_id=event_id,
        level=level,
        message=message,
        data=data or {},
    )


def test_kernel_power_bugcheck_is_decimal() -> None:
    assert decode_kernel_power_bugcheck({"BugcheckCode": "159"}) == 0x9F
    assert decode_kernel_power_bugcheck({"BugcheckCode": "278"}) == 0x116
    assert decode_kernel_power_bugcheck({"BugcheckCode": "0"}) == 0
    assert decode_kernel_power_bugcheck({}) is None
    assert decode_kernel_power_bugcheck({"BugcheckCode": "zz"}) is None
    assert format_bugcheck(159) == "0x9F"


def test_kernel_power_41_with_bugcheck_names_the_code() -> None:
    hit = match_event(
        ev(
            "Microsoft-Windows-Kernel-Power",
            41,
            level=EventLevel.CRITICAL,
            data={"BugcheckCode": "159", "SleepInProgress": "1", "PowerButtonTimestamp": "0"},
        )
    )
    assert hit is not None
    assert hit.signature_id == "kernel_power_41"
    assert hit.severity == "critical"
    assert hit.code == "0x9F"
    assert "DRIVER_POWER_STATE_FAILURE" in hit.detail
    assert "SleepInProgress=1" in hit.detail


def test_kernel_power_41_without_bugcheck_and_power_button() -> None:
    zero = match_event(
        ev("Microsoft-Windows-Kernel-Power", 41, data={"BugcheckCode": "0", "SleepInProgress": "0"})
    )
    assert zero is not None and zero.code is None
    assert "no bugcheck" in zero.detail.lower()
    held = match_event(
        ev(
            "Microsoft-Windows-Kernel-Power",
            41,
            data={"BugcheckCode": "0", "PowerButtonTimestamp": "133000000000000000"},
        )
    )
    assert held is not None and "power button" in held.detail.lower()


def test_system_wer_bugcheck_from_data_and_message() -> None:
    from_data = ev(
        "Microsoft-Windows-WER-SystemErrorReporting",
        1001,
        data={"param1": "0x0000009f (0x0000000000000004, 0x0000000000000258)"},
    )
    assert parse_wer_bugcheck(from_data) == 0x9F
    from_message = ev(
        "Microsoft-Windows-WER-SystemErrorReporting",
        1001,
        message="The computer has rebooted from a bugcheck. The bugcheck was: 0x00000116 (0x1).",
    )
    assert parse_wer_bugcheck(from_message) == 0x116
    hit = match_event(from_message)
    assert hit is not None and hit.severity == "critical" and hit.code == "0x116"
    assert "VIDEO_TDR_FAILURE" in hit.detail
    assert parse_wer_bugcheck(ev("Microsoft-Windows-WER-SystemErrorReporting", 1001)) is None


def test_system_bugcheck_0x9f_parameter_one() -> None:
    hit = match_event(
        ev(
            "Microsoft-Windows-WER-SystemErrorReporting",
            1001,
            data={"param1": "0x0000009f (0x0000000000000004, 0x0000000000000258, 0x1, 0x2)"},
        )
    )
    assert hit is not None and hit.code == "0x9F"
    assert "Parameter 1 = 0x4" in hit.detail


def test_live_kernel_event_p1_from_message_and_data() -> None:
    message = (
        "Fault bucket , type 0 Event Name: LiveKernelEvent Response: Not available Cab Id: 0 "
        "Problem signature: P1: 1b0 P2: 2 P3: ffffffffc000009a"
    )
    from_message = ev("Windows Error Reporting", 1001, log="Application", message=message)
    assert parse_live_kernel_event_code(from_message) == 0x1B0
    from_data = ev(
        "Windows Error Reporting",
        1001,
        log="Application",
        data={"EventName": "LiveKernelEvent", "P1": "141"},
    )
    assert parse_live_kernel_event_code(from_data) == 0x141
    camera = ev(
        "Windows Error Reporting",
        1001,
        log="Application",
        data={"EventName": "CameraStartupFailureEvent", "P1": "unknown"},
        message="Event Name: CameraStartupFailureEvent P1: unknown",
    )
    assert parse_live_kernel_event_code(camera) is None
    assert match_event(camera) is None


@pytest.mark.parametrize(
    ("p1", "severity", "name"),
    [
        ("117", "high", "VIDEO_TDR_TIMEOUT_DETECTED"),
        ("141", "high", "VIDEO_ENGINE_TIMEOUT_DETECTED"),
        ("1b0", "high", "VIDEO_MINIPORT_FAILED_LIVEDUMP"),
        ("1a1", "medium", "WIN32K_CALLOUT_WATCHDOG_LIVEDUMP"),
        ("abc", "low", "unrecognised"),
    ],
)
def test_live_kernel_event_codes(p1: str, severity: str, name: str) -> None:
    hit = match_event(
        ev(
            "Windows Error Reporting",
            1001,
            log="Application",
            level=EventLevel.INFORMATION,
            data={"EventName": "LiveKernelEvent", "P1": p1},
        )
    )
    assert hit is not None
    assert hit.signature_id == "wer_live_kernel_event"
    assert hit.severity == severity
    assert name in hit.detail
    assert "no reboot" in hit.meaning.lower()


def test_live_kernel_tdr_codes_never_imply_a_crash() -> None:
    hit = match_event(
        ev(
            "Windows Error Reporting",
            1001,
            log="Application",
            data={"EventName": "LiveKernelEvent", "P1": "117"},
        )
    )
    assert hit is not None and "recovered" in hit.detail.lower()


def test_display_4101_implicates_driver_vendor() -> None:
    nvidia = match_event(ev("Display", 4101, level=EventLevel.WARNING, data={"param1": "nvlddmkm"}))
    assert nvidia is not None
    assert nvidia.driver == "nvlddmkm" and nvidia.vendor is GpuVendor.NVIDIA
    intel = match_event(
        ev(
            "Display",
            4101,
            message="Display driver igfxn stopped responding and has successfully recovered.",
        )
    )
    assert intel is not None and intel.driver == "igfxn" and intel.vendor is GpuVendor.INTEL


def test_display_hdr_notifications_are_noise() -> None:
    assert match_event(ev("Display", 4123, level=EventLevel.INFORMATION)) is None
    assert match_event(ev("Display", 4127, level=EventLevel.INFORMATION)) is None


def test_nvlddmkm_ids() -> None:
    for event_id in (13, 14, 153):
        hit = match_event(ev("nvlddmkm", event_id))
        assert hit is not None and hit.vendor is GpuVendor.NVIDIA
    fourteen = match_event(ev("nvlddmkm", 14))
    assert fourteen is not None and "undocumented" in fourteen.meaning.lower()
    assert match_event(ev("nvlddmkm", 99)) is None


def test_amd_and_intel_need_warning_or_worse() -> None:
    assert match_event(ev("amdkmdag", 4101, level=EventLevel.INFORMATION)) is None
    amd = match_event(ev("amdkmdag", 4101, level=EventLevel.WARNING))
    assert amd is not None and amd.vendor is GpuVendor.AMD
    intel = match_event(ev("igfxn", 5, level=EventLevel.ERROR))
    assert intel is not None and intel.vendor is GpuVendor.INTEL
    assert match_event(ev("igfx", 5, level=EventLevel.INFORMATION)) is None


@pytest.mark.parametrize(
    ("event_id", "severity"),
    [
        (1, "high"),
        (17, "medium"),
        (18, "critical"),
        (19, "medium"),
        (46, "critical"),
        (47, "medium"),
    ],
)
def test_whea_ids(event_id: int, severity: str) -> None:
    hit = match_event(
        ev(
            "Microsoft-Windows-WHEA-Logger",
            event_id,
            data={"PrimaryDeviceName": "PCI\\VEN_10DE&DEV_2F58&SUBSYS_15F41043&REV_A1"},
        )
    )
    assert hit is not None and hit.severity == severity
    assert hit.device == "PCI\\VEN_10DE&DEV_2F58&SUBSYS_15F41043&REV_A1"
    # A hardware error is not a driver implication.
    assert hit.vendor is None
    assert match_event(ev("Microsoft-Windows-WHEA-Logger", 2)) is None


def test_vendor_for_driver_prefixes() -> None:
    assert vendor_for_driver("nvlddmkm") is GpuVendor.NVIDIA
    assert vendor_for_driver("amdkmdap") is GpuVendor.AMD
    assert vendor_for_driver("atikmpag") is GpuVendor.AMD
    assert vendor_for_driver("igfxnd") is GpuVendor.INTEL
    assert vendor_for_driver("BasicDisplay") is None
    assert vendor_for_driver(None) is None


def test_table_is_well_formed() -> None:
    ids = [s.id for s in SIGNATURES]
    assert len(ids) == len(set(ids))
    for signature in SIGNATURES:
        assert signature.meaning and signature.severity in {"critical", "high", "medium", "low"}
