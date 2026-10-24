from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.client.client import CallToolResult
from fastmcp.client.transports import StdioTransport

from pc_diag_mcp import server
from pc_diag_mcp.collectors import PowerShellCollector, ReplayCollector
from pc_diag_mcp.collectors.base import CollectorError, CollectorUnavailable
from pc_diag_mcp.tools.describe import TOOL_NAMES

REPO = Path(__file__).resolve().parents[2]
SCENARIO = REPO / "evals" / "scenarios" / "syn-026"
START = "2026-10-15T01:30:00Z"
END = "2026-10-15T02:00:00Z"


def run[T](coro: Callable[[Client[Any]], Awaitable[T]], collector: Any = None) -> T:
    mcp = server.create_server(collector or ReplayCollector(SCENARIO))

    async def go() -> T:
        async with Client(mcp) as client:
            return await coro(client)

    return asyncio.run(go())


def call(name: str, args: dict[str, Any] | None = None, collector: Any = None) -> CallToolResult:
    return run(lambda c: c.call_tool(name, args or {}, raise_on_error=False), collector)


def error_text(result: CallToolResult) -> str:
    assert result.is_error
    return " ".join(getattr(block, "text", "") for block in result.content)


def test_lists_every_tool_read_only_with_descriptions() -> None:
    tools = run(lambda c: c.list_tools())
    assert {t.name for t in tools} == set(TOOL_NAMES)
    for tool in tools:
        assert tool.description and len(tool.description) > 60, tool.name
        ann = tool.annotations
        assert ann is not None, tool.name
        assert ann.read_only_hint is True
        assert ann.destructive_hint is False
        assert ann.open_world_hint is False
        assert ann.title


def test_query_events_schema_constrains_inputs() -> None:
    tools = {t.name: t for t in run(lambda c: c.list_tools())}
    props = tools["query_events"].input_schema["properties"]
    assert set(props) >= {"start", "end", "logs", "providers", "save_to", "max_events"}
    assert "tempfile" in json.dumps(props["save_to"])


def test_system_info() -> None:
    result = call("system_info")
    assert not result.is_error
    data = result.structured_content
    assert data is not None
    assert data["system"]["os_build"] == "22631.4317"
    assert data["collection"]["source"] == "replay"


def test_list_tdr_incidents() -> None:
    result = call("list_tdr_incidents", {"start": START, "end": END})
    assert not result.is_error
    data = result.structured_content
    assert data is not None
    assert data["incidents"], data
    assert any(
        s["signature_id"] == "display_tdr_4101" for i in data["incidents"] for s in i["signals"]
    )


def test_query_events_inline() -> None:
    result = call("query_events", {"start": START, "end": END, "levels": ["error", "warning"]})
    assert not result.is_error
    data = result.structured_content
    assert data is not None
    assert data["count"] == len(data["events"]) > 0
    assert {e["level"] for e in data["events"]} <= {"error", "warning"}


def test_query_events_tempfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    result = call("query_events", {"start": START, "end": END, "save_to": "tempfile"})
    assert not result.is_error
    data = result.structured_content
    assert data is not None
    assert "events" not in data
    path = Path(data["path"])
    assert path.parent == tmp_path / "tdr-detective"
    assert len(json.loads(path.read_text(encoding="utf-8"))) == data["count"] > 0


def test_other_tools_work_on_replay() -> None:
    assert not call("process_snapshot", {"top_n": 3}).is_error
    assert not call("sensor_snapshot", {"start": START, "end": END}).is_error
    drivers = call("driver_inventory", {"device_classes": ["display"]})
    assert not drivers.is_error
    assert drivers.structured_content is not None
    assert drivers.structured_content["device_classes"] == ["DISPLAY"]
    assert not call("dxdiag_summary").is_error
    plan = call("describe_collection", {"tool_name": "query_events"})
    assert not plan.is_error
    assert plan.structured_content is not None
    assert plan.structured_content["read_only"] is True


def test_bad_device_class_is_a_tool_error() -> None:
    text = error_text(call("driver_inventory", {"device_classes": ["USB"]}))
    assert "USB" in text
    assert "DISPLAY" in text


@pytest.mark.parametrize(
    "provider",
    ["Display'; Remove-Item C:\\ -Recurse", "<QueryList>", "a" * 200, "$(calc)"],
)
def test_hostile_provider_is_a_tool_error(provider: str) -> None:
    result = call("query_events", {"start": START, "end": END, "providers": [provider]})
    assert result.is_error
    # The server keeps serving after the rejected call.
    assert not call("system_info").is_error


def test_naive_timestamp_is_a_tool_error() -> None:
    text = error_text(call("list_tdr_incidents", {"start": "2026-10-15T01:30:00", "end": END}))
    assert "timezone" in text


def test_unknown_tool_in_describe_is_a_tool_error() -> None:
    text = error_text(call("describe_collection", {"tool_name": "format_disk"}))
    assert "unknown tool" in text


def test_missing_replay_file_names_the_source(tmp_path: Path) -> None:
    for name in ("scenario.json", "events.json"):
        (tmp_path / name).write_bytes((SCENARIO / name).read_bytes())
    text = error_text(call("sensor_snapshot", collector=ReplayCollector(tmp_path)))
    assert "replay:sensors.json" in text
    assert "unavailable" in text


class _Failing:
    source = "live"

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def system_info(self) -> Any:
        raise self.exc


def test_collector_errors_are_tool_errors() -> None:
    text = error_text(
        call("system_info", collector=_Failing(CollectorUnavailable("powershell.exe", "not found")))
    )
    assert "powershell.exe" in text and "not found" in text
    text = error_text(call("system_info", collector=_Failing(CollectorError("script timed out"))))
    assert "script timed out" in text


def test_collector_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(server.REPLAY_ENV, str(SCENARIO))
    assert isinstance(server.collector_from_env(), ReplayCollector)
    monkeypatch.delenv(server.REPLAY_ENV)
    assert isinstance(server.collector_from_env(), PowerShellCollector)


def test_collector_from_env_rejects_missing_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(server.REPLAY_ENV, str(tmp_path / "nope"))
    with pytest.raises(CollectorError, match="not found"):
        server.collector_from_env()


def test_logging_goes_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    server.configure_logging()
    logging.getLogger("pc_diag_mcp").warning("probe-message")
    captured = capsys.readouterr()
    assert "probe-message" in captured.err
    assert "probe-message" not in captured.out


def test_stdio_server_process_keeps_stdout_for_protocol(tmp_path: Path) -> None:
    env = {**os.environ, server.REPLAY_ENV: str(SCENARIO)}
    log_file = tmp_path / "stderr.log"
    transport = StdioTransport(
        command=sys.executable, args=["-m", "pc_diag_mcp.server"], env=env, log_file=log_file
    )

    async def go() -> CallToolResult:
        async with Client(transport) as client:
            return await client.call_tool("system_info", {})

    result = asyncio.run(go())
    assert result.structured_content is not None
    assert result.structured_content["system"]["os_build"] == "22631.4317"
    assert "serving replay collector" in log_file.read_text(encoding="utf-8")
