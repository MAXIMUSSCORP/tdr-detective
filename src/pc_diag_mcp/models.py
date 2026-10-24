"""Shared data contracts.

These models are the single source of truth for every component:

* MCP tools return them (serialized to JSON).
* Replay fixtures are stored in the same shapes.
* The eval harness scores against ``Diagnosis``.
* The timeline API validates ``Incident`` against ``schema/incident.schema.json``,
  which is generated from this module (``tdr-detective schema``).

Timestamps are always timezone-aware UTC.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value


UtcDatetime = Annotated[datetime, AfterValidator(_require_utc)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TimeWindow(_Model):
    start: UtcDatetime
    end: UtcDatetime

    @model_validator(mode="after")
    def _ordered(self) -> TimeWindow:
        if self.end <= self.start:
            raise ValueError("window end must be after start")
        return self


# --------------------------------------------------------------------------- #
# Telemetry records (tool outputs and fixture contents)
# --------------------------------------------------------------------------- #


class EventLevel(StrEnum):
    CRITICAL = "critical"
    ERROR = "error"
    WARNING = "warning"
    INFORMATION = "information"
    VERBOSE = "verbose"


class EventRecord(_Model):
    """One Windows Event Log entry, normalized."""

    record_id: int
    time: UtcDatetime
    log: str
    provider: str
    event_id: int
    level: EventLevel
    message: str = Field(description="Rendered message, whitespace-collapsed and truncated.")
    data: dict[str, str] = Field(
        default_factory=dict, description="EventData/UserData name -> value pairs."
    )


class ProcessInfo(_Model):
    pid: int
    name: str
    working_set_bytes: int
    private_bytes: int
    page_faults: int
    read_bytes: int
    write_bytes: int
    cpu_seconds: float
    started: UtcDatetime | None = None


class MemoryState(_Model):
    total_bytes: int
    available_bytes: int
    commit_used_bytes: int
    commit_limit_bytes: int


class DiskState(_Model):
    name: str
    queue_length: float
    percent_busy: float
    read_bytes_per_sec: float
    write_bytes_per_sec: float


class ProcessSnapshot(_Model):
    taken_at: UtcDatetime
    memory: MemoryState
    disks: list[DiskState]
    top_processes: list[ProcessInfo]


class SensorReading(_Model):
    hardware: str
    sensor: str
    kind: Literal["temperature", "clock", "load", "power", "fan", "voltage"]
    value: float
    unit: str
    source: str = Field(description="Which backend produced the reading.")


class SensorSnapshot(_Model):
    taken_at: UtcDatetime
    readings: list[SensorReading]
    unavailable: list[str] = Field(
        default_factory=list, description="Backends that were tried and not available, with reason."
    )


class GpuVendor(StrEnum):
    AMD = "amd"
    NVIDIA = "nvidia"
    INTEL = "intel"
    OTHER = "other"


class DriverInfo(_Model):
    device_name: str
    device_class: str
    provider: str
    version: str
    date: str | None = None
    inf_name: str | None = None
    signed: bool | None = None


class GpuInfo(_Model):
    name: str
    vendor: GpuVendor
    driver_version: str
    driver_date: str | None = None


class SystemInfo(_Model):
    os_caption: str
    os_build: str
    gpus: list[GpuInfo]
    utc_offset_minutes: int = Field(
        description="Local UTC offset when captured; used to resolve questions like 'at 9:42'."
    )


class DisplayDevice(_Model):
    name: str
    manufacturer: str
    driver_version: str
    driver_date: str | None = None
    dedicated_memory_mb: int | None = None
    feature_levels: list[str] = Field(default_factory=list)


class DxDiagSummary(_Model):
    directx_version: str
    display_devices: list[DisplayDevice]
    problems: list[str] = Field(
        default_factory=list, description="Non-empty 'Notes'/'Problems' entries reported by dxdiag."
    )


class EventQuery(_Model):
    """A validated Event Log query. Collectors must never receive free-form query text."""

    window: TimeWindow
    logs: list[Literal["System", "Application"]] = Field(default=["System", "Application"])
    providers: list[Annotated[str, Field(pattern=r"^[A-Za-z0-9 ._-]{1,128}$")]] | None = None
    event_ids: list[Annotated[int, Field(ge=0, le=65535)]] | None = None
    levels: list[EventLevel] | None = None
    max_events: int = Field(default=500, ge=1, le=5000)


class ReplayScenario(_Model):
    """``scenario.json`` in a replay directory. See ``pc_diag_mcp.collectors.replay``.

    Layout of a replay directory::

        scenario.json          ReplayScenario (required)
        events.json            list[EventRecord] (required)
        process_snapshot.json  ProcessSnapshot (optional)
        sensors.json           list[SensorSnapshot], time-ordered (optional)
        drivers.json           list[DriverInfo] (optional)
        dxdiag.json            DxDiagSummary (optional)
        frametimes.json        list[FrameTimeSample] (optional)
    """

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    description: str
    now: UtcDatetime = Field(description="The replayed 'current time'.")
    system: SystemInfo


class FrameTimeSample(_Model):
    time_offset_ms: float
    frame_time_ms: float
    gpu_busy_ms: float | None = None


# --------------------------------------------------------------------------- #
# Diagnosis (agent output) and Incident (what the timeline stores)
# --------------------------------------------------------------------------- #


class RootCauseCategory(StrEnum):
    GPU_DRIVER_TIMEOUT = "gpu_driver_timeout"
    GPU_THERMAL = "gpu_thermal"
    POWER_LOSS = "power_loss"
    HARDWARE_ERROR = "hardware_error"
    KERNEL_DRIVER_FAULT = "kernel_driver_fault"
    MEMORY_PRESSURE = "memory_pressure"
    STORAGE_IO = "storage_io"
    APPLICATION_HANG = "application_hang"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class RankedCause(_Model):
    category: RootCauseCategory
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    evidence_refs: list[str] = Field(
        default_factory=list, description="EvidenceItem ids supporting this cause."
    )


class BugReport(_Model):
    title: str
    summary: str
    os_build: str
    gpu: str
    driver_version: str
    occurred_at: UtcDatetime
    steps_to_reproduce: list[str]
    expected: str
    actual: str
    evidence: list[str] = Field(min_length=1)
    severity: Literal["critical", "high", "medium", "low"]


class Diagnosis(_Model):
    """The structured answer the agent must produce."""

    window: TimeWindow
    causes: list[RankedCause] = Field(min_length=1)
    next_steps: list[str] = Field(min_length=1)
    bug_report: BugReport

    @model_validator(mode="after")
    def _sorted(self) -> Diagnosis:
        confidences = [c.confidence for c in self.causes]
        if confidences != sorted(confidences, reverse=True):
            raise ValueError("causes must be ranked by descending confidence")
        return self


class EvidenceKind(StrEnum):
    EVENT = "event"
    SENSOR = "sensor"
    PROCESS = "process"
    DRIVER = "driver"
    FRAMETIME = "frametime"


class EvidenceItem(_Model):
    id: str
    kind: EvidenceKind
    time: UtcDatetime
    label: str
    detail: str = ""
    severity: Literal["info", "warning", "error", "critical"] = "info"
    value: float | None = None
    unit: str | None = None


class RunStats(_Model):
    config: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cost_usd: float | None = None
    latency_ms: int


class Incident(_Model):
    id: str
    question: str
    created_at: UtcDatetime
    system: SystemInfo
    diagnosis: Diagnosis
    evidence: list[EvidenceItem]
    run: RunStats | None = None
