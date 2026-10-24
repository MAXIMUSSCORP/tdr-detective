"""Run one triage question through Claude with the pc-stability-triage skill.

The agent sees exactly four capabilities: the ``pc-diag`` MCP tools, the project skill,
``Read`` of the skill's own files (its ``reference/`` pages), and
``python <skill>/scripts/compress_events.py <saved files>``. The base tool set is limited
to Bash, Skill and Read, and every editing/network/search/subagent tool is disallowed. Allow
rules cover only the MCP tools and the skill; Read is checked by real path, so ``..`` or a
symlink cannot leave the skill directory. Bash deliberately has no allow rule: a CLI prefix
rule would also admit the compressor's ``--map PATH`` (an arbitrary file write), so every
Bash call reaches ``can_use_tool``, which parses it strictly and denies anything else. A
``PreToolUse`` hook applies the same check to every call, including any the CLI would
auto-approve on its own.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast, get_args

import claude_agent_sdk
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    HookContext,
    HookInput,
    HookJSONOutput,
    HookMatcher,
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
from pc_diag_mcp.tools.events import TEMPFILE_GLOB, events_temp_dir
from tdr_agent.answer import extract_diagnosis

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_NAME = "skill_tools"
SKILL_NAME = "pc-stability-triage"
SERVER_NAME = "pc-diag"
COMPRESSOR = f".claude/skills/{SKILL_NAME}/scripts/compress_events.py"
MCP_PREFIX = f"mcp__{SERVER_NAME}__"

BASE_TOOLS = ["Bash", "Skill", "Read"]
ALLOWED_TOOLS = [f"{MCP_PREFIX}*", f"Skill({SKILL_NAME})"]
DISALLOWED_TOOLS = [
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
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
    f"`python {COMPRESSOR} <files>`; Read works only for files inside the skill directory. "
    "You cannot change the system and must never try."
)

# Compressor arguments: input paths plus the flags that only tune the digest. --map is left
# out on purpose because it writes a file. Values are validated; argparse prefix
# abbreviations (e.g. --ma for --map) never match because flags must be exact.
_PATH_ARG = re.compile(r"^[A-Za-z0-9_./+,@%][A-Za-z0-9_./+,@%=-]*$")
# UNC (//host, which leaks NTLM over SMB) and drive-qualified paths; backslashes are
# rejected for the whole command by _SHELL_META.
_FOREIGN_PATH = re.compile(r"^(?://|[A-Za-z]:)")
_FLAG_VALUES = {
    "--prefix": re.compile(r"^[A-Za-z]{1,8}$"),
    "--burst-gap": re.compile(r"^[0-9]{1,5}(\.[0-9]{1,3})?$"),
    "--max-noise-lines": re.compile(r"^[0-9]{1,4}$"),
}
_HELP_FLAGS = ("-h", "--help")
_SHELL_META = re.compile(r"[;&|`$<>(){}\[\]*?!~#'\"\n\r\\]")
_DATE_SUFFIX = re.compile(r"^-\d{8}$")


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


def _denial(tool_name: str) -> str:
    return (
        f"{tool_name} is not permitted: TDR Detective is read-only. Use the "
        f"{MCP_PREFIX}* tools, the {SKILL_NAME} skill, Read of files under "
        f".claude/skills/{SKILL_NAME}/, or exactly `python {COMPRESSOR} <saved event files>`."
    )


def is_saved_events_file(arg: str, cwd: Path, temp_dir: Path) -> bool:
    """True only for a regular ``events-*.json`` file directly inside the MCP server's temp
    dir, after resolving symlinks. Anything else could be a UNC path, a device, a FIFO or
    simply a file the agent has no business reading."""
    if _FOREIGN_PATH.match(arg) or not _PATH_ARG.match(arg):
        return False
    target = Path(arg)
    resolved = Path(os.path.realpath(target if target.is_absolute() else cwd / target))
    if resolved.parent != Path(os.path.realpath(temp_dir)):
        return False
    if not fnmatch.fnmatchcase(resolved.name, TEMPFILE_GLOB):
        return False
    try:
        return stat.S_ISREG(os.stat(resolved).st_mode)
    except OSError:
        return False


def _compressor_args_ok(args: list[str], cwd: Path, temp_dir: Path) -> bool:
    i = 0
    while i < len(args):
        arg = args[i]
        flag, eq, inline = arg.partition("=")
        if arg in _HELP_FLAGS:
            i += 1
        elif flag in _FLAG_VALUES:
            value = inline if eq else (args[i + 1] if i + 1 < len(args) else "")
            if not _FLAG_VALUES[flag].match(value):
                return False
            i += 1 if eq else 2
        elif arg.startswith("-") or not is_saved_events_file(arg, cwd, temp_dir):
            return False
        else:
            i += 1
    return True


def is_compressor_command(command: str, cwd: Path, temp_dir: Path) -> bool:
    """True only for ``python[3] <COMPRESSOR> [saved event files] [digest flags]`` with no
    shell syntax and no option that writes a file."""
    if _SHELL_META.search(command):
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if len(argv) < 2 or argv[0] not in ("python", "python3") or argv[1] != COMPRESSOR:
        return False
    return _compressor_args_ok(argv[2:], cwd, temp_dir)


def is_skill_file(file_path: object, cwd: Path) -> bool:
    """True when ``file_path`` resolves (symlinks too) inside ``<cwd>/.claude/skills/<skill>``."""
    if not isinstance(file_path, str) or not file_path or "\0" in file_path:
        return False
    root = Path(os.path.realpath(cwd / ".claude" / "skills" / SKILL_NAME))
    target = Path(file_path)
    resolved = Path(os.path.realpath(target if target.is_absolute() else cwd / target))
    return resolved.is_relative_to(root)


def is_permitted(tool_name: str, tool_input: dict[str, Any], cwd: Path, temp_dir: Path) -> bool:
    """The read-only surface: pc-diag tools, this skill and its files, and the compressor."""
    if tool_name.startswith(MCP_PREFIX):
        return True
    if tool_name == "Skill":
        return SKILL_NAME in (tool_input.get("skill"), tool_input.get("command"))
    if tool_name == "Read":
        return is_skill_file(tool_input.get("file_path"), cwd)
    command = tool_input.get("command")
    return (
        tool_name == "Bash"
        and isinstance(command, str)
        and is_compressor_command(command, cwd, temp_dir)
    )


@dataclass(frozen=True)
class Guard:
    """The permission callback and PreToolUse hook for one run, bound to its cwd and to
    the pc-diag server's temp dir (the only place the compressor may read from)."""

    cwd: Path
    temp_dir: Path = field(default_factory=events_temp_dir)

    async def can_use_tool(
        self, tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        """Permission prompt replacement: allow the read-only surface, deny everything else."""
        if is_permitted(tool_name, tool_input, self.cwd, self.temp_dir):
            return PermissionResultAllow()
        return PermissionResultDeny(message=_denial(tool_name))

    async def pre_tool_use(
        self, hook_input: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        """Runs before every tool call, even ones the CLI would auto-approve (its built-in
        read-only commands, reads under cwd, settings rules), and denies anything outside the
        surface."""
        if hook_input["hook_event_name"] != "PreToolUse":
            return {}
        tool_name = hook_input["tool_name"]
        if is_permitted(tool_name, hook_input["tool_input"], self.cwd, self.temp_dir):
            return {}  # no decision: the normal permission flow still applies
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": _denial(tool_name),
            }
        }


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
    guard = Guard(cwd)
    return ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        tools=list(BASE_TOOLS),
        allowed_tools=list(ALLOWED_TOOLS),
        disallowed_tools=list(DISALLOWED_TOOLS),
        can_use_tool=guard.can_use_tool,
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[guard.pre_tool_use])]},
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
    """Exact id, or the same id with a dated snapshot suffix (``-YYYYMMDD``)."""
    if reported is None or not reported.startswith(requested):
        return False
    rest = reported[len(requested) :]
    return rest == "" or bool(_DATE_SUFFIX.match(rest))


def check_project_settings(cwd: Path) -> None:
    """``setting_sources=["project"]`` loads ``<cwd>/.claude/settings.json``. Allowlist it:
    the file may be absent or hold only ``$schema`` and ``permissions.deny``, which can only
    narrow the guardrail. Anything else fails closed."""
    path = cwd / ".claude" / "settings.json"
    if not path.exists():
        return
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TriageSetupError(f"cannot read {path}: {exc}") from exc
    if not isinstance(settings, dict):
        raise TriageSetupError(f"{path} is not a settings object; refusing to guess its effect")
    extra = sorted(set(settings) - {"$schema", "permissions"})
    permissions = settings.get("permissions", {})
    if isinstance(permissions, dict):
        extra += sorted(f"permissions.{key}" for key in set(permissions) - {"deny"})
        deny = permissions.get("deny", [])
        if not isinstance(deny, list) or not all(isinstance(rule, str) for rule in deny):
            extra.append("permissions.deny (must be a list of rule strings)")
    else:
        extra.append("permissions (must be an object)")
    if extra:
        raise TriageSetupError(
            f"{path} sets {', '.join(extra)}; a triage run allows only $schema and "
            "permissions.deny there, because anything else could widen the read-only "
            "guardrail. Remove those keys or run from a clean checkout."
        )


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
    # Same arithmetic as the raw_dump baseline: fresh input includes cache writes and cache
    # reads are separate. model_usage covers the whole run (every model, subagents and the
    # harness's own helper calls, system prompt included); usage covers the top-level loop.
    if result.model_usage:
        per_model = list(result.model_usage.values())
        fresh = sum(
            int(m.get("inputTokens", 0)) + int(m.get("cacheCreationInputTokens", 0))
            for m in per_model
        )
        output = sum(int(m.get("outputTokens", 0)) for m in per_model)
        cached = sum(int(m.get("cacheReadInputTokens", 0)) for m in per_model)
    else:
        usage = result.usage or {}
        fresh = int(usage.get("input_tokens", 0)) + int(usage.get("cache_creation_input_tokens", 0))
        output = int(usage.get("output_tokens", 0))
        cached = int(usage.get("cache_read_input_tokens", 0))
    return RunStats(
        config=CONFIG_NAME,
        model=model,
        input_tokens=fresh,
        output_tokens=output,
        cache_read_tokens=cached,
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
    check_project_settings(cwd)
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
