"""``describe_collection``: show exactly what a tool would run before it runs."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from pc_diag_mcp.collectors.powershell import (
    COMMANDS_BY_METHOD,
    POWERSHELL_FLAGS,
    PS_DIR,
    SCRIPTS_BY_METHOD,
)
from pc_diag_mcp.tools.params import (
    DescribeParams,
    DriverInventoryParams,
    IncidentParams,
    NoParams,
    ProcessSnapshotParams,
    QueryEventsParams,
    SensorSnapshotParams,
)

_TOOLS: dict[str, tuple[type[BaseModel], tuple[str, ...], str]] = {
    "query_events": (
        QueryEventsParams,
        ("events",),
        "One Get-WinEvent -FilterXml query per log; the QueryList is generated and escaped in "
        "Python from the validated parameters and passed on stdin.",
    ),
    "list_tdr_incidents": (
        IncidentParams,
        ("events",),
        "Two Get-WinEvent queries over the signature providers, then local matching/clustering.",
    ),
    "process_snapshot": (
        ProcessSnapshotParams,
        ("process_snapshot",),
        "Win32_OperatingSystem, Win32_PerfFormattedData_PerfDisk_PhysicalDisk, Win32_Process. "
        "Command lines are not collected.",
    ),
    "sensor_snapshot": (
        SensorSnapshotParams,
        ("sensors",),
        "Backends in order: LibreHardwareMonitor web server (localhost, GET data.json only), "
        "nvidia-smi, ACPI thermal zones, GPU engine counters. Live sensors have no history.",
    ),
    "driver_inventory": (
        DriverInventoryParams,
        ("drivers",),
        "Win32_PnPSignedDriver filtered to the allowlisted device classes.",
    ),
    "dxdiag_summary": (
        NoParams,
        ("dxdiag",),
        "dxdiag /x to a temporary file (deleted afterwards); only display fields are kept.",
    ),
    "system_info": (
        NoParams,
        ("system_info",),
        "Win32_OperatingSystem, the UBR registry value (read only), Win32_VideoController and "
        "the local UTC offset.",
    ),
    "describe_collection": (DescribeParams, (), "Reads nothing from the system."),
}

TOOL_NAMES: tuple[str, ...] = tuple(_TOOLS)


class ScriptSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    command: str
    source: str


class CollectionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str
    summary: str
    scripts: list[ScriptSource]
    commands: list[str]
    parameters_schema: dict[str, Any]
    read_only: bool = True


def describe_collection(tool_name: str) -> CollectionPlan:
    """Exactly which scripts and commands ``tool_name`` would run, with their full source and
    the parameter schema, so the user can review the collection before it happens."""
    params = DescribeParams(tool_name=tool_name)
    entry = _TOOLS.get(params.tool_name)
    if entry is None:
        raise ValueError(f"unknown tool {params.tool_name!r}; known tools: {list(TOOL_NAMES)}")
    model, methods, summary = entry
    scripts = [
        ScriptSource(
            name=name,
            command=" ".join(["powershell.exe", *POWERSHELL_FLAGS, name]),
            source=(PS_DIR / name).read_text(encoding="utf-8"),
        )
        for method in methods
        for name in SCRIPTS_BY_METHOD[method]
    ]
    commands = [cmd for method in methods for cmd in COMMANDS_BY_METHOD.get(method, ())]
    return CollectionPlan(
        tool=params.tool_name,
        summary=summary,
        scripts=scripts,
        commands=commands,
        parameters_schema=model.model_json_schema(),
    )
