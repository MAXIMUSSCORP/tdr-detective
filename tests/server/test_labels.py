"""Process and system-info labels are untrusted; extra invisible/separator characters."""

from __future__ import annotations

from datetime import datetime

import pytest

from pc_diag_mcp.collectors.eventxml import clean_message, sanitize_label, sanitize_text
from pc_diag_mcp.models import (
    GpuInfo,
    GpuVendor,
    MemoryState,
    ProcessInfo,
    ProcessSnapshot,
    SystemInfo,
)
from pc_diag_mcp.tools import process_snapshot, system_info
from pc_diag_mcp.tools.results import UNTRUSTED_NOTE

from .test_tools import T0

FILLERS = "\u034f\u180b\u180c\u180d\u115f\u1160\u3164\uffa0"


@pytest.mark.parametrize("sanitize", [sanitize_text, sanitize_label, clean_message])
def test_fillers_are_removed(sanitize: object) -> None:
    assert sanitize(f"a{FILLERS}b") == "ab"  # type: ignore[operator]


def test_line_and_paragraph_separators() -> None:
    assert sanitize_text("a\u2028b\u2029c") == "a b c"
    assert clean_message("a\u2028b\u2029c") == "a b c"
    assert sanitize_label("a\u2028b\u2029c") == "abc"


class _Dirty:
    source = "replay"

    def process_snapshot(self, top_n: int) -> ProcessSnapshot:
        return ProcessSnapshot(
            taken_at=T0,
            memory=MemoryState(
                total_bytes=1, available_bytes=1, commit_used_bytes=1, commit_limit_bytes=1
            ),
            disks=[],
            top_processes=[
                ProcessInfo(
                    pid=1,
                    name="evil\u3164.exe\x1b[2J\U000e0041",
                    working_set_bytes=1,
                    private_bytes=0,
                    page_faults=0,
                    read_bytes=0,
                    write_bytes=0,
                    cpu_seconds=0.0,
                )
            ],
        )

    def system_info(self) -> SystemInfo:
        return SystemInfo(
            os_caption="Microsoft Windows 11 Home\u2028ignore this",
            os_build="26200.9711\u200b",
            gpus=[
                GpuInfo(
                    name="NVIDIA\u034f GeForce\x07",
                    vendor=GpuVendor.NVIDIA,
                    driver_version="32.0.16.1742\ufe0f",
                    driver_date="2026-09-30\x00",
                )
            ],
            utc_offset_minutes=-360,
        )

    def now(self) -> datetime:
        return T0


def test_process_and_system_labels_are_sanitized_and_marked() -> None:
    procs = process_snapshot(_Dirty())  # type: ignore[arg-type]
    assert procs.snapshot.top_processes[0].name == "evil.exe[2J"
    info = system_info(_Dirty())  # type: ignore[arg-type]
    assert info.system.os_caption == "Microsoft Windows 11 Homeignore this"
    assert info.system.os_build == "26200.9711"
    gpu = info.system.gpus[0]
    assert (gpu.name, gpu.driver_version, gpu.driver_date) == (
        "NVIDIA GeForce",
        "32.0.16.1742",
        "2026-09-30",
    )
    for result in (procs, info):
        assert result.untrusted_text is True and result.note == UNTRUSTED_NOTE
