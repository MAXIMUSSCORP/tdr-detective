"""Tests for the pc-stability-triage event compressor (loaded by path; it is not a package)."""

from __future__ import annotations

import importlib.util
import io
import json
import random
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / ".claude"
    / "skills"
    / "pc-stability-triage"
    / "scripts"
    / "compress_events.py"
)
BASE = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)
ID_LINE = re.compile(r"^(E\d+) ")


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("compress_events", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


ce = _load()


def rec(
    record_id: int,
    seconds: float,
    provider: str,
    event_id: int,
    *,
    level: str = "information",
    message: str = "",
    log: str = "System",
    data: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "record_id": record_id,
        "time": (BASE + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z"),
        "log": log,
        "provider": provider,
        "event_id": event_id,
        "level": level,
        "message": message,
        "data": data or {},
    }


def digest_of(records: list[dict[str, Any]], **kwargs: Any) -> Any:
    return ce.compress(ce.load_events(records), **kwargs)


def body_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.startswith("#")]


def line_for(digest: Any, eid: str) -> str:
    return next(line for line in body_lines(digest.text) if line.startswith(eid + " "))


TDR = "Reset TDR occurred on GPUID:100"
SVC_NAMES = ("BITS", "Spooler", "WSearch")


def test_template_masks_guids_hex_and_digits() -> None:
    a = ce.template(
        "CLSID {2593F8B9-4EAF-457C-B68A-50F6B8EA6B54} at 0xFFFFE000 addr fffffa80029c5060 x12"
    )
    b = ce.template("CLSID {11111111-2222-3333-4444-555555555555} at 0x1 addr 00000000deadbeef1 x7")
    assert a == b == "CLSID <guid> at <hex> addr <hex> x#"
    assert ce.template("Display  driver\tnvlddmkm") == "Display driver nvlddmkm"


def test_noise_groups_by_provider_and_event_id_with_counts_only() -> None:
    records = [
        rec(
            i,
            i,
            "Service Control Manager",
            7036,
            message=f"The {SVC_NAMES[i % 3]} service entered the running state.",
        )
        for i in range(1, 31)
    ]
    digest = digest_of(records)
    assert digest.timeline_ids == [] and digest.unclassified_ids == []
    assert len(digest.noise_ids) == 1
    line = line_for(digest, digest.noise_ids[0])
    assert "x30 System Service Control Manager/7036 [known-noise]" in line
    assert "+2 variants" in line
    assert "rec=" not in line  # counts only
    assert sorted(digest.evidence[digest.noise_ids[0]]) == list(range(1, 31))


def test_unclassified_errors_group_by_masked_template() -> None:
    records = [
        rec(1, 0, "FooDriver", 9, level="error", message="Device 3 failed with 0x10"),
        rec(2, 40, "FooDriver", 9, level="error", message="Device 7 failed with 0x22"),
        rec(3, 50, "FooDriver", 9, level="error", message="Completely different text"),
        rec(4, 900, "FooDriver", 9, level="error", message="Device 9 failed with 0x01"),
    ]
    digest = digest_of(records)
    assert len(digest.unclassified_ids) == 3
    first = line_for(digest, digest.unclassified_ids[0])
    assert "14:00:00-14:00:40 x2" in first and "rec=1,2" in first
    assert digest.evidence[digest.unclassified_ids[2]] == [4]  # same template, later burst


def _noise_groups(count: int) -> list[dict[str, Any]]:
    """`count` noise groups; group g has g + 1 events, so the larger groups sort first."""
    records: list[dict[str, Any]] = []
    record_id = 1
    for group in range(count):
        for _ in range(group + 1):
            records.append(rec(record_id, record_id, f"Prov{chr(65 + group)}", 100, message="x"))
            record_id += 1
    return records


@pytest.mark.parametrize(("groups", "listed", "folded"), [(3, 3, 0), (4, 3, 1), (5, 3, 2)])
def test_noise_groups_beyond_limit_fold_into_other_line(
    groups: int, listed: int, folded: int
) -> None:
    records = _noise_groups(groups)
    digest = digest_of(records, max_noise_lines=3)
    lines = [line_for(digest, eid) for eid in digest.noise_ids]
    other = [line for line in lines if "other noise" in line]
    assert len(lines) == listed + (1 if folded else 0)
    assert len(other) == (1 if folded else 0)
    if folded:
        assert f"in {folded} provider/event groups" in other[0]
        assert other[0].split()[1] == f"x{sum(range(1, folded + 1))}"  # the smallest groups
    covered = sorted(r for eid in digest.noise_ids for r in digest.evidence[eid])
    assert covered == [r["record_id"] for r in records]
    with pytest.raises(ValueError):
        digest_of(records, max_noise_lines=0)


def test_bursts_collapse_into_count_with_first_and_last_time() -> None:
    records = [
        rec(10, 0, "nvlddmkm", 153, level="error", message=TDR),
        rec(11, 5, "nvlddmkm", 153, level="error", message=TDR),
        rec(12, 9, "nvlddmkm", 153, level="error", message=TDR),
        rec(13, 900, "nvlddmkm", 153, level="error", message=TDR),
    ]
    digest = digest_of(records)
    assert len(digest.timeline_ids) == 2
    burst = line_for(digest, digest.timeline_ids[0])
    assert "14:00:00-14:00:09 x3" in burst and "[gpu-driver]" in burst
    assert digest.evidence[digest.timeline_ids[0]] == [10, 11, 12]
    assert digest.evidence[digest.timeline_ids[1]] == [13]
    # A wider burst gap joins all four.
    assert len(digest_of(records, burst_gap=1000).timeline_ids) == 1


def test_evidence_ids_are_sequential_unique_and_partition_records() -> None:
    records = [
        rec(
            1,
            0,
            "Display",
            4101,
            level="warning",
            message="Display driver nvlddmkm stopped responding.",
        ),
        rec(2, 10, "FooDriver", 1, level="error", message="boom"),
        rec(3, 20, "Microsoft-Windows-Kernel-General", 16, message="noise"),
    ]
    digest = digest_of(records)
    ids = [ID_LINE.match(line).group(1) for line in body_lines(digest.text)]  # type: ignore[union-attr]
    assert ids == ["E1", "E2", "E3"] == list(digest.evidence)
    covered = sorted(r for members in digest.evidence.values() for r in members)
    assert covered == [1, 2, 3]
    prefixed = digest_of(records, prefix="Q")
    assert list(prefixed.evidence) == ["Q1", "Q2", "Q3"]
    with pytest.raises(ValueError):
        digest_of(records, prefix="1bad")


def test_signature_fields_decode_bugcheck_and_live_dump_codes() -> None:
    records = [
        rec(
            1,
            0,
            "Microsoft-Windows-Kernel-Power",
            41,
            level="critical",
            message="The system has rebooted without cleanly shutting down first.",
            data={
                "BugcheckCode": "159",
                "BugcheckParameter1": "0x3",
                "PowerButtonTimestamp": "0",
                "BootAppStatus": "0",
            },
        ),
        rec(
            2,
            5,
            "Windows Error Reporting",
            1001,
            log="Application",
            message="Fault bucket , type 0 Event Name: LiveKernelEvent Response: Not available "
            "P1: 141 P2: ffffc30a",
        ),
        rec(
            3,
            6,
            "Microsoft-Windows-WER-SystemErrorReporting",
            1001,
            level="error",
            message="The computer has rebooted from a bugcheck. "
            "The bugcheck was: 0x00000116 (0xffff, 0x0).",
        ),
        rec(
            4,
            7,
            "Microsoft-Windows-WHEA-Logger",
            18,
            level="error",
            data={"ErrorSource": "3", "ApicId": "2", "RawData": "AA" * 50},
        ),
    ]
    text = digest_of(records).text
    assert "BugcheckCode=159(0x9F DRIVER_POWER_STATE_FAILURE)" in text
    assert "BootAppStatus" not in text
    assert "P1=141(0x141 VIDEO_ENGINE_TIMEOUT_DETECTED)" in text
    assert "bugcheck=0x116 VIDEO_TDR_FAILURE" in text
    assert "[whea] ErrorSource=3 ApicId=2" in text and "RawData" not in text


def test_known_noise_never_counts_as_unclassified_error() -> None:
    records = [
        rec(
            1,
            0,
            "Microsoft-Windows-DistributedCOM",
            10016,
            level="error",
            message="CLSID {2593F8B9-4EAF-457C-B68A-50F6B8EA6B54}",
        ),
        rec(2, 1, "Display", 4123, level="warning", message="HDR notification"),
        rec(
            3,
            2,
            "Windows Error Reporting",
            1001,
            log="Application",
            message="Event Name: WindowsUpdateFailure3 P1: x",
        ),
    ]
    digest = digest_of(records)
    assert digest.timeline_ids == [] and digest.unclassified_ids == []
    assert digest.text.count("[known-noise]") == 2


def test_digest_is_deterministic_regardless_of_input_order() -> None:
    records = build_noisy_sample()[0]["records"]
    shuffled = list(records)
    random.Random(3).shuffle(shuffled)  # noqa: S311 - seeded test data, not crypto
    first, second = digest_of(records), digest_of(shuffled)
    assert first.text == second.text
    assert first.evidence == second.evidence
    assert digest_of(records).text == first.text


def test_duplicates_are_dropped_and_input_shapes_accepted() -> None:
    one = rec(
        1,
        0,
        "Display",
        4101,
        level="warning",
        message="Display driver nvlddmkm stopped responding.",
    )
    wrapped = [{"type": "text", "text": json.dumps({"records": [one], "truncated": True})}]
    records, truncated = ce.extract_records(wrapped)
    assert truncated is True and len(records) == 1
    assert ce.extract_records([one, one]) == ([one, one], False)
    assert len(ce.load_events([one, one])) == 1
    assert (
        "WARNING source result was truncated"
        in ce.compress(ce.load_events(records), truncated=True).text
    )
    with pytest.raises(ValueError):
        ce.extract_records({"events": []})
    with pytest.raises(ValueError):
        ce.load_events([{"record_id": 1, "time": "2026-10-23T14:00:00Z", "provider": "x"}])


def test_cli_reads_files_and_stdin_and_writes_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sample = [rec(5, 0, "nvlddmkm", 153, level="error", message=TDR)]
    path = tmp_path / "events.json"
    path.write_text(json.dumps(sample), encoding="utf-8")
    map_path = tmp_path / "map.json"
    assert ce.main([str(path), "--map", str(map_path)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# pc-diag event digest | 1 records")
    assert json.loads(map_path.read_text(encoding="utf-8")) == {"E1": [5]}

    stdin = io.TextIOWrapper(io.BytesIO(json.dumps({"records": sample}).encode()), encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", stdin)
    assert ce.main([]) == 0
    assert "[gpu-driver]" in capsys.readouterr().out

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert ce.main([str(bad)]) == 2
    assert "invalid JSON" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Realistic noisy sample
# --------------------------------------------------------------------------- #

NOISE_TEMPLATES: list[tuple[str, str, int, str, str]] = [
    (
        "System",
        "Service Control Manager",
        7036,
        "information",
        "The {svc} service entered the {state} state.",
    ),
    (
        "System",
        "Microsoft-Windows-DistributedCOM",
        10016,
        "warning",
        "The application-specific permission settings do not grant Local Activation permission "
        "for the COM Server application with CLSID {{{guid}}} and APPID {{{guid2}}} to the user "
        "NT AUTHORITY\\LOCAL SERVICE SID (S-1-5-19) from address LocalHost (Using LRPC) running "
        "in the application container Unavailable SID (Unavailable). This security permission "
        "can be modified using the Component Services administrative tool.",
    ),
    (
        "System",
        "Display",
        4123,
        "warning",
        "The display configuration changed: HDR state for monitor {n} toggled by the system.",
    ),
    (
        "System",
        "Microsoft-Windows-Kernel-General",
        16,
        "information",
        "The access history in hive "
        "\\??\\C:\\Users\\user\\AppData\\Local\\Packages\\App_{n}\\Settings\\"
        "settings.dat was cleared updating {n} keys and creating {n} modified pages.",
    ),
    (
        "Application",
        "Microsoft-Windows-Security-SPP",
        16384,
        "information",
        "Successfully scheduled Software Protection service for re-start at {ts}. "
        "Reason: RulesEngine.",
    ),
    (
        "Application",
        "ESENT",
        326,
        "information",
        "svchost ({n},D,{n}) SRUJet: The database engine attached a database (1, "
        "C:\\WINDOWS\\system32\\SRU\\SRUDB.dat). (Time={n} seconds)",
    ),
    (
        "Application",
        "Microsoft-Windows-RestartManager",
        10001,
        "information",
        "Ending session {n} started {ts}.",
    ),
]
SERVICES = [
    "Windows Update",
    "BITS",
    "Delivery Optimization",
    "WinHTTP Web Proxy",
    "Windows Search",
]


def build_noisy_sample() -> tuple[dict[str, Any], list[int]]:
    """A query_events result: hundreds of noise events with a few signal events."""
    rng = random.Random(42)  # noqa: S311 - seeded test data, not crypto
    records: list[dict[str, Any]] = []
    record_id = 1000
    for _ in range(480):
        log, provider, event_id, level, text = rng.choice(NOISE_TEMPLATES)
        message = text.format(
            svc=rng.choice(SERVICES),
            state=rng.choice(["running", "stopped"]),
            guid=f"{rng.getrandbits(32):08X}-{rng.getrandbits(16):04X}-4EAF-457C-{rng.getrandbits(48):012X}",
            guid2=f"{rng.getrandbits(32):08X}-{rng.getrandbits(16):04X}-4C2D-9A1B-{rng.getrandbits(48):012X}",
            n=rng.randint(1, 99999),
            ts=f"2026-10-24T{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:00Z",
        )
        records.append(
            rec(
                record_id,
                rng.uniform(0, 1800),
                provider,
                event_id,
                level=level,
                message=message,
                log=log,
                data={
                    "param1": str(rng.randint(1, 9999)),
                    "param2": message[:40],
                    "Binary": "00" * 24,
                },
            )
        )
        record_id += 1

    signal = [
        rec(1, 840, "nvlddmkm", 153, level="error", message=TDR),
        rec(2, 843, "nvlddmkm", 153, level="error", message=TDR),
        rec(
            3,
            846,
            "Display",
            4101,
            level="warning",
            message="Display driver nvlddmkm stopped responding and has successfully recovered.",
        ),
        rec(
            4,
            850,
            "Windows Error Reporting",
            1001,
            log="Application",
            message="Fault bucket LKD_0x141_Tdr:6_IMAGE_nvlddmkm.sys, type 0 "
            "Event Name: LiveKernelEvent Response: Not available Cab Id: 0 Problem signature: "
            "P1: 141 P2: ffffc30a1b2c3d4e P3: fffff8064b8c1234 P4: 0 P5: 3fe8 P6: 10_0_26100 "
            "P7: 0_0 P8: 256_1 P9: P10: Attached files: "
            "\\\\?\\C:\\Windows\\LiveKernelReports\\WATCHDOG\\WATCHDOG-20261023-1414.dmp",
        ),
        rec(
            5,
            1500,
            "Microsoft-Windows-Kernel-Power",
            41,
            level="critical",
            message="The system has rebooted without cleanly shutting down first. "
            "This error could be caused "
            "if the system stopped responding, crashed, or lost power unexpectedly.",
            data={
                "BugcheckCode": "278",
                "BugcheckParameter1": "0xffffe000c2c404c0",
                "PowerButtonTimestamp": "0",
                "SleepInProgress": "0",
            },
        ),
        rec(
            6,
            1501,
            "EventLog",
            6008,
            level="error",
            message="The previous system shutdown at 2:24:40 PM on 10/23/2026 was unexpected.",
        ),
    ]
    records.extend(signal)
    return {"records": records, "truncated": False}, [r["record_id"] for r in signal]


def test_realistic_sample_compresses_10x_and_keeps_every_signal_event() -> None:
    sample, signal_ids = build_noisy_sample()
    raw = json.dumps(sample)
    raw_records, _ = ce.extract_records(json.loads(raw))
    digest = ce.compress(ce.load_events(raw_records))

    assert len(raw) / len(digest.text) >= 10, (len(raw), len(digest.text))
    timeline_records = {r for eid in digest.timeline_ids for r in digest.evidence[eid]}
    assert set(signal_ids) <= timeline_records
    assert timeline_records == set(signal_ids)  # no noise leaked into the timeline
    timeline_text = "\n".join(line_for(digest, eid) for eid in digest.timeline_ids)
    for needle in (
        "nvlddmkm/153",
        "Display/4101",
        "P1=141(0x141 VIDEO_ENGINE_TIMEOUT_DETECTED)",
        "BugcheckCode=278(0x116 VIDEO_TDR_FAILURE)",
        "EventLog/6008",
    ):
        assert needle in timeline_text
    assert digest.unclassified_ids == []
    noise_records = {r for eid in digest.noise_ids for r in digest.evidence[eid]}
    assert len(noise_records) == 480
