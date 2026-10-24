from __future__ import annotations

import filecmp
import json
import re
import shutil
from collections import Counter
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from evals import generate
from evals.schema import EvalCase, load_cases
from evals.specs import SPECS
from pc_diag_mcp.models import (
    DriverInfo,
    DxDiagSummary,
    EventRecord,
    FrameTimeSample,
    ProcessSnapshot,
    ReplayScenario,
    RootCauseCategory,
    SensorSnapshot,
)
from pc_diag_mcp.record import Scrubber

EVENTS = TypeAdapter(list[EventRecord])
SENSORS = TypeAdapter(list[SensorSnapshot])
DRIVERS = TypeAdapter(list[DriverInfo])
FRAMES = TypeAdapter(list[FrameTimeSample])
OPTIONAL_FILES = {
    "process_snapshot.json": TypeAdapter(ProcessSnapshot),
    "sensors.json": SENSORS,
    "drivers.json": DRIVERS,
    "dxdiag.json": TypeAdapter(DxDiagSummary),
    "frametimes.json": FRAMES,
}
ALLOWED_FILES = {"scenario.json", "events.json", *OPTIONAL_FILES}
STORAGE_WARNINGS = {("stornvme", 129), ("storahci", 129), ("disk", 153), ("disk", 7), ("disk", 51)}


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("gen")
    generate.generate(root)
    return root


@pytest.fixture(scope="module")
def all_cases(generated: Path) -> list[EvalCase]:
    return load_cases(generated / "cases.jsonl")


@pytest.fixture(scope="module")
def cases(all_cases: list[EvalCase]) -> list[EvalCase]:
    """The synthetic cases; recorded ones are covered by their own tests below."""
    return [c for c in all_cases if c.source == "synthetic"]


def _scenario_dir(root: Path, case: EvalCase) -> Path:
    return root / "scenarios" / case.id


def _events(root: Path, case: EvalCase) -> list[EventRecord]:
    return EVENTS.validate_json((_scenario_dir(root, case) / "events.json").read_bytes())


# --------------------------------------------------------------------------- #
# committed output, determinism, --check
# --------------------------------------------------------------------------- #


def test_committed_output_is_current() -> None:
    assert generate.check(generate.EVALS_DIR) == []


def test_generation_is_deterministic(generated: Path, tmp_path: Path) -> None:
    generate.generate(tmp_path)
    cmp = filecmp.dircmp(generated, tmp_path)
    assert generate.check(tmp_path) == []
    assert not cmp.diff_files
    assert (generated / "cases.jsonl").read_bytes() == (tmp_path / "cases.jsonl").read_bytes()


def test_check_detects_drift(generated: Path, tmp_path: Path) -> None:
    copy = tmp_path / "copy"
    shutil.copytree(generated, copy)
    assert generate.main(["--check", "--root", str(copy)]) == 0

    target = copy / "scenarios" / SPECS[0].id / "events.json"
    target.write_text(target.read_text(encoding="utf-8").replace("Service", "Servise", 1), "utf-8")
    (copy / "scenarios" / SPECS[1].id / "stray.json").write_text("[]", encoding="utf-8")
    shutil.rmtree(copy / "scenarios" / SPECS[2].id)

    drift = generate.check(copy)
    assert any(SPECS[0].id in d and "events.json" in d for d in drift)
    assert any("stray.json" in d for d in drift)
    assert any(SPECS[2].id in d for d in drift)
    assert generate.main(["--check", "--root", str(copy)]) == 1


def test_main_writes_to_root(tmp_path: Path) -> None:
    assert generate.main(["--root", str(tmp_path)]) == 0
    assert (tmp_path / "cases.jsonl").exists()
    stale = tmp_path / "scenarios" / "syn-stale"
    stale.mkdir()
    assert generate.main(["--root", str(tmp_path)]) == 0
    assert not stale.exists()


def test_total_size_is_reasonable(generated: Path) -> None:
    total = sum(p.stat().st_size for p in (generated / "scenarios").rglob("*") if p.is_file())
    assert total < 5 * 1024 * 1024


# --------------------------------------------------------------------------- #
# every scenario validates against the shared contracts
# --------------------------------------------------------------------------- #


def test_cases_file_matches_scenarios(generated: Path, cases: list[EvalCase]) -> None:
    assert [c.id for c in cases] == [s.id for s in SPECS]
    assert len({c.id for c in cases}) == len(cases)
    dirs = sorted(p.name for p in (generated / "scenarios").iterdir())
    assert dirs == sorted(c.id for c in cases)
    for case in cases:
        assert case.id.startswith("syn-")
        assert case.scenario == f"evals/scenarios/{case.id}"
        assert case.source == "synthetic"
        assert (generate.REPO_ROOT / case.scenario / "scenario.json").exists()


def test_every_scenario_validates(generated: Path, cases: list[EvalCase]) -> None:
    for case in cases:
        sdir = _scenario_dir(generated, case)
        assert {p.name for p in sdir.iterdir()} <= ALLOWED_FILES
        scenario = ReplayScenario.model_validate_json((sdir / "scenario.json").read_bytes())
        assert scenario.id == case.id
        events = _events(generated, case)
        assert events, case.id
        for name, adapter in OPTIONAL_FILES.items():
            path = sdir / name
            if path.exists():
                adapter.validate_json(path.read_bytes())


def test_event_ordering_and_record_ids(generated: Path, cases: list[EvalCase]) -> None:
    for case in cases:
        scenario = ReplayScenario.model_validate_json(
            (_scenario_dir(generated, case) / "scenario.json").read_bytes()
        )
        events = _events(generated, case)
        times = [e.time for e in events]
        assert times == sorted(times), case.id
        assert times[-1] <= scenario.now, case.id
        for log in {e.log for e in events}:
            ids = [e.record_id for e in events if e.log == log]
            assert ids == sorted(ids) and len(set(ids)) == len(ids), (case.id, log)
        assert {e.log for e in events} <= {"System", "Application"}


def test_sensor_series_are_time_ordered(generated: Path, cases: list[EvalCase]) -> None:
    for case in cases:
        path = _scenario_dir(generated, case) / "sensors.json"
        if path.exists():
            snaps = SENSORS.validate_json(path.read_bytes())
            stamps = [s.taken_at for s in snaps]
            assert stamps == sorted(stamps), case.id


# --------------------------------------------------------------------------- #
# coverage, split and honesty properties of the dataset
# --------------------------------------------------------------------------- #


def test_case_count_and_category_coverage(cases: list[EvalCase]) -> None:
    assert 24 <= len(cases) <= 30
    counts = Counter(c.expected.primary for c in cases)
    for category in RootCauseCategory:
        assert counts[category] >= 2, category


def test_ambiguous_and_insufficient_cases(cases: list[EvalCase]) -> None:
    ambiguous = [c for c in cases if len(c.expected.acceptable) > 1]
    assert len(ambiguous) >= 3
    assert all("ambiguous" in c.tags for c in ambiguous)
    assert all("ambiguous" not in c.tags for c in cases if c not in ambiguous)
    insufficient = [c for c in cases if c.expected.primary == "insufficient_evidence"]
    assert len(insufficient) >= 2


def test_red_herrings_present(cases: list[EvalCase]) -> None:
    herrings = [c for c in cases if "red-herring" in c.tags]
    assert len(herrings) >= 3
    # Decoys must not be a tell for any one answer: spread them across categories.
    assert len({c.expected.primary for c in herrings}) >= 6


def test_decoys_sit_near_the_incident_across_categories() -> None:
    decoy_kinds = {
        ("Application Error", 1000),
        ("Microsoft-Windows-WHEA-Logger", 17),
        ("Microsoft-Windows-DistributedCOM", 10016),
    }
    categories = set()
    for spec in SPECS:
        bundle = generate.build(spec)
        for e in bundle.events:
            near = abs((e.time - bundle.incident).total_seconds()) <= 600
            live_dump = "LiveKernelEvent" in e.message and e.data.get("P1") == "1b0"
            if near and ((e.provider, e.event_id) in decoy_kinds or live_dump):
                categories.add(spec.primary)
    assert len(categories) >= 6


def test_hdr_changes_never_coincide_with_signals() -> None:
    for spec in SPECS:
        bundle = generate.build(spec)
        anchors = [bundle.incident] + [
            bundle.incident + timedelta(minutes=s.at) for s in spec.signals
        ]
        for e in bundle.events:
            if e.provider == "Display" and e.event_id in (4123, 4127):
                gaps = [abs((e.time - a).total_seconds()) for a in anchors]
                assert min(gaps) >= generate.PROTECTED_MIN * 60, spec.id


def test_split_is_stratified(cases: list[EvalCase]) -> None:
    test_share = sum(c.split == "test" for c in cases) / len(cases)
    assert 0.25 <= test_share <= 0.40
    for category in RootCauseCategory:
        splits = {c.split for c in cases if c.expected.primary == category}
        assert splits == {"dev", "test"}, category


def test_vendor_mix(generated: Path, cases: list[EvalCase]) -> None:
    vendors: Counter[str] = Counter()
    services: set[str] = set()
    for case in cases:
        scenario = ReplayScenario.model_validate_json(
            (_scenario_dir(generated, case) / "scenario.json").read_bytes()
        )
        vendors.update({g.vendor.value for g in scenario.system.gpus})
        for e in _events(generated, case):
            m = re.fullmatch(
                r"Display driver (\w+) stopped responding and has successfully recovered\.",
                e.message,
            )
            if e.provider == "Display" and e.event_id == 4101:
                assert m, e.message
                services.add(m.group(1))
    assert {"amd", "nvidia", "intel"} <= set(vendors)
    assert {"amdkmdag", "nvlddmkm"} <= services
    assert any(s.startswith("igfx") for s in services)


def test_tagged_data_requirements(generated: Path, cases: list[EvalCase]) -> None:
    tagged = {
        t: [c for c in cases if t in c.tags]
        for t in ("needs:sensors", "needs:process", "needs:disk")
    }
    assert all(len(v) >= 2 for v in tagged.values()), {k: len(v) for k, v in tagged.items()}

    for case in tagged["needs:sensors"]:
        snaps = SENSORS.validate_json(
            (_scenario_dir(generated, case) / "sensors.json").read_bytes()
        )
        assert len(snaps) >= 10, case.id
        temps = [
            r.value
            for s in snaps
            for r in s.readings
            if r.kind == "temperature" and r.sensor == "GPU Core"
        ]
        assert temps, case.id

    thermal = [c for c in cases if c.expected.primary == "gpu_thermal"]
    for case in thermal:
        assert "needs:sensors" in case.tags
        snaps = SENSORS.validate_json(
            (_scenario_dir(generated, case) / "sensors.json").read_bytes()
        )
        temps = [
            r.value
            for s in snaps
            for r in s.readings
            if r.sensor == "GPU Core" and r.kind == "temperature"
        ]
        assert max(temps) - min(temps) >= 15, case.id  # a genuine climb, not a flat line

    for case in tagged["needs:process"]:
        snap = ProcessSnapshot.model_validate_json(
            (_scenario_dir(generated, case) / "process_snapshot.json").read_bytes()
        )
        assert snap.top_processes
    memory = [c for c in cases if c.expected.primary == "memory_pressure"]
    for case in memory:
        snap = ProcessSnapshot.model_validate_json(
            (_scenario_dir(generated, case) / "process_snapshot.json").read_bytes()
        )
        assert snap.memory.commit_used_bytes / snap.memory.commit_limit_bytes > 0.9, case.id

    for case in tagged["needs:disk"]:
        snap = ProcessSnapshot.model_validate_json(
            (_scenario_dir(generated, case) / "process_snapshot.json").read_bytes()
        )
        assert max(d.queue_length for d in snap.disks) > 5, case.id
        found = {(e.provider, e.event_id) for e in _events(generated, case)}
        assert found & STORAGE_WARNINGS, case.id


def test_no_answer_leaks_into_scenario_or_question(generated: Path, cases: list[EvalCase]) -> None:
    words = {c.value for c in RootCauseCategory} | {
        "thermal",
        "insufficient",
        "red herring",
        "ambiguous",
    }
    for case in cases:
        meta = (
            (_scenario_dir(generated, case) / "scenario.json").read_text(encoding="utf-8").lower()
        )
        question = case.question.lower()
        for word in words:
            assert word.replace("_", " ") not in meta.replace("_", " "), (case.id, word)
            assert word not in question, (case.id, word)
        assert "utc" not in question, case.id


# --------------------------------------------------------------------------- #
# realism of the event records
# --------------------------------------------------------------------------- #


def test_bugcheck_events_are_consistent(generated: Path, cases: list[EvalCase]) -> None:
    seen_codes = 0
    for case in cases:
        events = _events(generated, case)
        kp41 = [
            e for e in events if e.provider == "Microsoft-Windows-Kernel-Power" and e.event_id == 41
        ]
        wer = [
            e
            for e in events
            if e.provider == "Microsoft-Windows-WER-SystemErrorReporting" and e.event_id == 1001
        ]
        for e in kp41:
            assert e.level == "critical"
            assert e.data["BugcheckCode"].isdigit(), e.data
            assert e.data["PowerButtonTimestamp"].isdigit()
        kp_codes = sorted(
            int(e.data["BugcheckCode"]) for e in kp41 if e.data["BugcheckCode"] != "0"
        )
        wer_codes = []
        for e in wer:
            m = re.match(
                r"The computer has rebooted from a bugcheck\. The bugcheck was: 0x([0-9a-f]{8}) \(",
                e.message,
            )
            assert m, e.message
            wer_codes.append(int(m.group(1), 16))
            assert e.level == "error"
        assert sorted(wer_codes) == kp_codes, case.id
        # 0x117 and 0x141 are live dumps only; they never appear as a real bugcheck.
        assert not set(wer_codes) & {0x117, 0x141}, case.id
        seen_codes += len(wer_codes)
        if kp41:
            unexpected = [e for e in events if e.provider == "EventLog" and e.event_id == 6008]
            assert len(unexpected) == len(kp41), case.id
    assert seen_codes >= 8


def test_live_kernel_events_follow_spec_format(generated: Path, cases: list[EvalCase]) -> None:
    pattern = re.compile(
        r"Fault bucket .*, type \d+ Event Name: LiveKernelEvent Response: Not available "
        r"Cab Id: 0 Problem signature: P1: ([0-9a-f]+) P2: "
    )
    found = 0
    for case in cases:
        for e in _events(generated, case):
            if "LiveKernelEvent" in e.message:
                assert e.log == "Application" and e.provider == "Windows Error Reporting"
                assert e.event_id == 1001
                m = pattern.match(e.message)
                assert m, e.message
                assert e.data["P1"] == m.group(1)
                found += 1
    assert found >= 5


def test_background_noise_is_present_and_dominant(cases: list[EvalCase]) -> None:
    expected_noise = {
        ("Service Control Manager", 7036),
        ("Microsoft-Windows-DistributedCOM", 10016),
        ("EventLog", 6005),
        ("Microsoft-Windows-Kernel-Boot", 27),
    }
    total_scm, total_hours = 0, 0.0
    for spec, case in zip(SPECS, cases, strict=True):
        bundle = generate.build(spec)
        found = {(e.provider, e.event_id) for e in bundle.events}
        if "log-cleared" in case.tags:
            system = [e for e in bundle.events if e.log == "System"]
            assert (system[0].provider, system[0].event_id) == ("Microsoft-Windows-Eventlog", 104)
            assert {"Security-SPP", "SecurityCenter"} <= {
                e.provider.split("Windows-")[-1] for e in bundle.events
            }
        else:
            assert expected_noise <= found, case.id
        assert bundle.noise_count / len(bundle.events) >= 0.7, case.id
        total_hours += (bundle.scenario.now - bundle.events[0].time).total_seconds() / 3600
        total_scm += sum(
            e.provider == "Service Control Manager" and e.event_id == 7036 for e in bundle.events
        )
    # Steady state is 3/h plus boot bursts; the dataset-wide rate must stay in a sane band
    # so that noise is neither absent nor inflated.
    assert 2.0 <= total_scm / total_hours <= 8.0, total_scm / total_hours


def test_utc_offsets_vary(generated: Path, cases: list[EvalCase]) -> None:
    offsets = set()
    for case in cases:
        scenario = ReplayScenario.model_validate_json(
            (_scenario_dir(generated, case) / "scenario.json").read_bytes()
        )
        offsets.add(scenario.system.utc_offset_minutes)
        assert scenario.now.utcoffset() is not None
    assert len(offsets) >= 4


def test_cases_jsonl_lines_are_compact_json(generated: Path) -> None:
    lines = (generated / "cases.jsonl").read_text(encoding="utf-8").splitlines()
    for line in lines:
        obj = json.loads(line)
        assert list(obj) == ["id", "split", "question", "scenario", "expected", "tags", "source"]


# --------------------------------------------------------------------------- #
# recorded cases (hand-labelled scrubbed captures under scenarios/rec-*)
# --------------------------------------------------------------------------- #

RECORDED_IDS = ["rec-001", "rec-002"]


def _recorded_events(case_id: str) -> list[EventRecord]:
    path = generate.REPO_ROOT / "evals" / "scenarios" / case_id / "events.json"
    return EVENTS.validate_json(path.read_bytes())


def test_recorded_cases_follow_the_synthetic_ones(all_cases: list[EvalCase]) -> None:
    assert [c.id for c in all_cases] == [s.id for s in SPECS] + RECORDED_IDS
    for case in all_cases[len(SPECS) :]:
        assert case.source == "recorded"
        assert case.split == "test"
        assert case.scenario == f"evals/scenarios/{case.id}"
        sdir = generate.REPO_ROOT / case.scenario
        assert {p.name for p in sdir.iterdir()} <= ALLOWED_FILES
        scenario = ReplayScenario.model_validate_json((sdir / "scenario.json").read_bytes())
        assert scenario.id == case.id
        assert _recorded_events(case.id)
        assert ("ambiguous" in case.tags) == (len(case.expected.acceptable) > 1)


def test_recorded_cases_do_not_leak_answers(all_cases: list[EvalCase]) -> None:
    words = {c.value.replace("_", " ") for c in RootCauseCategory} | {"bugcheck", "0x"}
    for case in all_cases[len(SPECS) :]:
        meta = (generate.REPO_ROOT / case.scenario / "scenario.json").read_text("utf-8").lower()
        for word in words:
            assert word not in case.question.lower(), (case.id, word)
            assert word not in meta.replace("_", " "), (case.id, word)


def test_recorded_scenarios_hold_the_labelled_evidence() -> None:
    crash = _recorded_events("rec-001")
    kp41 = [e for e in crash if e.provider == "Microsoft-Windows-Kernel-Power" and e.event_id == 41]
    assert [e.data["BugcheckCode"] for e in kp41] == ["159"]  # 0x9F
    assert kp41[0].data["PowerButtonTimestamp"] == "0"
    assert any(
        e.provider == "Microsoft-Windows-WER-SystemErrorReporting" and "0x0000009f" in e.message
        for e in crash
    )

    dumps = _recorded_events("rec-002")
    live = [e for e in dumps if "LiveKernelEvent" in e.message]
    assert [e.data["P1"] for e in live] == ["1b0", "1b0", "1b0"]
    assert {e.data["P2"] for e in live} == {"2"}  # start device failed
    assert not [e for e in dumps if e.provider == "Display" and e.event_id == 4101]
    assert not [e for e in dumps if e.event_id == 41 and "Kernel-Power" in e.provider]


def test_recorded_scenarios_are_scrubbed() -> None:
    scrubber = Scrubber()
    for case_id in RECORDED_IDS:
        sdir = generate.REPO_ROOT / "evals" / "scenarios" / case_id
        for path in sdir.iterdir():
            assert scrubber.findings(json.loads(path.read_text("utf-8"))) == [], path


def test_generate_and_check_leave_recorded_dirs_alone(tmp_path: Path) -> None:
    rec = tmp_path / "scenarios" / "rec-900"
    rec.mkdir(parents=True)
    (rec / "scenario.json").write_text("hand-made", encoding="utf-8")
    generate.generate(tmp_path)
    assert (rec / "scenario.json").read_text(encoding="utf-8") == "hand-made"
    (rec / "notes.json").write_text("[]", encoding="utf-8")
    assert generate.check(tmp_path) == []


def _recorded_line(**overrides: object) -> str:
    case: dict[str, object] = {
        "id": "rec-001",
        "split": "test",
        "question": "What happened?",
        "scenario": "evals/scenarios/rec-001",
        "expected": {"primary": "power_loss", "acceptable": ["power_loss"]},
        "tags": [],
        "source": "recorded",
    }
    case.update(overrides)
    return json.dumps(case) + "\n"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"split": "dev"}, "split"),
        ({"source": "synthetic"}, "source"),
        ({"id": "syn-900", "scenario": "evals/scenarios/syn-900"}, "rec-"),
        ({"scenario": "evals/scenarios/rec-002"}, "scenario must be"),
        ({"id": "rec-999", "scenario": "evals/scenarios/rec-999"}, "missing"),
    ],
)
def test_load_recorded_rejects_bad_cases(
    tmp_path: Path, overrides: dict[str, object], message: str
) -> None:
    path = tmp_path / "recorded.jsonl"
    path.write_text(_recorded_line(**overrides), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        generate.load_recorded(path)


def test_load_recorded_without_a_file(tmp_path: Path) -> None:
    assert generate.load_recorded(tmp_path / "absent.jsonl") == []
