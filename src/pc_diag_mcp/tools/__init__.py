"""Tool functions: take a ``Collector``, return pydantic results with a collection manifest."""

from pc_diag_mcp.tools.describe import CollectionPlan, describe_collection
from pc_diag_mcp.tools.events import (
    IncidentsResult,
    QueryEventsResult,
    SavedEventsResult,
    TdrIncident,
    list_tdr_incidents,
    query_events,
)
from pc_diag_mcp.tools.inventory import (
    DriverInventoryResult,
    DxDiagResult,
    ProcessSnapshotResult,
    SensorSnapshotResult,
    SystemInfoResult,
    driver_inventory,
    dxdiag_summary,
    process_snapshot,
    sensor_snapshot,
    system_info,
)
from pc_diag_mcp.tools.params import DEVICE_CLASS_ALLOWLIST

__all__ = [
    "DEVICE_CLASS_ALLOWLIST",
    "CollectionPlan",
    "DriverInventoryResult",
    "DxDiagResult",
    "IncidentsResult",
    "ProcessSnapshotResult",
    "QueryEventsResult",
    "SavedEventsResult",
    "SensorSnapshotResult",
    "SystemInfoResult",
    "TdrIncident",
    "describe_collection",
    "driver_inventory",
    "dxdiag_summary",
    "list_tdr_incidents",
    "process_snapshot",
    "query_events",
    "sensor_snapshot",
    "system_info",
]
