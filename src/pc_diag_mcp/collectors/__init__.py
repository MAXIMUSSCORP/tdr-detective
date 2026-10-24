"""Telemetry collectors: live (PowerShell/CIM) and replay (recorded directories)."""

from pc_diag_mcp.collectors.base import (
    CollectionManifest,
    Collector,
    CollectorError,
    CollectorUnavailable,
    collect,
)
from pc_diag_mcp.collectors.powershell import PowerShellCollector
from pc_diag_mcp.collectors.replay import ReplayCollector

__all__ = [
    "CollectionManifest",
    "Collector",
    "CollectorError",
    "CollectorUnavailable",
    "PowerShellCollector",
    "ReplayCollector",
    "collect",
]
