from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pc_diag_mcp import record
from pc_diag_mcp.collectors import CollectorUnavailable, ReplayCollector
from pc_diag_mcp.models import EventQuery, TimeWindow
from pc_diag_mcp.record import Scrubber, ScrubError, capture, main, scrub_directory

from .test_replay import NOW, write_replay


@pytest.fixture(autouse=True)
def _no_host_literals(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep results independent of the machine running the tests.
    monkeypatch.setattr(record, "default_literals", dict)


def test_scrub_text_patterns_are_replaced_with_stable_placeholders() -> None:
    scrubber = Scrubber()
    scrubber.add_literal("TESTHOST", "[COMPUTERNAME]")
    scrubber.add_literal("ab", "[X]")  # too short to replace safely
    scrubber.add_literal("Admin", "[X]")  # generic word
    text = (
        "Host testhost user path C:\\Users\\Alice\\AppData\\x and \\\\?\\C:\\Users\\Alice\\y "
        "public C:\\Users\\Public\\z SID S-1-5-21-111-222-333-1001 and S-1-5-21-111-222-333-1001 "
        "well-known S-1-5-18 mac 00:1A:2B:3C:4D:5E alt 00-1a-2b-3c-4d-5f mail a.b@example.com "
        "dev PCI\\VEN_10DE&DEV_2F58&SUBSYS_15F41043&REV_A1\\34388A8CF42DB04800 "
        "guid {8110c099-6812-499c-b29c-eef52811dbb0} hex 0xffffc2887a2632c0"
    )
    out = scrubber.scrub_text(text)
    assert "testhost" not in out.lower() and "[COMPUTERNAME]" in out
    assert out.count("C:\\Users\\[USER]") == 2 and "Alice" not in out
    assert "C:\\Users\\Public\\z" in out
    assert out.count("[SID-1]") == 2 and "S-1-5-18" in out
    assert "[MAC-1]" in out and "[MAC-2]" in out
    assert "[EMAIL-1]" in out
    assert "PCI\\VEN_10DE&DEV_2F58&SUBSYS_15F41043&REV_A1\\[INSTANCE-1]" in out
    assert "{8110c099-6812-499c-b29c-eef52811dbb0}" in out and "0xffffc2887a2632c0" in out
    assert scrubber.literals == {"TESTHOST": "[COMPUTERNAME]"}
    assert scrubber.findings(out) == []


def test_scrub_value_handles_keys_and_serial_fields() -> None:
    scrubber = Scrubber()
    scrubber.add_literal("alice", "[USERNAME]")
    doc = {
        "alice-notes": "x",
        "items": [{"SerialNumber": "ABC123"}, {"DeviceSerialNumber": "0x0"}, {"n": 5}],
    }
    out = scrubber.scrub_value(doc)
    assert out == {
        "[USERNAME]-notes": "x",
        "items": [{"SerialNumber": "[SERIAL-1]"}, {"DeviceSerialNumber": "0x0"}, {"n": 5}],
    }
    assert scrubber.findings(out) == []
    assert scrubber.findings(doc) == [
        "$.<key>: host literal",
        "$.items[0].SerialNumber: serial-like field SerialNumber",
    ]


def write_raw(root: Path) -> Path:
    raw = write_replay(
        root,
        events=[
            {
                "record_id": 1,
                "time": NOW.isoformat(),
                "log": "Application",
                "provider": "Windows Error Reporting",
                "event_id": 1001,
                "level": "information",
                "message": "Event Name: LiveKernelEvent P1: 141 on HOSTPC by alice",
                "data": {"AttachedFiles": "C:\\Users\\alice\\AppData\\Local\\Temp\\x.dmp"},
            }
        ],
    )
    (raw / record.IDENTIFIERS_FILE).write_text(
        json.dumps({"computer_name": "HOSTPC", "user_name": "alice"})
    )
    (raw / "notes.txt").write_text("captured on HOSTPC\n")
    return raw


def test_scrub_directory_round_trip(tmp_path: Path) -> None:
    raw = write_raw(tmp_path / "raw" / "case")
    written = scrub_directory(raw, tmp_path / "clean")
    names = sorted(p.name for p in written)
    assert names == ["events.json", "notes.txt", "scenario.json"]
    assert not (tmp_path / "clean" / record.IDENTIFIERS_FILE).exists()
    text = "".join(p.read_text(encoding="utf-8") for p in written)
    assert "HOSTPC" not in text and "alice" not in text
    assert "[COMPUTERNAME]" in text and "C:\\\\Users\\\\[USER]" in text
    # Scrubbed output is still a valid replay directory.
    collector = ReplayCollector(tmp_path / "clean")
    window = TimeWindow(start=NOW - timedelta(hours=1), end=NOW)
    assert len(collector.events(EventQuery(window=window))) == 1


def test_scrub_fails_closed_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = write_raw(tmp_path / "raw")
    # Simulate a scrubbing bug: text replacement that does nothing.
    monkeypatch.setattr(Scrubber, "scrub_text", lambda self, text: text)
    with pytest.raises(ScrubError, match="still match"):
        scrub_directory(raw, tmp_path / "clean")
    assert not (tmp_path / "clean").exists()
    assert [p.name for p in tmp_path.iterdir()] == ["raw"]


def test_scrub_rejects_bad_inputs(tmp_path: Path) -> None:
    with pytest.raises(ScrubError, match="not found"):
        scrub_directory(tmp_path / "missing", tmp_path / "out")
    raw = write_raw(tmp_path / "raw")
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / "x").write_text("x")
    with pytest.raises(ScrubError, match="not empty"):
        scrub_directory(raw, tmp_path / "busy")
    (raw / "blob.bin").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ScrubError, match="binary"):
        scrub_directory(raw, tmp_path / "out")


def test_scrub_cli_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    raw = write_raw(tmp_path / "raw")
    assert main(["scrub", str(raw), str(tmp_path / "ok"), "--literal", "LiveKernelEvent"]) == 0
    events = (tmp_path / "ok" / "events.json").read_text(encoding="utf-8")
    assert "LiveKernelEvent" not in events and "[REDACTED]" in events
    assert main(["scrub", str(tmp_path / "nope"), str(tmp_path / "x")]) == 2
    assert "scrub failed" in capsys.readouterr().err


def test_default_literals_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()
    monkeypatch.setenv("COMPUTERNAME", "BOXNAME")
    monkeypatch.setenv("USERNAME", "winuser")
    literals = record.default_literals()
    assert literals["BOXNAME"] == "[COMPUTERNAME]" and literals["winuser"] == "[USERNAME]"


def test_capture_writes_replay_directory(tmp_path: Path) -> None:
    source = ReplayCollector(write_raw(tmp_path / "src"))
    window = TimeWindow(start=NOW - timedelta(hours=1), end=NOW)
    logs: list[str] = []
    target = capture(
        source,
        "case-1",
        window,
        root=tmp_path / "raw",
        include_dxdiag=True,
        identity=lambda: {"computer_name": "HOSTPC"},
        log=logs.append,
    )
    assert target == tmp_path / "raw" / "case-1"
    replayed = ReplayCollector(target)
    assert replayed.scenario.id == "case-1" and replayed.scenario.now == NOW
    assert len(replayed.events(EventQuery(window=window))) == 1
    # Optional sources the source collector lacks are skipped, not fatal.
    assert any("skipped" in line for line in logs)
    with pytest.raises(CollectorUnavailable):
        replayed.dxdiag()
    assert json.loads((target / record.IDENTIFIERS_FILE).read_text()) == {"computer_name": "HOSTPC"}


def test_capture_cli_uses_live_collector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = ReplayCollector(write_raw(tmp_path / "src"))
    monkeypatch.setattr(source, "identity", lambda: {"user_name": "alice"}, raising=False)
    monkeypatch.setattr(record, "PowerShellCollector", lambda: source)
    end = NOW.isoformat()
    start = (NOW - timedelta(hours=2)).isoformat()
    args = ["capture", "cli-case", "--root", str(tmp_path / "raw"), "--start", start, "--end", end]
    assert main(args) == 0
    assert (tmp_path / "raw" / "cli-case" / "events.json").is_file()
    with pytest.raises(SystemExit):
        main(["capture", "x", "--start", "2026-10-06T05:00:00"])  # naive time rejected


def test_parse_time_converts_to_utc() -> None:
    parsed = record._parse_time("2026-10-06T00:00:00-06:00")
    assert parsed == datetime(2026, 10, 6, 6, 0, tzinfo=UTC)
