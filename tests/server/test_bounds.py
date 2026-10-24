"""Tool parameter bounds: list lengths and a 90-day window cap."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from pc_diag_mcp.models import EventLevel
from pc_diag_mcp.tools import list_tdr_incidents, query_events, sensor_snapshot
from pc_diag_mcp.tools.params import MAX_LIST, MAX_WINDOW

from .test_tools import T0, StubCollector

END = T0 + timedelta(hours=1)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"providers": [f"P{i}" for i in range(MAX_LIST + 1)]},
        {"event_ids": list(range(MAX_LIST + 1))},
        {"levels": [EventLevel.ERROR] * (MAX_LIST + 1)},
    ],
)
def test_query_lists_are_capped(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="at most 32"):
        query_events(StubCollector(), T0, END, **kwargs)  # type: ignore[arg-type]


def test_query_lists_at_the_cap_are_accepted() -> None:
    result = query_events(StubCollector(), T0, END, event_ids=list(range(MAX_LIST)))
    assert result.count == 0


def test_window_cap() -> None:
    assert timedelta(days=90) == MAX_WINDOW
    stub = StubCollector()
    assert query_events(stub, T0 - MAX_WINDOW, T0).count == 0
    too_long = T0 - MAX_WINDOW - timedelta(seconds=1)
    with pytest.raises(ValidationError, match="90 days"):
        query_events(stub, too_long, T0)
    with pytest.raises(ValidationError, match="90 days"):
        list_tdr_incidents(stub, too_long, T0)
    with pytest.raises(ValidationError, match="90 days"):
        sensor_snapshot(stub, too_long, T0)
