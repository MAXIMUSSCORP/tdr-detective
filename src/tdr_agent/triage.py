"""Run one triage question through Claude with the pc-stability-triage skill.

The agent sees exactly three capabilities: the ``pc-diag`` MCP tools, the project skill,
and ``python <skill>/scripts/compress_events.py``. The base tool set is limited to Bash and
Skill, every editing/network/subagent tool is disallowed, the allow rules cover only the
three capabilities, and ``can_use_tool`` denies anything else that reaches a permission
prompt. Nothing in a triage run can change the machine.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any, cast, get_args

import claude_agent_sdk
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
)
from claude_agent_sdk.types import EffortLevel, McpStdioServerConfig
from pydantic import BaseModel, ConfigDict

from pc_diag_mcp.models import Diagnosis, RunStats
from tdr_agent.answer import extract_diagnosis

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_NAME = "skill_tools"
SKILL_NAME = "pc-stability-triage"
SERVER_NAME = "pc-diag"
COMPRESSOR = f".claude/skills/{SKILL_NAME}/scripts/compress_events.py"
MCP_PREFIX = f"mcp__{SERVER_NAME}__"

BASE_TOOLS = ["Bash", "Skill"]
ALLOWED_TOOLS = [
    f"{MCP_PREFIX}*",
    f"Skill({SKILL_NAME})",
    f"Bash(python {COMPRESSOR}:*)",
]
DISALLOWED_TOOLS = [
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "Read",
    "Glob",
    "Grep",
    "LS",
    "WebFetch",
    "WebSearch",
    "Agent",
    "Task",
    "TodoWrite",
    "KillShell",
    "BashOutput",
]
EFFORTS: tuple[str, ...] = get_args(EffortLevel)

SYSTEM_PROMPT = (
    "You are TDR Detective, a read-only Windows stability triage agent. Answer the user's "
    f"question with the {SKILL_NAME} skill: load it with the Skill tool first, then follow "
    "its runbook and output contract exactly. Your only data sources are the "
    f"{MCP_PREFIX}* tools and the skill's event compressor, run as "
    f"`python {COMPRESSOR} <files>`. You cannot change the system and must never try."
)

# Arguments to the compressor: paths and flags only, no shell syntax of any kind.
_SAFE_ARG = re.compile(r"^[A-Za-z0-9_./:\\=+,@%-]+$")
_SHELL_META = re.compile(r"[;&|`$<>(){}\[\]*?!~#'\"\n\r]")


class TriageSetupError(RuntimeError):
    """The skill or the pc-diag server did not load, or the CLI chose another model."""


class TriageResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    answer: str
    diagnosis: Diagnosis | None
    parse_error: str | None
    run: RunStats
    tool_calls: list[str]
    error: str | None


def _deny(tool_name: str) -> PermissionResultDeny:
    return PermissionResultDeny(
        message=(
            f"{tool_name} is not permitted: TDR Detective is read-only. Use the "
            f"{MCP_PREFIX}* tools, the {SKILL_NAME} skill, or exactly "
            f"`python {COMPRESSOR} <saved event files>`."
        )
    )


def is_compressor_command(command: str) -> bool:
    """True only for ``python[3] <COMPRESSOR> <path-or-flag>...`` with no shell syntax."""
    if _SHELL_META.search(command):
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if len(argv) < 2 or argv[0] not in ("python", "python3") or argv[1] != COMPRESSOR:
        return False
    return all(_SAFE_ARG.match(arg) for arg in argv[2:])


async def can_use_tool(
    tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
) -> PermissionResultAllow | PermissionResultDeny:
    """Permission prompt replacement: allow the read-only surface, deny everything else."""
    if tool_name.startswith(MCP_PREFIX):
        return PermissionResultAllow()
    if tool_name == "Skill" and SKILL_NAME in (tool_input.get("skill"), tool_input.get("command")):
        return PermissionResultAllow()
    command = tool_input.get("command")
    if tool_name == "Bash" and isinstance(command, str) and is_compressor_command(command):
        return PermissionResultAllow()
    return _deny(tool_name)


def build_options(
    *,
    replay_dir: Path | None,
    model: str,
    effort: str,
    max_budget_usd: float,
    cwd: Path,
) -> ClaudeAgentOptions:
    if effort not in EFFORTS:
        raise ValueError(f"effort must be one of {list(EFFORTS)}, got {effort!r}")
    server: McpStdioServerConfig = {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-m", "pc_diag_mcp.server"],
    }
    if replay_dir is not None:
        server["env"] = {"PC_DIAG_REPLAY": str(replay_dir.resolve())}
    # The skill runs `python ...`; make that resolve to this interpreter's environment.
    path = os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("PATH", "")])
    return ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        tools=list(BASE_TOOLS),
        allowed_tools=list(ALLOWED_TOOLS),
        disallowed_tools=list(DISALLOWED_TOOLS),
        can_use_tool=can_use_tool,
        permission_mode="default",
        setting_sources=["project"],
        skills=[SKILL_NAME],
        mcp_servers={SERVER_NAME: server},
        strict_mcp_config=True,
        model=model,
        fallback_model=None,
        effort=cast(EffortLevel, effort),
        max_budget_usd=max_budget_usd,
        cwd=cwd,
        env={"PATH": path},
    )


def _same_model(requested: str, reported: str | None) -> bool:
    return reported is not None and (reported == requested or reported.startswith(requested + "-"))


def _check_init(data: dict[str, Any], model: str) -> None:
    skills = {s if isinstance(s, str) else s.get("name") for s in data.get("skills") or []}
    if SKILL_NAME not in skills:
        raise TriageSetupError(
            f"skill {SKILL_NAME!r} did not load (init reported {sorted(map(str, skills))}); "
            f"check .claude/skills/{SKILL_NAME}/SKILL.md under cwd"
        )
    servers = {s.get("name"): s.get("status") for s in data.get("mcp_servers") or []}
    if servers.get(SERVER_NAME) != "connected":
        raise TriageSetupError(
            f"MCP server {SERVER_NAME!r} is {servers.get(SERVER_NAME, 'missing')!r}, "
            "not connected; run `python -m pc_diag_mcp.server` by hand to see why"
        )
    if not _same_model(model, data.get("model")):
        raise TriageSetupError(
            f"CLI selected model {data.get('model')!r}, not {model!r}; pass a full model id"
        )


def _usage(result: ResultMessage | None, model: str, elapsed_ms: int) -> RunStats:
    if result is None:
        return RunStats(
            config=CONFIG_NAME, model=model, input_tokens=0, output_tokens=0, latency_ms=elapsed_ms
        )
    usage = result.usage or {}
    return RunStats(
        config=CONFIG_NAME,
        model=model,
        # Cache writes are fresh prompt tokens too; cache reads are reported separately.
        input_tokens=int(usage.get("input_tokens", 0))
        + int(usage.get("cache_creation_input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
        cache_read_tokens=int(usage.get("cache_read_input_tokens", 0)),
        cost_usd=result.total_cost_usd,
        latency_ms=result.duration_ms,
    )


async def run_triage(
    question: str,
    *,
    replay_dir: Path | None = None,
    model: str = "claude-opus-5-5",
    effort: str = "medium",
    max_budget_usd: float = 1.0,
    cwd: Path = REPO_ROOT,
) -> TriageResult:
    """Ask one question. Setup problems raise ``TriageSetupError``; run problems (refusal,
    budget, model switch, CLI failure) are returned in ``error``."""
    options = build_options(
        replay_dir=replay_dir, model=model, effort=effort, max_budget_usd=max_budget_usd, cwd=cwd
    )
    texts: list[str] = []
    tool_calls: list[str] = []
    errors: list[str] = []
    result: ResultMessage | None = None
    initialized = False
    started = time.monotonic()

    stream = claude_agent_sdk.query(prompt=question, options=options)
    try:
        async for message in stream:
            if isinstance(message, SystemMessage) and message.subtype == "init":
                _check_init(message.data, model)
                initialized = True
            elif isinstance(message, AssistantMessage):
                if not initialized:
                    raise TriageSetupError("no init message before the first model turn")
                if not _same_model(model, message.model):
                    errors.append(f"model switched to {message.model!r} (requested {model!r})")
                if message.error:
                    errors.append(f"assistant error: {message.error}")
                if message.stop_reason == "refusal":
                    errors.append("model refused to answer")
                for block in message.content:
                    if isinstance(block, TextBlock):
                        texts.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        tool_calls.append(block.name)
            elif isinstance(message, ResultMessage):
                result = message
    except ClaudeSDKError as exc:
        errors.append(f"agent run failed: {exc}")
    finally:
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            await aclose()

    if result is None and not errors and not initialized:
        raise TriageSetupError("the CLI produced no init message")
    if result is None:
        errors.append("no result message from the agent run")
    else:
        if result.is_error or result.subtype != "success":
            detail = "; ".join(result.errors or [])
            errors.append(f"run ended with {result.subtype}" + (f": {detail}" if detail else ""))
        if result.stop_reason == "refusal":
            errors.append("model refused to answer")

    answer = (result.result if result is not None and result.result else "") or "\n".join(texts)
    diagnosis, parse_error = extract_diagnosis(answer)
    return TriageResult(
        answer=answer,
        diagnosis=diagnosis,
        parse_error=parse_error,
        run=_usage(result, model, int((time.monotonic() - started) * 1000)),
        tool_calls=tool_calls,
        error="; ".join(dict.fromkeys(errors)) or None,
    )
