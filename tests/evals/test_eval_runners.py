"""Tests for the raw_dump and skill_tools runners, with a fake Anthropic client and a fake
``tdr_agent`` package. Nothing here talks to a network."""

from __future__ import annotations

import json
import re
import sys
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from evals.pricing import cost_usd
from evals.runners.prompts import RAW_DUMP_SYSTEM_PROMPT
from evals.runners.raw_dump import OPTIONAL_FILES, RawDumpRunner
from evals.runners.skill_tools import SkillToolsRunner
from evals.schema import EvalCase, Runner
from pc_diag_mcp.models import Diagnosis, RunStats

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / ".claude" / "skills" / "pc-stability-triage"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


@dataclass
class FakeUsage:
    input_tokens: int = 30_000
    output_tokens: int = 2_000
    cache_read_input_tokens: int | None = 0
    cache_creation_input_tokens: int | None = 0


@dataclass
class FakeBlock:
    type: str
    text: str = ""
    thinking: str = ""


@dataclass
class FakeStopDetails:
    category: str | None
    explanation: str | None = None
    type: str = "refusal"


@dataclass
class FakeMessage:
    content: list[FakeBlock]
    stop_reason: str = "end_turn"
    usage: FakeUsage = field(default_factory=FakeUsage)
    stop_details: FakeStopDetails | None = None


class FakeStreamManager:
    def __init__(self, message: FakeMessage) -> None:
        self._message = message

    def __enter__(self) -> FakeStreamManager:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get_final_message(self) -> FakeMessage:
        return self._message


class FakeMessages:
    def __init__(self, message: FakeMessage, count: int) -> None:
        self.message = message
        self.count = count
        self.stream_calls: list[dict[str, Any]] = []
        self.count_calls: list[dict[str, Any]] = []

    def count_tokens(self, **kwargs: Any) -> Any:
        self.count_calls.append(kwargs)
        return types.SimpleNamespace(input_tokens=self.count)

    def stream(self, **kwargs: Any) -> FakeStreamManager:
        self.stream_calls.append(kwargs)
        return FakeStreamManager(self.message)

    def create(self, **kwargs: Any) -> Any:
        raise AssertionError("raw_dump must stream, not call messages.create")


class FakeClient:
    def __init__(self, message: FakeMessage | None = None, count: int = 30_000) -> None:
        default = FakeMessage(content=[FakeBlock("thinking"), FakeBlock("text", ANSWERS.text)])
        self.messages = FakeMessages(message or default, count)


class _Answers:
    """Answer builders, bound to the conftest ``diag``/``fence`` fixtures by ``_bind``."""

    diag: Callable[..., dict[str, Any]]
    fence: Callable[[dict[str, Any] | str], str]

    @property
    def text(self) -> str:
        return self.fence(self.diag(["storage_io"]))


ANSWERS = _Answers()


@pytest.fixture(autouse=True)
def _bind(diag: Callable[..., dict[str, Any]], fence: Callable[[Any], str]) -> None:
    ANSWERS.diag = diag
    ANSWERS.fence = fence


def diagnosis_dict(categories: list[str]) -> dict[str, Any]:
    return ANSWERS.diag(categories)


def fenced(payload: dict[str, Any]) -> str:
    return ANSWERS.fence(payload)


class Ticker:
    """A perf_counter stand-in that advances 1.5 s per call."""

    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        self.t += 1.5
        return self.t


def install_fake_module(
    monkeypatch: pytest.MonkeyPatch, name: str, **attrs: Any
) -> types.ModuleType:
    package, _, _ = name.partition(".")
    if package not in sys.modules or not isinstance(sys.modules[package], types.ModuleType):
        monkeypatch.setitem(sys.modules, package, types.ModuleType(package))
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)
    return module


@pytest.fixture
def fake_extract(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Install ``tdr_agent.answer.extract_diagnosis`` backed by a strict parser."""
    seen: list[str] = []

    def extract_diagnosis(answer: str) -> tuple[Diagnosis | None, str | None]:
        seen.append(answer)
        start = answer.rfind("```json")
        if start == -1:
            return None, "no fenced json block"
        body = answer[start + len("```json") :].split("```", 1)[0]
        return Diagnosis.model_validate(json.loads(body)), None

    install_fake_module(monkeypatch, "tdr_agent.answer", extract_diagnosis=extract_diagnosis)
    return seen


def case_for(scenario: str, **overrides: Any) -> EvalCase:
    payload: dict[str, Any] = {
        "id": "syn-900",
        "split": "dev",
        "question": "The game froze around 7:55 and then loaded. Why?",
        "scenario": scenario,
        "expected": {"primary": "storage_io", "acceptable": ["storage_io"]},
        "tags": ["label-tag-must-not-leak"],
        "source": "synthetic",
    }
    payload.update(overrides)
    return EvalCase.model_validate(payload)


@pytest.fixture
def raw_runner_factory(
    fake_extract: list[str],
) -> Callable[..., tuple[RawDumpRunner, FakeClient]]:
    def _make(**kwargs: Any) -> tuple[RawDumpRunner, FakeClient]:
        client = kwargs.pop("client", None) or FakeClient()
        runner = RawDumpRunner(client=client, clock=Ticker(), **kwargs)  # type: ignore[arg-type]
        return runner, client

    return _make


# --------------------------------------------------------------------------- #
# raw_dump: request shape
# --------------------------------------------------------------------------- #


def test_raw_dump_satisfies_runner_protocol(raw_runner_factory: Callable[..., Any]) -> None:
    runner, _ = raw_runner_factory()
    assert isinstance(runner, Runner)
    assert runner.name == "raw_dump"


def test_raw_dump_sends_one_streamed_request_with_fixed_parameters(
    raw_runner_factory: Callable[..., Any],
) -> None:
    runner, client = raw_runner_factory()
    runner.run(case_for("evals/scenarios/syn-002"))

    assert len(client.messages.stream_calls) == 1
    call = client.messages.stream_calls[0]
    assert set(call) == {"model", "max_tokens", "system", "messages", "thinking", "output_config"}
    assert call["model"] == "claude-opus-5-5"
    assert call["max_tokens"] == 16_000
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"] == {"effort": "medium"}
    assert call["system"] == RAW_DUMP_SYSTEM_PROMPT
    # One user turn and no assistant prefill.
    assert [m["role"] for m in call["messages"]] == ["user"]


def test_raw_dump_effort_and_model_are_passed_through(
    raw_runner_factory: Callable[..., Any],
) -> None:
    runner, client = raw_runner_factory(effort="high")
    row = runner.run(case_for("evals/scenarios/syn-002"))
    assert client.messages.stream_calls[0]["output_config"] == {"effort": "high"}
    assert client.messages.count_calls[0]["output_config"] == {"effort": "high"}
    assert row.model == "claude-opus-5-5"
    assert row.run.model == "claude-opus-5-5"


def test_raw_dump_rejects_a_model_without_known_prices() -> None:
    with pytest.raises(ValueError, match="no price"):
        RawDumpRunner(client=FakeClient(), model="claude-unknown")  # type: ignore[arg-type]


def test_raw_dump_input_is_question_plus_raw_files_verbatim(
    raw_runner_factory: Callable[..., Any],
) -> None:
    runner, client = raw_runner_factory()
    case = case_for("evals/scenarios/syn-004")
    runner.run(case)
    content = client.messages.stream_calls[0]["messages"][0]["content"]
    assert isinstance(content, str)

    assert case.question in content
    scenario = REPO_ROOT / case.scenario
    for name in ("scenario.json", "events.json", *OPTIONAL_FILES):
        text = (scenario / name).read_text(encoding="utf-8")
        assert text.strip() in content, name
    # Labels never reach the model.
    assert "label-tag-must-not-leak" not in content
    assert '"acceptable"' not in content and '"primary"' not in content


def test_raw_dump_skips_optional_files_that_do_not_exist(
    raw_runner_factory: Callable[..., Any],
) -> None:
    runner, client = raw_runner_factory()
    runner.run(case_for("evals/scenarios/syn-001"))
    content = client.messages.stream_calls[0]["messages"][0]["content"]
    assert not (REPO_ROOT / "evals/scenarios/syn-001/frametimes.json").exists()
    assert 'name="frametimes.json"' not in content
    assert 'name="events.json"' in content


def test_raw_dump_refuses_a_scenario_outside_the_repository(
    raw_runner_factory: Callable[..., Any],
) -> None:
    runner, client = raw_runner_factory()
    with pytest.raises(ValueError, match="outside"):
        runner.run(case_for("../outside"))
    assert client.messages.stream_calls == []


def test_raw_dump_missing_required_file_raises(
    raw_runner_factory: Callable[..., Any], tmp_path: Path
) -> None:
    (tmp_path / "evals" / "scenarios" / "syn-900").mkdir(parents=True)
    (tmp_path / "evals" / "scenarios" / "syn-900" / "scenario.json").write_text("{}")
    runner, client = raw_runner_factory(repo_root=tmp_path)
    with pytest.raises(FileNotFoundError):
        runner.run(case_for("evals/scenarios/syn-900"))
    assert client.messages.stream_calls == []


# --------------------------------------------------------------------------- #
# raw_dump: result mapping
# --------------------------------------------------------------------------- #


def test_raw_dump_maps_usage_cost_latency_and_diagnosis(
    raw_runner_factory: Callable[..., Any], fake_extract: list[str]
) -> None:
    usage = FakeUsage(
        input_tokens=31_000,
        output_tokens=4_000,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    answer = fenced(diagnosis_dict(["storage_io", "hardware_error"]))
    message = FakeMessage(
        content=[
            FakeBlock("thinking"),
            FakeBlock("text", "First part. "),
            FakeBlock("text", answer),
        ],
        usage=usage,
    )
    runner, _ = raw_runner_factory(client=FakeClient(message))
    row = runner.run(case_for("evals/scenarios/syn-002"))

    assert row.case_id == "syn-900"
    assert row.config == "raw_dump"
    assert row.raw_answer == "First part. " + answer
    assert fake_extract == [row.raw_answer]
    assert row.diagnosis is not None
    assert [c.category.value for c in row.diagnosis.causes] == ["storage_io", "hardware_error"]
    assert row.error is None
    assert row.run == RunStats(
        config="raw_dump",
        model="claude-opus-5-5",
        input_tokens=31_000,
        output_tokens=4_000,
        cache_read_tokens=0,
        cost_usd=cost_usd("claude-opus-5-5", input_tokens=31_000, output_tokens=4_000),
        latency_ms=1500,
    )


def test_raw_dump_counts_cache_writes_as_input_and_prices_them(
    raw_runner_factory: Callable[..., Any],
) -> None:
    usage = FakeUsage(
        input_tokens=10,
        output_tokens=20,
        cache_read_input_tokens=30,
        cache_creation_input_tokens=40,
    )
    message = FakeMessage(content=[FakeBlock("text", "no json")], usage=usage)
    runner, _ = raw_runner_factory(client=FakeClient(message))
    row = runner.run(case_for("evals/scenarios/syn-002"))
    assert row.run.input_tokens == 50
    assert row.run.cache_read_tokens == 30
    assert row.run.cost_usd == pytest.approx(
        cost_usd(
            "claude-opus-5-5",
            input_tokens=10,
            output_tokens=20,
            cache_read_tokens=30,
            cache_write_tokens=40,
        )
    )


def test_raw_dump_unparseable_answer_leaves_scoring_to_the_scorer(
    raw_runner_factory: Callable[..., Any],
) -> None:
    message = FakeMessage(content=[FakeBlock("text", "I think it was the disk.")])
    runner, _ = raw_runner_factory(client=FakeClient(message))
    row = runner.run(case_for("evals/scenarios/syn-002"))
    assert row.diagnosis is None
    # The scorer re-parses raw_answer and records the extraction error itself.
    assert row.error is None
    assert row.raw_answer == "I think it was the disk."


def test_raw_dump_refusal_is_recorded_as_error(raw_runner_factory: Callable[..., Any]) -> None:
    message = FakeMessage(
        content=[],
        stop_reason="refusal",
        stop_details=FakeStopDetails(category="cyber", explanation="declined"),
        usage=FakeUsage(input_tokens=30_000, output_tokens=5),
    )
    runner, _ = raw_runner_factory(client=FakeClient(message))
    row = runner.run(case_for("evals/scenarios/syn-002"))
    assert row.diagnosis is None
    assert row.error is not None and "refusal" in row.error and "cyber" in row.error
    assert row.run.input_tokens == 30_000
    assert row.run.cost_usd is not None and row.run.cost_usd > 0


def test_raw_dump_truncated_answer_is_flagged(raw_runner_factory: Callable[..., Any]) -> None:
    message = FakeMessage(content=[FakeBlock("text", "partial")], stop_reason="max_tokens")
    runner, _ = raw_runner_factory(client=FakeClient(message))
    row = runner.run(case_for("evals/scenarios/syn-002"))
    assert row.error is not None and "max_tokens" in row.error


def test_raw_dump_extraction_failure_keeps_billed_usage(
    raw_runner_factory: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(answer: str) -> tuple[Diagnosis | None, str | None]:
        raise RuntimeError("parser exploded")

    install_fake_module(monkeypatch, "tdr_agent.answer", extract_diagnosis=broken)
    runner, _ = raw_runner_factory()
    row = runner.run(case_for("evals/scenarios/syn-002"))
    assert row.error is not None and "parser exploded" in row.error
    assert row.run.cost_usd is not None and row.run.cost_usd > 0


def test_raw_dump_without_extractor_fails_before_any_call(
    raw_runner_factory: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "tdr_agent.answer", None)  # import raises ImportError
    runner, client = raw_runner_factory()
    with pytest.raises(ImportError):
        runner.run(case_for("evals/scenarios/syn-002"))
    assert client.messages.count_calls == [] and client.messages.stream_calls == []


# --------------------------------------------------------------------------- #
# raw_dump: token pre-check and cap
# --------------------------------------------------------------------------- #


def test_raw_dump_pre_counts_with_the_same_request(
    raw_runner_factory: Callable[..., Any],
) -> None:
    runner, client = raw_runner_factory()
    runner.run(case_for("evals/scenarios/syn-002"))
    assert len(client.messages.count_calls) == 1
    counted = client.messages.count_calls[0]
    streamed = client.messages.stream_calls[0]
    for key in ("model", "system", "messages", "thinking", "output_config"):
        assert counted[key] == streamed[key], key
    assert "max_tokens" not in counted


def test_raw_dump_count_is_cached_between_estimate_and_run(
    raw_runner_factory: Callable[..., Any],
) -> None:
    runner, client = raw_runner_factory()
    case = case_for("evals/scenarios/syn-002")
    assert runner.estimate_input_tokens(case) == 30_000
    runner.cost_ceiling_usd(case)
    runner.run(case)
    assert len(client.messages.count_calls) == 1


def test_raw_dump_refuses_cases_above_the_input_cap(
    raw_runner_factory: Callable[..., Any],
) -> None:
    runner, client = raw_runner_factory(client=FakeClient(count=250_001), max_input_tokens=250_000)
    case = case_for("evals/scenarios/syn-002")
    assert runner.cost_ceiling_usd(case) == 0.0
    row = runner.run(case)
    assert client.messages.stream_calls == []
    assert row.error is not None and "250,001" in row.error and "250,000" in row.error
    assert row.diagnosis is None
    assert row.run.input_tokens == 0 and row.run.output_tokens == 0
    assert row.run.cost_usd == 0.0


def test_raw_dump_cost_ceiling_is_input_plus_full_output_budget(
    raw_runner_factory: Callable[..., Any],
) -> None:
    runner, _ = raw_runner_factory(client=FakeClient(count=40_000))
    ceiling = runner.cost_ceiling_usd(case_for("evals/scenarios/syn-002"))
    assert ceiling == pytest.approx(
        cost_usd("claude-opus-5-5", input_tokens=40_000, output_tokens=16_000)
    )


# --------------------------------------------------------------------------- #
# Fairness: the baseline prompt carries the output contract and nothing else of the skill
# --------------------------------------------------------------------------- #


def _skill_files() -> list[Path]:
    files = sorted(p for p in SKILL_DIR.rglob("*") if p.is_file() and p.suffix in {".md", ".py"})
    assert SKILL_DIR / "SKILL.md" in files
    assert any(p.parent.name == "reference" for p in files)
    return files


def _words(text: str) -> list[str]:
    return "".join(ch if ch.isalnum() or ch == "_" else " " for ch in text.lower()).split()


def _shingles(text: str, n: int) -> set[tuple[str, ...]]:
    words = _words(text)
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def _runbook_text() -> str:
    """Everything in the skill except its output contract: runbook, decision table,
    red herrings, guardrails, references and scripts."""
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    body = skill[skill.index("## Runbook") :]
    others = [p.read_text(encoding="utf-8") for p in _skill_files() if p.name != "SKILL.md"]
    return "\n".join([body, *others])


def test_fairness_prompt_shares_no_8_word_run_with_any_skill_file() -> None:
    prompt = _shingles(RAW_DUMP_SYSTEM_PROMPT, 8)
    for path in _skill_files():
        shared = prompt & _shingles(path.read_text(encoding="utf-8"), 8)
        assert not shared, f"{path.relative_to(REPO_ROOT)}: {sorted(shared)[:3]}"


def test_fairness_prompt_has_no_event_ids_or_stop_codes_from_the_runbook() -> None:
    pattern = r"\b0x[0-9a-f]+\b|\b\d{3,5}\b"

    def codes(text: str) -> set[str]:
        # Years and the name of the ISO 8601 standard are not event IDs or stop codes.
        text = re.sub(r"iso[- ]8601", " ", text.lower())
        return {c for c in re.findall(pattern, text) if not re.fullmatch(r"20\d\d", c)}

    runbook_codes = codes(_runbook_text())
    assert {"0x116", "4101", "6008"} <= runbook_codes
    assert not runbook_codes & codes(RAW_DUMP_SYSTEM_PROMPT)


def test_fairness_prompt_names_no_runbook_tool_tag_or_signature() -> None:
    runbook = _runbook_text()
    # Contract names and the field names of the raw files themselves are not know-how.
    allowed = {c.value for c in _categories()} | _diagnosis_field_names() | _data_field_names()
    identifiers = set(re.findall(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", runbook)) - allowed
    assert {"list_tdr_incidents", "query_events", "compress_events"} <= identifiers
    tags = set(re.findall(r"\[[a-z][a-z-]+\]", runbook))
    assert "[tdr-recovered]" in tags
    signatures = {"nvlddmkm", "amdkmdag", "Kernel-Power", "WHEA", "LiveKernelEvent", "TdrDelay"}
    for token in identifiers | tags | signatures:
        assert token.lower() not in RAW_DUMP_SYSTEM_PROMPT.lower(), token


def test_prompt_carries_the_full_output_contract() -> None:
    for category in _categories():
        assert f"- {category.value}:" in RAW_DUMP_SYSTEM_PROMPT
    for name in _diagnosis_field_names():
        assert name in RAW_DUMP_SYSTEM_PROMPT, name
    assert "utc_offset_minutes" in RAW_DUMP_SYSTEM_PROMPT
    assert "```json" in RAW_DUMP_SYSTEM_PROMPT or "tagged json" in RAW_DUMP_SYSTEM_PROMPT


def _categories() -> list[Any]:
    from pc_diag_mcp.models import RootCauseCategory

    return list(RootCauseCategory)


def _data_field_names() -> set[str]:
    """Keys that appear in the scenario files the baseline is given verbatim."""
    from pc_diag_mcp import models

    names: set[str] = set()
    for model in (
        models.ReplayScenario,
        models.SystemInfo,
        models.GpuInfo,
        models.EventRecord,
        models.ProcessSnapshot,
        models.ProcessInfo,
        models.MemoryState,
        models.DiskState,
        models.SensorSnapshot,
        models.SensorReading,
        models.DriverInfo,
        models.DxDiagSummary,
        models.DisplayDevice,
        models.FrameTimeSample,
    ):
        names |= set(model.model_fields)
    assert "utc_offset_minutes" in names and "record_id" in names
    return names


def _diagnosis_field_names() -> set[str]:
    from pc_diag_mcp.models import BugReport, RankedCause, TimeWindow

    names: set[str] = set()
    for model in (Diagnosis, BugReport, RankedCause, TimeWindow):
        names |= set(model.model_fields)
    return names


# --------------------------------------------------------------------------- #
# skill_tools
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FakeTriageResult:
    answer: str
    diagnosis: Diagnosis | None
    parse_error: str | None
    run: RunStats
    tool_calls: list[str]
    error: str | None


def triage_result(
    *, categories: list[str] | None = None, error: str | None = None
) -> FakeTriageResult:
    diagnosis = Diagnosis.model_validate(diagnosis_dict(categories)) if categories else None
    return FakeTriageResult(
        answer=fenced(diagnosis_dict(categories)) if categories else "no answer",
        diagnosis=diagnosis,
        parse_error=None if categories else "no fenced json block",
        run=RunStats(
            config="agent",
            model="claude-opus-5-5",
            input_tokens=12_000,
            output_tokens=1_500,
            cache_read_tokens=40_000,
            cost_usd=0.21,
            latency_ms=42_000,
        ),
        tool_calls=["mcp__pc-diag__system_info", "mcp__pc-diag__query_events"],
        error=error,
    )


class FakeTriage:
    def __init__(self, result: FakeTriageResult) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, question: str, **kwargs: Any) -> FakeTriageResult:
        self.calls.append((question, kwargs))
        return self.result


def test_skill_tools_calls_run_triage_with_public_fields_only() -> None:
    triage = FakeTriage(triage_result(categories=["storage_io"]))
    runner = SkillToolsRunner(triage=triage, effort="high", max_budget_usd=0.75)
    assert isinstance(runner, Runner) and runner.name == "skill_tools"
    case = case_for("evals/scenarios/syn-002")
    runner.run(case)

    assert triage.calls == [
        (
            case.public()["question"],
            {
                "replay_dir": REPO_ROOT / "evals" / "scenarios" / "syn-002",
                "model": "claude-opus-5-5",
                "effort": "high",
                "max_budget_usd": 0.75,
            },
        )
    ]


def test_skill_tools_maps_triage_result_to_row() -> None:
    result = triage_result(categories=["storage_io", "hardware_error"])
    runner = SkillToolsRunner(triage=FakeTriage(result))
    row = runner.run(case_for("evals/scenarios/syn-002"))
    assert row.case_id == "syn-900"
    assert row.config == "skill_tools"
    assert row.model == "claude-opus-5-5"
    assert row.raw_answer == result.answer
    assert row.diagnosis == result.diagnosis
    assert row.error is None
    assert row.run == result.run.model_copy(update={"config": "skill_tools"})


def test_skill_tools_propagates_agent_error_but_not_parse_error() -> None:
    result = triage_result(error="agent hit max_budget_usd")
    row = SkillToolsRunner(triage=FakeTriage(result)).run(case_for("evals/scenarios/syn-002"))
    assert row.diagnosis is None
    assert row.error == "agent hit max_budget_usd"
    assert row.raw_answer == "no answer"


def test_skill_tools_loads_run_triage_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    triage = FakeTriage(triage_result(categories=["storage_io"]))
    install_fake_module(monkeypatch, "tdr_agent.triage", run_triage=triage)
    row = SkillToolsRunner().run(case_for("evals/scenarios/syn-002"))
    assert len(triage.calls) == 1
    assert row.diagnosis is not None


def test_skill_tools_estimates_and_ceiling() -> None:
    runner = SkillToolsRunner(triage=FakeTriage(triage_result()), max_budget_usd=0.6)
    case = case_for("evals/scenarios/syn-002")
    assert runner.estimate_input_tokens(case) is None
    assert runner.cost_ceiling_usd(case) == 0.6


def test_skill_tools_refuses_a_scenario_outside_the_repository() -> None:
    triage = FakeTriage(triage_result())
    with pytest.raises(ValueError, match="outside"):
        SkillToolsRunner(triage=triage).run(case_for("/etc"))
    assert triage.calls == []
