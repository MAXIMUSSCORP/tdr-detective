"""The ``raw_dump`` baseline: one Messages API call over the scenario's raw files.

The model gets the user's question (from ``EvalCase.public()``) and the replay directory's
files serialized as-is: ``scenario.json``, the full ``events.json`` and whichever of the
optional telemetry files exist. The system prompt is ``RAW_DUMP_SYSTEM_PROMPT`` (output
contract only; see ``evals.runners.prompts`` for the fairness rule). No tools, no skill.

Request (anthropic 1.13.0, ``client.messages.stream(...).get_final_message()``):
``model`` as configured (default ``claude-opus-5-5``), ``max_tokens=16000``,
``thinking={"type": "adaptive"}`` (thinking cannot be disabled on Opus 5.5),
``output_config={"effort": <same as skill_tools>}``, ``system``, one ``user`` message.
No sampling parameters, no assistant prefill and no model fallbacks: an eval must not
switch models. Before the call, ``messages.count_tokens`` measures the same request, and a
case above ``max_input_tokens`` is refused without calling the model.

Credentials come from the SDK's default chain (``ANTHROPIC_API_KEY`` and friends).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from evals.pricing import UnknownModelPricing, cost_usd, pricing_for
from evals.runners.prompts import RAW_DUMP_SYSTEM_PROMPT
from evals.runners.scenario import resolve_scenario
from evals.schema import EvalCase, ResultRow
from pc_diag_mcp.models import Diagnosis, RunStats

if TYPE_CHECKING:
    from anthropic import Anthropic
    from anthropic.types import Message, MessageParam

REQUIRED_FILES = ("scenario.json", "events.json")
OPTIONAL_FILES = (
    "process_snapshot.json",
    "sensors.json",
    "drivers.json",
    "dxdiag.json",
    "frametimes.json",
)

Effort = Literal["low", "medium", "high", "xhigh", "max"]

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT: Effort = "medium"
DEFAULT_MAX_TOKENS = 16_000
DEFAULT_MAX_INPUT_TOKENS = 200_000

# stop_reason values that mean the answer is incomplete or absent.
_BAD_STOPS = {
    "max_tokens": "answer truncated at max_tokens",
    "model_context_window_exceeded": "input exceeded the model context window",
    "pause_turn": "model paused the turn",
    "tool_use": "model asked for a tool, but none are offered",
}


class RawDumpRunner:
    """``Runner`` for the ``raw_dump`` configuration. Safe to share across threads."""

    name = "raw_dump"

    def __init__(
        self,
        *,
        client: Anthropic | None = None,
        model: str = DEFAULT_MODEL,
        effort: Effort = DEFAULT_EFFORT,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
        repo_root: Path | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        try:
            pricing_for(model)
        except UnknownModelPricing as exc:
            raise ValueError(exc.args[0]) from None
        self.model = model
        self.effort: Effort = effort
        self.max_tokens = max_tokens
        self.max_input_tokens = max_input_tokens
        self._repo_root = repo_root
        self._client = client
        self._clock = clock
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    # ------------------------------------------------------------------ #
    # Request construction
    # ------------------------------------------------------------------ #

    def user_message(self, case: EvalCase) -> str:
        """The single user turn: the public question plus the raw files, verbatim."""
        public = case.public()
        directory = resolve_scenario(public["scenario"], self._repo_root)
        parts = [
            f"Question: {public['question']}",
            f"Telemetry exported from the machine ({public['scenario']}), one file per "
            "block, each reproduced verbatim:",
        ]
        for name in REQUIRED_FILES:
            parts.append(_file_block(name, (directory / name).read_text(encoding="utf-8")))
        for name in OPTIONAL_FILES:
            path = directory / name
            if path.is_file():
                parts.append(_file_block(name, path.read_text(encoding="utf-8")))
        return "\n\n".join(parts) + "\n"

    def _messages(self, case: EvalCase) -> list[MessageParam]:
        return [{"role": "user", "content": self.user_message(case)}]

    def _client_or_default(self) -> Anthropic:
        with self._lock:
            if self._client is None:
                from anthropic import Anthropic

                self._client = Anthropic()
            return self._client

    # ------------------------------------------------------------------ #
    # Estimates (used by evals.run for --dry-run and the budget guard)
    # ------------------------------------------------------------------ #

    def estimate_input_tokens(self, case: EvalCase) -> int:
        """``messages.count_tokens`` for exactly the request ``run`` would send (cached)."""
        with self._lock:
            cached = self._counts.get(case.id)
        if cached is not None:
            return cached
        counted = (
            self._client_or_default()
            .messages.count_tokens(
                model=self.model,
                system=RAW_DUMP_SYSTEM_PROMPT,
                messages=self._messages(case),
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
            )
            .input_tokens
        )
        with self._lock:
            self._counts[case.id] = counted
        return counted

    def cost_ceiling_usd(self, case: EvalCase) -> float:
        """Most this case can cost: counted input plus a full ``max_tokens`` of output."""
        tokens = self.estimate_input_tokens(case)
        if tokens > self.max_input_tokens:
            return 0.0
        return cost_usd(self.model, input_tokens=tokens, output_tokens=self.max_tokens)

    # ------------------------------------------------------------------ #
    # Run
    # ------------------------------------------------------------------ #

    def run(self, case: EvalCase) -> ResultRow:
        # Load the extractor before anything is billed: a missing ``tdr_agent`` must fail
        # the case up front, not turn every paid answer into an error row.
        extract = _load_extractor()
        messages = self._messages(case)
        counted = self.estimate_input_tokens(case)
        if counted > self.max_input_tokens:
            return self._row(
                case,
                raw_answer="",
                diagnosis=None,
                error=(
                    f"input of {counted:,} tokens exceeds the cap of "
                    f"{self.max_input_tokens:,}; not sent"
                ),
                run=RunStats(
                    config=self.name,
                    model=self.model,
                    input_tokens=0,
                    output_tokens=0,
                    cost_usd=0.0,
                    latency_ms=0,
                ),
            )

        started = self._clock()
        with self._client_or_default().messages.stream(
            model=self.model,
            max_tokens=self.max_tokens,
            system=RAW_DUMP_SYSTEM_PROMPT,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
        ) as stream:
            message: Message = stream.get_final_message()
        latency_ms = round((self._clock() - started) * 1000)

        run = self._run_stats(message, latency_ms)
        answer = "".join(block.text for block in message.content if block.type == "text")
        errors: list[str] = []
        if message.stop_reason == "refusal":
            details = message.stop_details
            category = details.category if details else None
            explanation = details.explanation if details else None
            errors.append(
                f"refusal (category={category or 'unspecified'})"
                + (f": {explanation}" if explanation else "")
            )
        elif message.stop_reason in _BAD_STOPS:
            errors.append(f"stop_reason {message.stop_reason}: {_BAD_STOPS[message.stop_reason]}")

        diagnosis: Diagnosis | None = None
        if answer:
            try:
                diagnosis, _parse_error = extract(answer)
            except Exception as exc:  # a parser bug: keep the billed usage, record the failure
                errors.append(f"diagnosis extraction failed: {type(exc).__name__}: {exc}")
        return self._row(
            case,
            raw_answer=answer,
            diagnosis=diagnosis,
            error="; ".join(errors) or None,
            run=run,
        )

    def _run_stats(self, message: Message, latency_ms: int) -> RunStats:
        usage = message.usage
        cache_read = usage.cache_read_input_tokens or 0
        cache_write = usage.cache_creation_input_tokens or 0
        return RunStats(
            config=self.name,
            model=self.model,
            input_tokens=usage.input_tokens + cache_write,
            output_tokens=usage.output_tokens,
            cache_read_tokens=cache_read,
            cost_usd=cost_usd(
                self.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
            ),
            latency_ms=latency_ms,
        )

    def _row(
        self,
        case: EvalCase,
        *,
        raw_answer: str,
        diagnosis: Diagnosis | None,
        error: str | None,
        run: RunStats,
    ) -> ResultRow:
        return ResultRow(
            case_id=case.id,
            config=self.name,
            model=self.model,
            diagnosis=diagnosis,
            raw_answer=raw_answer,
            error=error,
            run=run,
            timestamp=datetime.now(UTC),
        )


def _file_block(name: str, text: str) -> str:
    return f'<file name="{name}">\n{text.rstrip()}\n</file>'


Extractor = Callable[[str], tuple[Diagnosis | None, str | None]]


def _load_extractor() -> Extractor:
    """The agent's own answer parser, so both configs parse alike.

    A parse failure is not an error row: the scorer re-parses ``raw_answer`` and records
    why it failed.
    """
    from tdr_agent.answer import extract_diagnosis

    extract: Extractor = extract_diagnosis
    return extract
