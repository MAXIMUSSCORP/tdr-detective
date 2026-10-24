"""Regression tests for the security and code review findings."""

from __future__ import annotations

import http.client
import json
import os
import stat
import sys
import threading
import urllib.error
from collections.abc import Iterator
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from pc_diag_mcp import record
from pc_diag_mcp.collectors import CollectorError, PowerShellCollector, ReplayCollector, collect
from pc_diag_mcp.collectors.powershell import LHM_UNREACHABLE, http_get_localhost
from pc_diag_mcp.models import DriverInfo, EventQuery, EventRecord, SensorSnapshot, TimeWindow
from pc_diag_mcp.record import Scrubber, ScrubError, capture, scrub_directory
from pc_diag_mcp.signatures import match_event
from pc_diag_mcp.tools import query_events
from pc_diag_mcp.tools.events import save_events_tempfile

from .conftest import FakeRunner, envelope, live, sample
from .test_replay import NOW, write_replay
from .test_signatures import ev
from .test_tools import T0, StubCollector, rec


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/data.json" and self.server.server_port % 2 == 0:  # type: ignore[attr-defined]
            body = b'{"Text": "Sensor"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(302)
        self.send_header("Location", "/Sensor?action=Set&id=/fan/0&value=0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def server() -> Iterator[HTTPServer]:
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_lhm_fetch_never_follows_redirects(server: HTTPServer) -> None:
    url = f"http://127.0.0.1:{server.server_port}/data.json"
    if server.server_port % 2 == 0:
        assert json.loads(http_get_localhost(url, 2.0)) == {"Text": "Sensor"}
    else:
        with pytest.raises(urllib.error.HTTPError) as caught:
            http_get_localhost(url, 2.0)
        assert caught.value.code == 302


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:80@evil.example/data.json",
        "http://evil.example/data.json",
        "https://127.0.0.1:8085/data.json",
        "http://127.0.0.1:8085/Sensor?action=Set&id=x&value=1",
        "http://127.0.0.1:8085/data.json?x=1",
        "http://localhost:8085/data.json",
    ],
)
def test_lhm_fetch_rejects_other_urls(url: str) -> None:
    with pytest.raises(ValueError, match="only"):
        http_get_localhost(url, 1.0)


def test_numeric_serials_are_scrubbed_and_flagged() -> None:
    scrubber = Scrubber()
    doc = {"SerialNumber": 123456789, "DeviceSerialNumber": 0, "IsSerialPort": True}
    assert scrubber.findings(doc) == ["$.SerialNumber: serial-like field SerialNumber"]
    clean = scrubber.scrub_value(doc)
    assert clean == {"SerialNumber": "[SERIAL-1]", "DeviceSerialNumber": 0, "IsSerialPort": True}
    assert scrubber.findings(clean) == []


def test_entra_id_sids_are_scrubbed() -> None:
    out = Scrubber().scrub_text("owner S-1-12-1-1111111111-2222222222-3333333333-4444444444")
    assert out == "owner [SID-1]"


@pytest.mark.parametrize("name", ["pc", "admin"])
def test_unscrubbable_captured_identifier_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setattr(record, "default_literals", dict)
    raw = write_replay(tmp_path / "raw", events=[])
    (raw / record.IDENTIFIERS_FILE).write_text(json.dumps({"computer_name": name}))
    with pytest.raises(ScrubError, match="too short or generic"):
        scrub_directory(raw, tmp_path / "out")
    assert not (tmp_path / "out").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_tempfile_dir_must_be_private(tmp_path: Path) -> None:
    (tmp_path / "elsewhere").mkdir()
    os.symlink(tmp_path / "elsewhere", tmp_path / "tdr-detective")
    with pytest.raises(PermissionError, match="not a real directory"):
        save_events_tempfile([], tmp_path)
    assert list((tmp_path / "elsewhere").iterdir()) == []

    loose = tmp_path / "loose"
    (loose / "tdr-detective").mkdir(parents=True)
    os.chmod(loose / "tdr-detective", 0o777)  # noqa: S103 - simulating a planted directory
    path = save_events_tempfile([], loose)
    assert stat.S_IMODE(os.stat(loose / "tdr-detective").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership")
def test_tempfile_dir_owned_by_someone_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    with pytest.raises(PermissionError, match="another user"):
        save_events_tempfile([], tmp_path)


# --------------------------------------------------------------------------- #
# Code review findings
# --------------------------------------------------------------------------- #


class _LateSnapshots(ReplayCollector):
    """Live snapshots are taken after the window ends, as in a real capture."""

    def sensors(self, window: TimeWindow | None) -> list[SensorSnapshot]:
        return [SensorSnapshot(taken_at=NOW + timedelta(seconds=5), readings=[])]

    def drivers(self, device_classes: list[str]) -> list[DriverInfo]:
        raise CollectorError("drivers.ps1 timed out after 60 s")


def _hour() -> TimeWindow:
    return TimeWindow(start=NOW - timedelta(hours=1), end=NOW)


def test_capture_now_covers_late_snapshots_and_survives_errors(tmp_path: Path) -> None:
    source = _LateSnapshots(write_replay(tmp_path / "src", events=[]))
    logs: list[str] = []
    target = capture(source, "late", _hour(), root=tmp_path / "raw", log=logs.append)
    replayed = ReplayCollector(target)
    assert replayed.scenario.now == NOW + timedelta(seconds=5)
    assert len(replayed.sensors(None)) == 1
    assert any("drivers.json: skipped" in line and "timed out" in line for line in logs)
    with pytest.raises(FileExistsError):
        capture(source, "late", _hour(), root=tmp_path / "raw", log=logs.append)


class _BrokenEvents(ReplayCollector):
    def events(self, query: EventQuery) -> list[EventRecord]:
        raise CollectorError("get_events.ps1 timed out")


def test_failed_capture_leaves_nothing_behind(tmp_path: Path) -> None:
    source = _BrokenEvents(write_replay(tmp_path / "src", events=[]))
    with pytest.raises(CollectorError):
        capture(source, "broken", _hour(), root=tmp_path / "raw", log=lambda line: None)
    assert not (tmp_path / "raw").exists() or list((tmp_path / "raw").iterdir()) == []


def test_scrub_destination_that_is_a_file(tmp_path: Path) -> None:
    raw = write_replay(tmp_path / "raw", events=[])
    (tmp_path / "file").write_text("x")
    with pytest.raises(ScrubError, match="not empty"):
        scrub_directory(raw, tmp_path / "file")


def test_profile_paths_in_wsl_and_posix_forms() -> None:
    out = Scrubber().scrub_text("/mnt/c/Users/alice/x and /home/bob/y and C:/Users/carol/z")
    assert out == "/mnt/c/Users/[USER]/x and /home/[USER]/y and C:/Users/[USER]/z"


def _live(runner: FakeRunner, **kwargs: object) -> PowerShellCollector:
    return live(runner, **kwargs)


def test_one_bad_event_is_skipped_but_all_bad_is_an_error(runner: FakeRunner) -> None:
    good = sample("events_raw.json")[:2]
    runner.responses["get_events.ps1"] = envelope([*good, {"xml": "<broken", "message": ""}])
    collector = _live(runner, wsl=False)
    wide = TimeWindow(start=NOW - timedelta(days=400), end=NOW + timedelta(days=30))
    query = EventQuery(window=wide)
    records, manifest = collect(collector, lambda: collector.events(query))
    assert len(records) == 2
    assert any("skipped 1 unparseable" in note for note in manifest.notes)
    runner.responses["get_events.ps1"] = envelope([{"xml": "<broken"}])
    with pytest.raises(CollectorError, match="no event could be parsed"):
        collector.events(query)


def test_wsl_fetches_lhm_through_the_windows_side(runner: FakeRunner) -> None:
    sensor = {
        "Text": "CPU Package",
        "SensorId": "/cpu/0/t/0",
        "Type": "Temperature",
        "Value": "55.0 °C",
    }
    runner.responses["lhm_http.ps1"] = envelope(
        {"body": json.dumps({"Text": "x", "Children": [sensor]})}
    )

    def forbidden(url: str, timeout: float) -> bytes:
        raise AssertionError("must not fetch from the WSL side")

    collector = _live(runner, http_get=forbidden, wsl=True)
    snapshot, manifest = collect(collector, lambda: collector.sensors(None))
    assert snapshot[0].readings[0].sensor == "CPU Package"
    assert "ps/lhm_http.ps1" in manifest.scripts
    runner.responses["lhm_http.ps1"] = json.dumps(
        {"ok": False, "kind": "unavailable", "error": "Unable to connect to the remote server"}
    )
    assert collector.sensors(None)[0].unavailable[0] == LHM_UNREACHABLE
    runner.responses["lhm_http.ps1"] = envelope({})
    assert "unreadable" in collector.sensors(None)[0].unavailable[0]


def test_lhm_protocol_errors_and_empty_nvidia_output(runner: FakeRunner) -> None:
    def incomplete(url: str, timeout: float) -> bytes:
        raise http.client.IncompleteRead(b"")

    runner.responses["nvidia-smi"] = "NVIDIA-SMI has failed\n"
    unavailable = _live(runner, http_get=incomplete, wsl=False).sensors(None)[0].unavailable
    assert unavailable[:2] == [LHM_UNREACHABLE, "nvidia-smi produced no parsable readings"]


def test_query_events_notes_the_cap() -> None:
    stub = StubCollector([rec(i, i, "Display", 4101) for i in range(5)])
    result = query_events(stub, T0, T0 + timedelta(hours=1), max_events=3)
    assert result.count == 3
    assert any("max_events cap" in note for note in result.collection.notes)


def test_sleep_in_progress_false_is_not_a_transition() -> None:
    data = {"BugcheckCode": "0", "SleepInProgress": "False"}
    hit = match_event(ev("Microsoft-Windows-Kernel-Power", 41, data=data))
    assert hit is not None and "sleep" not in hit.detail
