"""Validated parameters for each tool.

The tool functions take plain arguments (so an MCP framework can register them directly) and
validate them through these models. ``describe_collection`` publishes the same models' JSON
Schema, so what the user is shown is exactly what is enforced.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pc_diag_mcp.models import EventLevel, UtcDatetime

LogName = Literal["System", "Application"]
ProviderName = Annotated[str, Field(pattern=r"^[A-Za-z0-9 ._-]{1,128}$")]
SortKey = Literal["working_set", "page_faults", "io"]
SaveMode = Literal["none", "tempfile"]

DEVICE_CLASS_ALLOWLIST: tuple[str, ...] = (
    "DISPLAY",
    "SYSTEM",
    "SCSIADAPTER",
    "HDC",
    "NET",
    "MEDIA",
)


# Bounds on model-supplied input: query size and how far back one call may reach.
MAX_LIST = 32
MAX_WINDOW = timedelta(days=90)


def check_window(start: datetime | None, end: datetime | None) -> None:
    if start is not None and end is not None and end - start > MAX_WINDOW:
        raise ValueError("the time window may span at most 90 days")


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class QueryEventsParams(_Params):
    start: UtcDatetime
    end: UtcDatetime
    logs: list[LogName] = Field(default=["System", "Application"], min_length=1)
    providers: list[ProviderName] | None = Field(default=None, max_length=MAX_LIST)
    event_ids: list[Annotated[int, Field(ge=0, le=65535)]] | None = Field(
        default=None, max_length=MAX_LIST
    )
    levels: list[EventLevel] | None = Field(default=None, max_length=MAX_LIST)
    max_events: int = Field(default=500, ge=1, le=5000)
    save_to: SaveMode = Field(
        default="none",
        description="'tempfile' writes the records to a private temp file and returns its path.",
    )

    @model_validator(mode="after")
    def _bounded(self) -> QueryEventsParams:
        check_window(self.start, self.end)
        return self


class IncidentParams(_Params):
    start: UtcDatetime
    end: UtcDatetime

    @model_validator(mode="after")
    def _bounded(self) -> IncidentParams:
        check_window(self.start, self.end)
        return self


class ProcessSnapshotParams(_Params):
    top_n: int = Field(default=15, ge=1, le=100)
    sort_by: SortKey = "working_set"


class SensorSnapshotParams(_Params):
    start: UtcDatetime | None = None
    end: UtcDatetime | None = None

    @model_validator(mode="after")
    def _both_or_neither(self) -> SensorSnapshotParams:
        if (self.start is None) != (self.end is None):
            raise ValueError("give both start and end, or neither")
        check_window(self.start, self.end)
        return self


class DriverInventoryParams(_Params):
    device_classes: list[str] = Field(
        default=list(DEVICE_CLASS_ALLOWLIST),
        min_length=1,
        max_length=MAX_LIST,
        description=f"Subset of {', '.join(DEVICE_CLASS_ALLOWLIST)}.",
    )

    @model_validator(mode="after")
    def _allowlisted(self) -> DriverInventoryParams:
        rejected = [c for c in self.device_classes if c.upper() not in DEVICE_CLASS_ALLOWLIST]
        if rejected:
            raise ValueError(
                f"device classes not allowed: {rejected}; allowed: {list(DEVICE_CLASS_ALLOWLIST)}"
            )
        return self


class NoParams(_Params):
    pass


class DescribeParams(_Params):
    tool_name: str
