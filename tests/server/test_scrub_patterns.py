"""Scrubber coverage: network identifiers, UNC paths, URLs, bare MACs, profile literals."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from pc_diag_mcp import record
from pc_diag_mcp.collectors import ReplayCollector
from pc_diag_mcp.models import TimeWindow
from pc_diag_mcp.record import Scrubber, ScrubError, capture, main, scrub_directory

from .test_replay import NOW, write_replay


@pytest.fixture(autouse=True)
def _no_host_literals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(record, "default_literals", dict)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("peer 192.168.1.20 replied", "peer [IPV4-1] replied"),
        ("gateway 10.0.0.1.", "gateway [IPV4-1]."),
        ("bound 255.255.255.255 and 127.0.0.1 and 0.0.0.0", None),  # non-identifying, kept
        ("driver 32.0.101.8509 and 32.0.16.1742", None),  # versions: an octet is > 255
        ("build 10.0.26200.6899", None),
        ("v1.2.3.4 tag", None),  # glued to a word: a version, not an address
        ("link fe80::1c2d:3e4f:5a6b:7c8d%12 up", "link [IPV6-1] up"),
        ("addr 2001:db8:0:0:0:0:2:1", "addr [IPV6-1]"),
        ("at 16:47:42 using std::vector", None),  # times and C++ scopes are not IPv6
        ("share \\\\FILESRV\\team$\\docs", "share \\\\[UNC-1]\\docs"),
        ("device \\\\?\\C:\\Windows\\x and \\\\.\\PhysicalDrive0", None),
        (
            "see https://example.com/report?user=alice&id=42 now",
            "see [URL-1] now",
        ),
        ("plain https://example.com/docs/page stays", None),
    ],
)
def test_text_patterns(text: str, expected: str | None) -> None:
    scrubber = Scrubber()
    out = scrubber.scrub_text(text)
    assert out == (expected if expected is not None else text)
    assert scrubber.findings(out) == []
    if expected is not None:
        assert scrubber.findings(text), "fail-closed check must flag the raw text"


def test_bare_macs_only_in_mac_like_fields() -> None:
    scrubber = Scrubber()
    doc = {
        "MacAddress": "001A2B3C4D5E",
        "PermanentAddress": "001a2b3c4d5f",
        "Checksum": "001A2B3C4D5E",
        "MacAddressShort": "001A2B",
    }
    assert scrubber.findings(doc) == [
        "$.MacAddress: MAC address field MacAddress",
        "$.PermanentAddress: MAC address field PermanentAddress",
    ]
    clean = scrubber.scrub_value(doc)
    assert clean == {
        "MacAddress": "[MAC-1]",
        "PermanentAddress": "[MAC-2]",
        "Checksum": "001A2B3C4D5E",
        "MacAddressShort": "001A2B",
    }
    assert scrubber.findings(clean) == []


def _raw_with_identity(root: Path, identity: dict[str, str], text: str) -> Path:
    raw = write_replay(
        root,
        events=[
            {
                "record_id": 1,
                "time": NOW.isoformat(),
                "log": "System",
                "provider": "Display",
                "event_id": 4101,
                "level": "warning",
                "message": text,
                "data": {},
            }
        ],
    )
    (raw / record.IDENTIFIERS_FILE).write_text(json.dumps(identity))
    return raw


def test_user_profile_and_its_leaf_are_literals(tmp_path: Path) -> None:
    raw = _raw_with_identity(
        tmp_path / "raw",
        {
            "computer_name": "HOSTPC",
            "user_name": "alice",
            "user_profile": "D:\\Profiles\\alice.corp",
        },
        "loaded D:\\Profiles\\alice.corp\\ntuser.dat for alice.corp",
    )
    scrub_directory(raw, tmp_path / "out")
    text = (tmp_path / "out" / "events.json").read_text(encoding="utf-8")
    assert "alice" not in text and "Profiles" not in text
    assert "[USERPROFILE]" in text and "[USERNAME]" in text


def test_generic_profile_leaf_fails_closed(tmp_path: Path) -> None:
    raw = _raw_with_identity(
        tmp_path / "raw", {"user_profile": "C:\\Users\\user"}, "nothing to see"
    )
    with pytest.raises(ScrubError, match="user_profile"):
        scrub_directory(raw, tmp_path / "out")


@pytest.mark.parametrize("name", ["../escape", "a/b", "a\\b", "..", "UPPER", "", "-lead"])
def test_capture_rejects_unsafe_names(tmp_path: Path, name: str) -> None:
    source = ReplayCollector(write_replay(tmp_path / "src", events=[]))
    window = TimeWindow(start=NOW - timedelta(hours=1), end=NOW)
    with pytest.raises(ValueError, match="capture name"):
        capture(source, name, window, root=tmp_path / "raw", log=lambda line: None)
    assert not (tmp_path / "raw").exists()
    with pytest.raises(SystemExit):
        main(["capture", name, "--root", str(tmp_path / "raw")])


def test_cli_reminds_to_review(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    raw = _raw_with_identity(tmp_path / "raw", {"computer_name": "HOSTPC"}, "hello")
    assert main(["scrub", str(raw), str(tmp_path / "out")]) == 0
    assert "review the output before committing" in capsys.readouterr().out.lower()


def test_version_fields_are_not_ip_addresses() -> None:
    scrubber = Scrubber()
    doc = {"version": "4.92.0.0", "driver_version": "1.4.6.3", "peer": "4.92.0.0"}
    assert scrubber.findings(doc) == ["$.peer: IPv4 address"]
    assert scrubber.scrub_value(doc) == {
        "version": "4.92.0.0",
        "driver_version": "1.4.6.3",
        "peer": "[IPV4-1]",
    }
