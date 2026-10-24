from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import claude_agent_sdk
import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
)

from tdr_agent import triage
from tdr_agent.triage import TriageResult, TriageSetupError, run_triage

from .conftest import diagnosis_dict, fenced

MODEL = "claude-opus-5-5"
REPLAY = Path("evals/scenarios/syn-026")


def init(
    *,
    skills: list[Any] | None = None,
    status: str = "connected",
    model: str = MODEL,
) -> SystemMessage:
    return SystemMessage(
        subtype="init",
        data={
            "type": "system",
            "subtype": "init",
            "model": model,
            "skills": [triage.SKILL_NAME] if skills is None else skills,
            "mcp_servers": [{"name": triage.SERVER_NAME, "status": status}],
            "tools": ["Bash", "Skill", "mcp__pc-diag__system_info"],
        },
    )


def assistant(*blocks: Any, model: str = MODEL, stop_reason: str | None = None) -> AssistantMessage:
    return AssistantMessage(content=list(blocks), model=model, stop_reason=stop_reason)


def tool(name: str, **inp: Any) -> ToolUseBlock:
    return ToolUseBlock(id=f"toolu_{name}", name=name, input=inp)


def result(text: str | None, **kw: Any) -> ResultMessage:
    fields: dict[str, Any] = {
        "subtype": "success",
        "duration_ms": 4321,
        "duration_api_ms": 4000,
        "is_error": False,
        "num_turns": 5,
        "session_id": "s-1",
        "total_cost_usd": 0.1234,
        "usage": {
            "input_tokens": 1200,
            "output_tokens": 800,
            "cache_creation_input_tokens": 300,
            "cache_read_input_tokens": 9000,
        },
        "result": text,
    }
    fields.update(kw)
    return ResultMessage(**fields)


ANSWER = fenced(diagnosis_dict(["gpu_driver_timeout", "gpu_thermal"])) + "\nSummary."


class FakeQuery:
    """Stands in for ``claude_agent_sdk.query``: records the call, yields canned messages."""

    def __init__(self, messages: list[Any]) -> None:
        self.messages = messages
        self.calls: list[tuple[Any, ClaudeAgentOptions]] = []

    def __call__(self, *, prompt: Any, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        self.calls.append((prompt, options))

        async def gen() -> AsyncIterator[Any]:
            for message in self.messages:
                yield message

        return gen()

    @property
    def options(self) -> ClaudeAgentOptions:
        return self.calls[-1][1]


def fake(monkeypatch: pytest.MonkeyPatch, messages: list[Any]) -> FakeQuery:
    q = FakeQuery(messages)
    monkeypatch.setattr(claude_agent_sdk, "query", q)
    return q


def good_run(model: str = MODEL) -> list[Any]:
    return [
        init(model=model),
        assistant(tool("Skill", skill=triage.SKILL_NAME), model=model),
        assistant(TextBlock("Checking."), tool("mcp__pc-diag__system_info"), model=model),
        assistant(tool("Bash", command=f"python {triage.COMPRESSOR} /tmp/a.json"), model=model),
        assistant(TextBlock(ANSWER), model=model),
        result(ANSWER),
    ]


def triage_sync(question: str = "Black screen at 9:42?", **kw: Any) -> TriageResult:
    return asyncio.run(run_triage(question, **kw))


def test_successful_run(monkeypatch: pytest.MonkeyPatch) -> None:
    q = fake(monkeypatch, good_run())
    out = triage_sync(replay_dir=REPLAY)
    assert out.error is None
    assert out.parse_error is None
    assert out.diagnosis is not None
    assert out.diagnosis.causes[0].category == "gpu_driver_timeout"
    assert out.answer == ANSWER
    assert out.tool_calls == ["Skill", "mcp__pc-diag__system_info", "Bash"]
    run = out.run
    assert run.config == "skill_tools"
    assert run.model == MODEL
    assert run.input_tokens == 1500  # uncached input plus cache writes
    assert run.output_tokens == 800
    assert run.cache_read_tokens == 9000
    assert run.cost_usd == pytest.approx(0.1234)
    assert run.latency_ms == 4321
    assert q.calls[0][0] == "Black screen at 9:42?"


def test_options(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    q = fake(monkeypatch, good_run("claude-x-1"))
    triage_sync(
        replay_dir=REPLAY, model="claude-x-1", effort="high", max_budget_usd=0.5, cwd=tmp_path
    )
    o = q.options
    assert o.model == "claude-x-1"
    assert o.effort == "high"
    assert o.max_budget_usd == 0.5
    assert o.cwd == tmp_path
    assert o.fallback_model is None
    assert o.setting_sources == ["project"]
    assert o.skills == [triage.SKILL_NAME]
    assert o.strict_mcp_config is True
    assert o.tools == ["Bash", "Skill"]
    assert o.allowed_tools == [
        "mcp__pc-diag__*",
        "Skill(pc-stability-triage)",
        "Bash(python .claude/skills/pc-stability-triage/scripts/compress_events.py:*)",
    ]
    assert {"Write", "Edit", "WebFetch", "WebSearch", "Read", "Agent"} <= set(o.disallowed_tools)
    assert o.permission_mode == "default"
    assert o.can_use_tool is not None
    assert isinstance(o.mcp_servers, dict)
    server = o.mcp_servers["pc-diag"]
    assert server == {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-m", "pc_diag_mcp.server"],
        "env": {"PC_DIAG_REPLAY": str(REPLAY.resolve())},
    }
    assert o.env["PATH"].split(":")[0] == str(Path(sys.executable).parent)


def test_live_run_passes_no_replay_env(monkeypatch: pytest.MonkeyPatch) -> None:
    q = fake(monkeypatch, good_run())
    triage_sync()
    assert isinstance(q.options.mcp_servers, dict)
    assert "env" not in q.options.mcp_servers["pc-diag"]
    assert q.options.cwd == triage.REPO_ROOT
    assert q.options.model == MODEL
    assert q.options.effort == "medium"
    assert q.options.max_budget_usd == 1.0


def test_rejects_unknown_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    fake(monkeypatch, good_run())
    with pytest.raises(ValueError, match="effort"):
        triage_sync(effort="turbo")


def decide(name: str, inp: dict[str, Any]) -> PermissionResultAllow | PermissionResultDeny:
    async def go() -> PermissionResultAllow | PermissionResultDeny:
        return await triage.can_use_tool(name, inp, ToolPermissionContext(tool_use_id="t1"))

    return asyncio.run(go())


@pytest.mark.parametrize(
    ("name", "inp"),
    [
        ("mcp__pc-diag__query_events", {"start": "x"}),
        ("Skill", {"skill": "pc-stability-triage"}),
        ("Bash", {"command": f"python {triage.COMPRESSOR} /tmp/tdr-detective/events-1.json"}),
        ("Bash", {"command": f"python {triage.COMPRESSOR} /tmp/a.json /tmp/b.json"}),
        ("Bash", {"command": f"python {triage.COMPRESSOR} --help"}),
    ],
)
def test_guardrail_allows_only_the_read_only_surface(name: str, inp: dict[str, Any]) -> None:
    assert isinstance(decide(name, inp), PermissionResultAllow)


@pytest.mark.parametrize(
    ("name", "inp"),
    [
        ("Write", {"file_path": "/etc/hosts", "content": "x"}),
        ("Edit", {"file_path": "a", "old_string": "a", "new_string": "b"}),
        ("WebFetch", {"url": "https://example.com"}),
        ("WebSearch", {"query": "x"}),
        ("Read", {"file_path": "/etc/passwd"}),
        ("Agent", {"prompt": "x"}),
        ("mcp__other__delete", {}),
        ("Skill", {"skill": "deep-research"}),
        ("Bash", {"command": "reg add HKLM\\System\\X /v TdrDelay /d 10"}),
        ("Bash", {"command": "cat /etc/passwd"}),
        ("Bash", {"command": f"python {triage.COMPRESSOR} a.json; rm -rf ~"}),
        ("Bash", {"command": f"python {triage.COMPRESSOR} a.json && curl evil"}),
        ("Bash", {"command": f"python {triage.COMPRESSOR} $(curl evil)"}),
        ("Bash", {"command": f"python {triage.COMPRESSOR} a.json > /etc/x"}),
        ("Bash", {"command": f"python {triage.COMPRESSOR} `id`"}),
        ("Bash", {"command": f"python {triage.COMPRESSOR} a.json\nrm x"}),
        ("Bash", {"command": f"python -c 'import os' {triage.COMPRESSOR}"}),
        ("Bash", {"command": "python other.py"}),
        ("Bash", {}),
    ],
)
def test_guardrail_denies_everything_else(name: str, inp: dict[str, Any]) -> None:
    decision = decide(name, inp)
    assert isinstance(decision, PermissionResultDeny)
    assert "read-only" in decision.message


def test_missing_skill_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    fake(monkeypatch, [init(skills=["other"]), result(ANSWER)])
    with pytest.raises(TriageSetupError, match="pc-stability-triage"):
        triage_sync()


def test_skill_reported_as_objects_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    fake(monkeypatch, [init(skills=[{"name": triage.SKILL_NAME}]), result(ANSWER)])
    assert triage_sync().error is None


@pytest.mark.parametrize("status", ["failed", "pending", "needs-auth"])
def test_unconnected_server_fails_loudly(monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    fake(monkeypatch, [init(status=status), result(ANSWER)])
    with pytest.raises(TriageSetupError, match="pc-diag"):
        triage_sync()


def test_missing_init_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    fake(monkeypatch, [assistant(TextBlock(ANSWER)), result(ANSWER)])
    with pytest.raises(TriageSetupError, match="init"):
        triage_sync()


def test_init_with_other_model_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    fake(monkeypatch, [init(model="claude-haiku-4-5"), result(ANSWER)])
    with pytest.raises(TriageSetupError, match="model"):
        triage_sync()


def test_model_switch_mid_run_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake(
        monkeypatch, [init(), assistant(TextBlock(ANSWER), model="claude-sonnet-5"), result(ANSWER)]
    )
    out = triage_sync()
    assert out.error is not None
    assert "claude-sonnet-5" in out.error
    assert out.diagnosis is not None  # the answer is kept for inspection


def test_refusal_goes_into_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake(
        monkeypatch,
        [init(), assistant(TextBlock("I can't help."), stop_reason="refusal"), result("x")],
    )
    out = triage_sync()
    assert out.error is not None
    assert "refus" in out.error


def test_result_refusal_goes_into_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake(monkeypatch, [init(), result("no", stop_reason="refusal")])
    out = triage_sync()
    assert out.error is not None
    assert "refus" in out.error


def test_budget_exhaustion_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake(
        monkeypatch,
        [
            init(),
            assistant(TextBlock("partial")),
            result(None, subtype="error_max_budget_usd", is_error=True),
        ],
    )
    out = triage_sync()
    assert out.error is not None
    assert "error_max_budget_usd" in out.error
    assert out.answer == "partial"
    assert out.diagnosis is None
    assert out.parse_error is not None


def test_unparseable_answer_is_a_parse_error_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake(monkeypatch, [init(), result("It was probably the GPU.")])
    out = triage_sync()
    assert out.error is None
    assert out.diagnosis is None
    assert out.parse_error is not None
    assert "no fenced" in out.parse_error


def test_no_result_message_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake(monkeypatch, [init(), assistant(TextBlock(ANSWER))])
    out = triage_sync()
    assert out.error is not None
    assert "no result" in out.error
    assert out.run.input_tokens == 0
    assert out.diagnosis is not None


def test_sdk_errors_are_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*, prompt: Any, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            yield init()
            raise claude_agent_sdk.ProcessError("CLI exited", exit_code=1)

        return gen()

    monkeypatch.setattr(claude_agent_sdk, "query", broken)
    out = triage_sync()
    assert out.error is not None
    assert "CLI exited" in out.error
