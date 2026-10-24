from __future__ import annotations

import asyncio
import os
import re
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
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
    assert o.tools == ["Bash", "Skill", "Read"]
    assert o.allowed_tools == [*triage.PC_DIAG_TOOLS, "Skill(pc-stability-triage)"]
    assert not [rule for rule in o.allowed_tools if "*" in rule]
    # No Bash allow rule: every Bash call must reach the strict can_use_tool parser.
    assert not [rule for rule in o.allowed_tools if rule.startswith("Bash")]
    assert {"Write", "Edit", "WebFetch", "WebSearch", "Glob", "Grep", "Agent"} <= set(
        o.disallowed_tools
    )
    assert "Read" not in o.disallowed_tools  # gated to the skill directory instead
    assert o.permission_mode == "default"
    assert o.can_use_tool is not None
    assert isinstance(o.mcp_servers, dict)
    server = o.mcp_servers["pc-diag"]
    assert server["type"] == "stdio"
    assert server["command"] == sys.executable
    assert server["args"] == ["-m", "pc_diag_mcp.server"]
    assert server["env"]["PC_DIAG_REPLAY"] == str(REPLAY.resolve())
    assert o.env["PATH"].split(os.pathsep)[0] == str(Path(sys.executable).parent)
    assert o.env["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] == "1"
    assert set(o.env) == {"PATH", "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"}


def test_live_run_passes_no_replay_env(monkeypatch: pytest.MonkeyPatch) -> None:
    q = fake(monkeypatch, good_run())
    triage_sync()
    assert isinstance(q.options.mcp_servers, dict)
    assert "PC_DIAG_REPLAY" not in q.options.mcp_servers["pc-diag"]["env"]
    assert q.options.cwd == triage.REPO_ROOT
    assert q.options.model == MODEL
    assert q.options.effort == "medium"
    assert q.options.max_budget_usd == 1.0


def test_mcp_server_env_is_minimal_and_credential_free(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = {
        "ANTHROPIC_API_KEY": "sk-ant-api03-secret",
        "ANTHROPIC_AUTH_TOKEN": "tok",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth",
        "AWS_SECRET_ACCESS_KEY": "aws",
        "GITHUB_TOKEN": "gh",
        "UNRELATED": "x",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("TMPDIR", "scratch-temp")
    monkeypatch.setenv("WSL_INTEROP", "/run/WSL/1_interop")
    env = triage.mcp_server_env(REPLAY)
    assert not set(secrets) & set(env)
    assert not any(value in env.values() for value in secrets.values())
    assert set(env) <= {*triage._SERVER_ENV_PASSTHROUGH, "PATH", "PC_DIAG_REPLAY"}
    assert env["TMPDIR"] == "scratch-temp"
    assert env["WSL_INTEROP"] == "/run/WSL/1_interop"
    assert env["PC_DIAG_REPLAY"] == str(REPLAY.resolve())
    assert "PC_DIAG_REPLAY" not in triage.mcp_server_env(None)


def test_rejects_unknown_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    fake(monkeypatch, good_run())
    with pytest.raises(ValueError, match="effort"):
        triage_sync(effort="turbo")


def decide(
    name: str, inp: dict[str, Any], cwd: Path = triage.REPO_ROOT
) -> PermissionResultAllow | PermissionResultDeny:
    async def go() -> PermissionResultAllow | PermissionResultDeny:
        guard = triage.Guard(cwd, cwd / "tmp" / "tdr-detective")
        return await guard.can_use_tool(name, inp, ToolPermissionContext(tool_use_id="t1"))

    return asyncio.run(go())


def hook(name: str, inp: dict[str, Any], cwd: Path = triage.REPO_ROOT) -> Any:
    hook_input: Any = {
        "hook_event_name": "PreToolUse",
        "session_id": "s-1",
        "transcript_path": "transcript.jsonl",
        "cwd": ".",
        "tool_name": name,
        "tool_input": inp,
        "tool_use_id": "t1",
    }
    guard = triage.Guard(cwd, cwd / "tmp" / "tdr-detective")
    return asyncio.run(guard.pre_tool_use(hook_input, "t1", {"signal": None}))


SAVED = "tmp/tdr-detective/events-a1.json"
SAVED_B = "tmp/tdr-detective/events-b2.json"
SAVED_VIA_DOTDOT = "tmp/tdr-detective/../tdr-detective/events-a1.json"


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """A cwd whose pc-diag temp dir holds two saved event files."""
    temp_dir = tmp_path / "tmp" / "tdr-detective"
    temp_dir.mkdir(parents=True)
    for rel in (SAVED, SAVED_B):
        (tmp_path / rel).write_text("[]", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    ("name", "inp"),
    [
        ("mcp__pc-diag__query_events", {"start": "x"}),
        ("Skill", {"skill": "pc-stability-triage"}),
        ("Bash", {"command": f"python {triage.COMPRESSOR} {SAVED}"}),
        ("Bash", {"command": f"python {triage.COMPRESSOR} {SAVED} {SAVED_B}"}),
        ("Bash", {"command": f"python {triage.COMPRESSOR} --help"}),
        ("Bash", {"command": f"python3 {triage.COMPRESSOR} {SAVED} --prefix F --burst-gap 30"}),
        ("Bash", {"command": f"python {triage.COMPRESSOR} {SAVED} --max-noise-lines=40"}),
        ("Bash", {"command": f"python {triage.COMPRESSOR} {SAVED_VIA_DOTDOT}"}),
    ],
)
def test_guardrail_allows_only_the_read_only_surface(
    sandbox: Path, name: str, inp: dict[str, Any]
) -> None:
    assert isinstance(decide(name, inp, sandbox), PermissionResultAllow)
    assert hook(name, inp, sandbox) == {}  # the PreToolUse hook makes no decision


def test_compressor_accepts_the_absolute_temp_path(sandbox: Path) -> None:
    absolute = (sandbox / SAVED).resolve().as_posix()  # X:/... on native Windows
    command = f"python {triage.COMPRESSOR} {absolute}"
    assert triage.is_compressor_command(command, sandbox, sandbox / "tmp" / "tdr-detective")


def test_saved_events_path_round_trips_through_the_guard(tmp_path: Path) -> None:
    from pc_diag_mcp.collectors import ReplayCollector
    from pc_diag_mcp.tools import SavedEventsResult, query_events
    from pc_diag_mcp.tools.events import events_temp_dir

    result = query_events(
        ReplayCollector(REPLAY),
        datetime(2026, 10, 15, 1, 30, tzinfo=UTC),
        datetime(2026, 10, 15, 2, 0, tzinfo=UTC),
        save_to="tempfile",
        temp_dir=tmp_path,
    )
    assert isinstance(result, SavedEventsResult)
    assert "\\" not in result.path  # forward slashes on every OS
    command = f"python {triage.COMPRESSOR} {result.path}"
    guard_dir = events_temp_dir(tmp_path)
    assert triage.is_compressor_command(command, triage.REPO_ROOT, guard_dir)


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Simulate native Windows: drive Q: maps onto tmp_path; returns the temp dir."""
    real_resolve = triage._resolve

    def resolve(arg: str, cwd: Path) -> Path:
        if arg[:2].upper() == "Q:":
            return real_resolve(str(tmp_path) + arg[2:], cwd)
        return real_resolve(arg, cwd)

    monkeypatch.setattr(triage, "_on_windows", lambda: True)
    monkeypatch.setattr(triage, "_resolve", resolve)
    temp_dir = tmp_path / "Users" / "RUNNER~1" / "AppData" / "Local" / "Temp" / "tdr-detective"
    temp_dir.mkdir(parents=True)
    (temp_dir / "events-k3j.json").write_text("[]", encoding="utf-8")
    return temp_dir


WIN_SAVED = "Q:/Users/RUNNER~1/AppData/Local/Temp/tdr-detective/events-k3j.json"


def test_windows_drive_path_with_forward_slashes_is_accepted(windows: Path) -> None:
    command = f"python {triage.COMPRESSOR} {WIN_SAVED} --prefix E"
    assert triage.is_compressor_command(command, triage.REPO_ROOT, windows)


@pytest.mark.parametrize(
    "arg",
    [
        WIN_SAVED.replace("/", "\\"),  # backslashes
        "Q:" + WIN_SAVED[2:].replace("/", "\\", 1),
        "//?/" + WIN_SAVED,  # device namespace, slash spelling
        "\\\\?\\" + WIN_SAVED.replace("/", "\\"),
        "//./pipe/events-k3j.json",
        "\\\\.\\pipe\\events-k3j.json",
        "//host/share/events-k3j.json",
        "Q:Users/RUNNER~1/AppData/Local/Temp/tdr-detective/events-k3j.json",  # drive-relative
        "Q:/Users/RUNNER~1/AppData/Local/Temp/events-k3j.json",  # outside the temp dir
        "~/events-k3j.json",  # Bash would expand this
        "Q:/x/~/events-k3j.json",
    ],
)
def test_windows_paths_outside_the_rule_are_denied(windows: Path, arg: str) -> None:
    command = f"python {triage.COMPRESSOR} {arg}"
    assert not triage.is_compressor_command(command, triage.REPO_ROOT, windows)


def test_drive_paths_are_denied_off_windows(windows: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(triage, "_on_windows", lambda: False)
    command = f"python {triage.COMPRESSOR} {WIN_SAVED}"
    assert not triage.is_compressor_command(command, triage.REPO_ROOT, windows)


@pytest.mark.parametrize(
    "arg",
    [
        "//attacker/share/tdr-detective/events-a1.json",  # UNC: NTLM over SMB
        "C:/Users/x/AppData/Local/Temp/tdr-detective/events-a1.json",
        "c:events-a1.json",
        "tmp/tdr-detective/other.json",  # wrong name
        "tmp/tdr-detective/events-missing.json",
        "tmp/events-a1.json",  # outside the temp dir
        "tmp/tdr-detective/nested/events-n.json",
        "/dev/zero",
        "/etc/passwd",
        "tmp/tdr-detective/events-link.json",  # symlink pointing out
        "tmp/tdr-detective/events-dir.json",  # a directory
        "tmp/tdr-detective/events-fifo.json",  # a FIFO, where supported
    ],
)
def test_compressor_reads_only_saved_event_files(sandbox: Path, arg: str) -> None:
    temp_dir = sandbox / "tmp" / "tdr-detective"
    (sandbox / "tmp" / "events-a1.json").write_text("[]", encoding="utf-8")
    (temp_dir / "nested").mkdir()
    (temp_dir / "nested" / "events-n.json").write_text("[]", encoding="utf-8")
    (temp_dir / "other.json").write_text("[]", encoding="utf-8")
    outside = sandbox / "secret.json"
    outside.write_text("[]", encoding="utf-8")
    (temp_dir / "events-link.json").symlink_to(outside)
    (temp_dir / "events-dir.json").mkdir()
    if hasattr(os, "mkfifo"):
        os.mkfifo(temp_dir / "events-fifo.json")
    command = f"python {triage.COMPRESSOR} {arg}"
    assert isinstance(decide("Bash", {"command": command}, sandbox), PermissionResultDeny)
    assert (
        hook("Bash", {"command": command}, sandbox)["hookSpecificOutput"]["permissionDecision"]
        == "deny"
    )


@pytest.mark.parametrize(
    "command",
    [
        f"python {triage.COMPRESSOR} \\\\attacker\\share\\events-a1.json",
        f"python {triage.COMPRESSOR} tmp\\tdr-detective\\events-a1.json",
        f"python {triage.COMPRESSOR} \\\\?\\C:\\x\\events-a1.json",
    ],
)
def test_compressor_rejects_backslash_paths(sandbox: Path, command: str) -> None:
    assert isinstance(decide("Bash", {"command": command}, sandbox), PermissionResultDeny)


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
        ("mcp__pc-diag__format_disk", {}),  # not a registered pc-diag tool
        ("mcp__pc-diag__", {}),
        ("Skill", {"command": "pc-stability-triage"}),  # only the skill key counts
        ("Skill", {"skill": "pc-stability-triage-evil"}),
        ("Skill", {}),
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
        ("Bash", {"command": f"python {triage.COMPRESSOR} a.json --map /tmp/x"}),
        ("Bash", {"command": f"python {triage.COMPRESSOR} a.json --map=/home/u/profile"}),
        ("Bash", {"command": f"python {triage.COMPRESSOR} --map x a.json"}),
        ("Bash", {"command": f"python {triage.COMPRESSOR} a.json --ma x"}),  # argparse prefix
        ("Bash", {"command": f"python {triage.COMPRESSOR} a.json --prefix --map"}),
        ("Bash", {"command": f"python {triage.COMPRESSOR} a.json --burst-gap 1e9"}),
        ("Bash", {"command": f"python {triage.COMPRESSOR} -m"}),
        ("Bash", {"command": f"python {triage.COMPRESSOR} -- --map x"}),
        ("Bash", {"command": f"python {triage.COMPRESSOR} a.json --prefix E1"}),
        ("Bash", {"command": f"python {triage.COMPRESSOR} a.json --prefix=-x"}),
        ("Bash", {"command": f"python /abs/{triage.COMPRESSOR} a.json"}),
        ("Bash", {}),
    ],
)
def test_guardrail_denies_everything_else(name: str, inp: dict[str, Any]) -> None:
    decision = decide(name, inp)
    assert isinstance(decision, PermissionResultDeny)
    assert "read-only" in decision.message
    # The hook denies the same calls even if the CLI would have auto-approved them.
    output = hook(name, inp)["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "read-only" in output["permissionDecisionReason"]


def test_pc_diag_tools_match_the_server_registration() -> None:
    from fastmcp import Client

    from pc_diag_mcp.collectors import ReplayCollector
    from pc_diag_mcp.server import create_server

    async def names() -> set[str]:
        async with Client(create_server(ReplayCollector(REPLAY))) as client:
            return {t.name for t in await client.list_tools()}

    registered = asyncio.run(names())
    assert len(registered) == 8
    assert set(triage.PC_DIAG_TOOLS) == {f"mcp__pc-diag__{n}" for n in registered}


@pytest.mark.parametrize(
    "hook_input",
    [
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": "rm -rf ~"},
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": None},
        {"hook_event_name": "PreToolUse", "tool_name": "mcp__pc-diag__system_info"},
        {"hook_event_name": "PreToolUse", "tool_input": {}},
        {"tool_name": "Write", "tool_input": {}},
        None,
    ],
)
def test_hook_fails_closed_on_malformed_input(hook_input: Any) -> None:
    guard = triage.Guard(triage.REPO_ROOT)
    out: Any = asyncio.run(guard.pre_tool_use(hook_input, "t1", {"signal": None}))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_can_use_tool_denies_non_dict_input() -> None:
    guard = triage.Guard(triage.REPO_ROOT)
    bad: Any = "python x"
    decision = asyncio.run(guard.can_use_tool("Bash", bad, ToolPermissionContext(tool_use_id="t")))
    assert isinstance(decision, PermissionResultDeny)


def test_hook_is_registered_for_every_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    q = fake(monkeypatch, good_run())
    triage_sync()
    assert q.options.hooks is not None
    (matcher,) = q.options.hooks["PreToolUse"]
    assert matcher.matcher is None
    (callback,) = matcher.hooks
    guard = triage.Guard(triage.REPO_ROOT)
    assert callback.__self__ == guard  # type: ignore[attr-defined]
    assert q.options.can_use_tool is not None
    assert q.options.can_use_tool.__self__ == guard  # type: ignore[attr-defined]


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


def _model_usage(inp: int, out: int, read: int, write: int, cost: float) -> dict[str, Any]:
    return {
        "inputTokens": inp,
        "outputTokens": out,
        "cacheReadInputTokens": read,
        "cacheCreationInputTokens": write,
        "webSearchRequests": 0,
        "costUSD": cost,
        "contextWindow": 200000,
        "maxOutputTokens": 32000,
    }


def test_run_stats_sum_model_usage_over_all_models(monkeypatch: pytest.MonkeyPatch) -> None:
    model_usage = {
        MODEL: _model_usage(1000, 700, 20000, 4000, 0.3),
        # The harness's own helper calls count too: no overhead is subtracted.
        "claude-haiku-4-5": _model_usage(50, 10, 0, 5, 0.001),
    }
    fake(monkeypatch, [init(), result(ANSWER, model_usage=model_usage, total_cost_usd=0.301)])
    run = triage_sync().run
    assert run.input_tokens == 1000 + 4000 + 50 + 5  # uncached input + cache writes
    assert run.output_tokens == 700 + 10
    assert run.cache_read_tokens == 20000
    assert run.cost_usd == pytest.approx(0.301)  # total_cost_usd, not the per-model sum


def test_run_stats_fall_back_to_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    fake(monkeypatch, [init(), result(ANSWER, model_usage=None)])
    run = triage_sync().run
    assert (run.input_tokens, run.output_tokens, run.cache_read_tokens) == (1200 + 300, 800, 9000)


@pytest.mark.parametrize(
    "content",
    [
        "{}",
        '{"$schema": "https://json.schemastore.org/claude-code-settings.json"}',
        '{"permissions": {"deny": ["WebFetch", "Bash(curl:*)"]}}',
        '{"$schema": "x", "permissions": {}}',
    ],
)
def test_narrowing_project_settings_are_accepted(tmp_path: Path, content: str) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(content, encoding="utf-8")
    triage.check_project_settings(tmp_path)


@pytest.mark.parametrize(
    ("content", "named"),
    [
        ('{"env": {"X": "1"}}', "env"),
        ('{"hooks": {}}', "hooks"),
        ('{"model": "claude-haiku-4-5"}', "model"),
        ('{"apiKeyHelper": "/bin/x"}', "apiKeyHelper"),
        ('{"enabledPlugins": {}}', "enabledPlugins"),
        ('{"permissions": {"allow": []}}', "permissions.allow"),
        ('{"permissions": {"deny": [], "ask": ["Bash"]}}', "permissions.ask"),
        ('{"permissions": {"deny": "Write"}}', "permissions.deny"),
        ('{"permissions": []}', "permissions"),
        ("[]", "not a settings object"),
        ("{not json", "cannot read"),
    ],
)
def test_other_project_settings_fail_closed(tmp_path: Path, content: str, named: str) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(content, encoding="utf-8")
    with pytest.raises(TriageSetupError, match=re.escape(named)):
        triage.check_project_settings(tmp_path)


def test_widening_project_settings_fail_loudly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake(monkeypatch, good_run())
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{"permissions": {"allow": ["Bash"]}}', encoding="utf-8")
    with pytest.raises(TriageSetupError, match=r"permissions\.allow"):
        triage_sync(cwd=tmp_path)
    settings.write_text('{"permissions": {"deny": ["WebFetch"]}}', encoding="utf-8")
    triage_sync(cwd=tmp_path)  # deny rules only narrow the guardrail
    settings.write_text('{"permissions": {"defaultMode": "bypassPermissions"}}', encoding="utf-8")
    with pytest.raises(TriageSetupError, match="defaultMode"):
        triage_sync(cwd=tmp_path)
    settings.write_text("[]", encoding="utf-8")
    with pytest.raises(TriageSetupError, match="not a settings object"):
        triage_sync(cwd=tmp_path)


def test_repository_project_settings_do_not_widen_the_guardrail() -> None:
    triage.check_project_settings(triage.REPO_ROOT)


@pytest.mark.parametrize(
    ("reported", "same"),
    [
        (MODEL, True),
        (f"{MODEL}-20261001", True),
        (f"{MODEL}-5", False),
        (f"{MODEL}-fast", False),
        ("claude-sonnet-5", False),
        (None, False),
    ],
)
def test_model_identity_is_exact(reported: str | None, same: bool) -> None:
    assert triage._same_model(MODEL, reported) is same


SKILL_DIR = triage.REPO_ROOT / ".claude" / "skills" / triage.SKILL_NAME


def _read_decisions(path: str, cwd: Path = triage.REPO_ROOT) -> tuple[bool, bool]:
    """(can_use_tool allows, hook stays silent) for a Read of ``path``."""
    inp = {"file_path": path}
    return isinstance(decide("Read", inp, cwd), PermissionResultAllow), hook("Read", inp, cwd) == {}


@pytest.mark.parametrize(
    "path",
    [
        str(SKILL_DIR / "reference" / "bugcheck_table.md"),
        str(SKILL_DIR / "reference" / "tdr_checklist.md"),
        f".claude/skills/{triage.SKILL_NAME}/reference/tdr_checklist.md",
        str(SKILL_DIR / "reference" / ".." / "SKILL.md"),  # still inside the skill
    ],
)
def test_read_allows_the_skill_reference_files(path: str) -> None:
    assert _read_decisions(path) == (True, True)


@pytest.mark.parametrize(
    "path",
    [
        str(SKILL_DIR / "reference" / ".." / ".." / ".." / ".." / "pyproject.toml"),
        f".claude/skills/{triage.SKILL_NAME}/../../../evals/cases.jsonl",
        str(SKILL_DIR) + "-evil/reference/x.md",  # sibling sharing the prefix
        "/etc/passwd",
        str(triage.REPO_ROOT / "evals" / "cases.jsonl"),
        "",
        "relative/elsewhere.md",
    ],
)
def test_read_denies_paths_outside_the_skill(path: str) -> None:
    assert _read_decisions(path) == (False, False)


def test_read_denies_symlink_escaping_the_skill(tmp_path: Path) -> None:
    skill = tmp_path / ".claude" / "skills" / triage.SKILL_NAME / "reference"
    skill.mkdir(parents=True)
    (skill / "ok.md").write_text("fine", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("outside", encoding="utf-8")
    (skill / "link.md").symlink_to(secret)
    (skill.parent / "linkdir").symlink_to(tmp_path)
    assert _read_decisions(str(skill / "ok.md"), tmp_path) == (True, True)
    assert _read_decisions(str(skill / "link.md"), tmp_path) == (False, False)
    assert _read_decisions(str(skill.parent / "linkdir" / "secret.txt"), tmp_path) == (False, False)


@pytest.mark.parametrize(
    ("name", "inp"),
    [
        ("Glob", {"pattern": f".claude/skills/{triage.SKILL_NAME}/**"}),
        ("Grep", {"pattern": "x", "path": str(SKILL_DIR)}),
        ("Read", {"path": str(SKILL_DIR / "SKILL.md")}),  # wrong key: no file_path
    ],
)
def test_search_tools_stay_denied_even_inside_the_skill(name: str, inp: dict[str, Any]) -> None:
    assert isinstance(decide(name, inp), PermissionResultDeny)
    assert hook(name, inp)["hookSpecificOutput"]["permissionDecision"] == "deny"
