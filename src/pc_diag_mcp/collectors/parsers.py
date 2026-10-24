"""Pure parsers that turn raw collector output into the shared models."""

from __future__ import annotations

import csv
import io
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator, Mapping
from typing import Any, Literal

from defusedxml import ElementTree as SafeET  # type: ignore[import-untyped]
from defusedxml.common import DefusedXmlException  # type: ignore[import-untyped]

from pc_diag_mcp.collectors.base import CollectorError
from pc_diag_mcp.models import (
    DisplayDevice,
    DxDiagSummary,
    GpuVendor,
    ProcessInfo,
    SensorReading,
)

SensorKind = Literal["temperature", "clock", "load", "power", "fan", "voltage"]

# --------------------------------------------------------------------------- #
# Processes
# --------------------------------------------------------------------------- #


def _io_bytes(process: ProcessInfo) -> int:
    return process.read_bytes + process.write_bytes


PROCESS_SORT_KEYS = {
    "working_set": lambda p: p.working_set_bytes,
    "page_faults": lambda p: p.page_faults,
    "io": _io_bytes,
}


def top_process_union(processes: Iterable[ProcessInfo], top_n: int) -> list[ProcessInfo]:
    """The top ``top_n`` by each sort key, deduplicated, ordered by working set."""
    pool = list(processes)
    chosen: dict[int, ProcessInfo] = {}
    for key in PROCESS_SORT_KEYS.values():
        for process in sorted(pool, key=key, reverse=True)[:top_n]:
            chosen.setdefault(process.pid, process)
    return sorted(chosen.values(), key=lambda p: p.working_set_bytes, reverse=True)


# --------------------------------------------------------------------------- #
# Sensors
# --------------------------------------------------------------------------- #

NVIDIA_SMI_FIELDS: tuple[tuple[str, SensorKind, str], ...] = (
    ("temperature.gpu", "temperature", "°C"),
    ("utilization.gpu", "load", "%"),
    ("utilization.memory", "load", "%"),
    ("clocks.gr", "clock", "MHz"),
    ("clocks.mem", "clock", "MHz"),
    ("power.draw", "power", "W"),
    ("fan.speed", "fan", "%"),
)
NVIDIA_SMI_QUERY = "name," + ",".join(f for f, _, _ in NVIDIA_SMI_FIELDS)


_LABEL_UNSAFE = re.compile(r"[^\w .:/()%+-]")


def sensor_label(text: str) -> str:
    """Names reported by other programs (LHM, nvidia-smi) are untrusted: restrict and cap."""
    return _LABEL_UNSAFE.sub("_", text)[:64]


def _number(text: str) -> float | None:
    match = re.search(r"-?\d+(?:[.,]\d+)?", text)
    return float(match.group(0).replace(",", ".")) if match else None


def parse_nvidia_smi(stdout: str) -> list[SensorReading]:
    readings: list[SensorReading] = []
    for row in csv.reader(io.StringIO(stdout)):
        cells = [c.strip() for c in row]
        if len(cells) != len(NVIDIA_SMI_FIELDS) + 1:
            continue
        name, values = cells[0], cells[1:]
        for (field, kind, unit), raw in zip(NVIDIA_SMI_FIELDS, values, strict=True):
            if raw.startswith("[") or not raw:  # [N/A], [Not Supported]
                continue
            value = _number(raw)
            if value is not None:
                readings.append(
                    SensorReading(
                        hardware=sensor_label(name),
                        sensor=field,
                        kind=kind,
                        value=value,
                        unit=unit,
                        source="nvidia-smi",
                    )
                )
    return readings


def parse_thermal_zones(rows: Iterable[Mapping[str, Any]]) -> list[SensorReading]:
    readings: list[SensorReading] = []
    for row in rows:
        tenths = float(row.get("kelvin_tenths") or 0)
        kelvin = tenths / 10 if tenths > 0 else float(row.get("kelvin") or 0)
        if kelvin <= 0:
            continue
        readings.append(
            SensorReading(
                hardware="ACPI thermal zone",
                sensor=str(row.get("name") or "zone"),
                kind="temperature",
                value=round(kelvin - 273.15, 1),
                unit="°C",
                source="cim:ThermalZoneInformation",
            )
        )
    return readings


_ENGINE_NAME = re.compile(
    r"luid_(?P<luid>0x[0-9A-Fa-f]+_0x[0-9A-Fa-f]+)_phys_(?P<phys>\d+)_eng_(?P<eng>\d+)"
    r"_engtype_(?P<type>.*)$"
)


def aggregate_gpu_engines(rows: Iterable[Mapping[str, Any]]) -> list[SensorReading]:
    """Per-process engine rows -> one utilization per (adapter, engine type).

    Rows for the same physical engine are summed across processes; engines of the same type
    (e.g. several Copy engines) are reduced to the busiest one.
    """
    per_engine: dict[tuple[str, str, str, str], float] = {}
    for row in rows:
        match = _ENGINE_NAME.search(str(row.get("name") or ""))
        if match is None or not match.group("type").strip():
            continue
        key = (match["luid"], match["phys"], match["eng"], match["type"].strip())
        per_engine[key] = per_engine.get(key, 0.0) + float(row.get("utilization") or 0)
    per_type: dict[tuple[str, str], float] = {}
    for (luid, _phys, _eng, engine_type), value in per_engine.items():
        type_key = (luid, engine_type)
        per_type[type_key] = max(per_type.get(type_key, 0.0), value)
    return [
        SensorReading(
            hardware=f"GPU adapter luid {luid}",
            sensor=f"{engine_type} engine utilization",
            kind="load",
            value=min(value, 100.0),
            unit="%",
            source="cim:GPUEngine",
        )
        for (luid, engine_type), value in sorted(per_type.items())
    ]


_LHM_KINDS: dict[str, tuple[SensorKind, str]] = {
    "Temperature": ("temperature", "°C"),
    "Clock": ("clock", "MHz"),
    "Load": ("load", "%"),
    "Power": ("power", "W"),
    "Fan": ("fan", "RPM"),
    "Voltage": ("voltage", "V"),
}


def _walk_lhm(node: Mapping[str, Any], hardware: str) -> Iterator[SensorReading]:
    if node.get("HardwareId"):
        hardware = sensor_label(str(node.get("Text") or hardware))
    kind_unit = _LHM_KINDS.get(str(node.get("Type") or ""))
    if node.get("SensorId") and kind_unit is not None:
        value = _number(str(node.get("RawValue") or node.get("Value") or ""))
        if value is not None:
            yield SensorReading(
                hardware=hardware,
                sensor=sensor_label(str(node.get("Text") or node["SensorId"])),
                kind=kind_unit[0],
                value=value,
                unit=kind_unit[1],
                source="librehardwaremonitor:http",
            )
    for child in node.get("Children") or []:
        if isinstance(child, Mapping):
            yield from _walk_lhm(child, hardware)


def parse_lhm_data_json(document: Any) -> list[SensorReading]:
    """LibreHardwareMonitor ``/data.json``: a tree of nodes; sensors carry ``SensorId``/``Type``."""
    if not isinstance(document, Mapping):
        raise CollectorError("LibreHardwareMonitor data.json is not a JSON object")
    return list(_walk_lhm(document, "unknown"))


# --------------------------------------------------------------------------- #
# GPU vendors
# --------------------------------------------------------------------------- #

_PCI_VENDORS = {
    "10DE": GpuVendor.NVIDIA,
    "1002": GpuVendor.AMD,
    "1022": GpuVendor.AMD,
    "8086": GpuVendor.INTEL,
}


def gpu_vendor(pci_vendor_id: str | None, *names: str | None) -> GpuVendor:
    if pci_vendor_id and pci_vendor_id.upper() in _PCI_VENDORS:
        return _PCI_VENDORS[pci_vendor_id.upper()]
    text = " ".join(n for n in names if n).lower()
    if "nvidia" in text:
        return GpuVendor.NVIDIA
    if "amd" in text or "advanced micro devices" in text or "radeon" in text:
        return GpuVendor.AMD
    if "intel" in text:
        return GpuVendor.INTEL
    return GpuVendor.OTHER


# --------------------------------------------------------------------------- #
# dxdiag
# --------------------------------------------------------------------------- #

_NO_PROBLEMS = "no problems found."


def _text(element: ET.Element, tag: str) -> str:
    return (element.findtext(tag) or "").strip()


def _known(value: str) -> str | None:
    return None if value.lower() in {"", "unknown", "n/a"} else value


def parse_dxdiag_xml(xml: str) -> DxDiagSummary:
    """Only the fields we need are read; machine/user names in the report are ignored."""
    try:
        root = SafeET.fromstring(xml.lstrip("﻿"), forbid_dtd=True)
    except (ET.ParseError, DefusedXmlException, ValueError) as exc:
        raise CollectorError(f"could not parse dxdiag XML: {exc}") from exc
    system = root.find("SystemInformation")
    directx = _text(system, "DirectXVersion") if system is not None else ""

    devices: list[DisplayDevice] = []
    problems: list[str] = []
    for device in root.iter("DisplayDevice"):
        memory = (
            _number(_text(device, "DedicatedMemory"))
            if _known(_text(device, "DedicatedMemory"))
            else None
        )
        levels = _known(_text(device, "FeatureLevels"))
        devices.append(
            DisplayDevice(
                name=_text(device, "CardName"),
                manufacturer=_text(device, "Manufacturer"),
                driver_version=_text(device, "DriverVersion"),
                driver_date=_known(_text(device, "DriverDate")),
                dedicated_memory_mb=int(memory) if memory is not None else None,
                feature_levels=[lvl.strip() for lvl in levels.split(",")] if levels else [],
            )
        )
        note = _text(device, "Notes")
        if note and note.lower() != _NO_PROBLEMS:
            problems.append(f"{devices[-1].name}: {note}")
    notes = root.find("DxDiagNotes")
    if notes is not None:
        for entry in notes:
            text = (entry.text or "").strip()
            if text and text.lower() != _NO_PROBLEMS:
                problems.append(f"{entry.tag}: {text}")
    return DxDiagSummary(
        directx_version=directx or "unknown",
        display_devices=devices,
        problems=list(dict.fromkeys(problems)),
    )
