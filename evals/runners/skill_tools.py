"""The ``skill_tools`` configuration: the Claude Agent SDK with the pc-stability-triage
skill and the pc-diag MCP tools, replaying the case's scenario.

The heavy lifting lives in ``tdr_agent.triage.run_triage``; this module only adapts a case
to it and its ``TriageResult`` to a ``ResultRow``. It is imported lazily, so the eval
harness loads without the agent extra until this runner actually runs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from evals.runners.scenario import resolve_scenario
from evals.schema import EvalCase, ResultRow
from pc_diag_mcp.models import Diagnosis, RunStats

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"
DEFAULT_MAX_BUDGET_USD = 1.0


class TriageOutcome(Protocol):
    """The fields of ``tdr_agent.triage.TriageResult`` this runner reads."""

    @property
    def answer(self) -> str: ...
    @property
    def diagnosis(self) -> Diagnosis | None: ...
    @property
    def run(self) -> RunStats: ...
    @property
    def error(self) -> str | None: ...


class TriageFn(Protocol):
    def __call__(
        self,
        question: str,
        *,
        replay_dir: Path | None = ...,
        model: str = ...,
        effort: str = ...,
        max_budget_usd: float = ...,
    ) -> Awaitable[TriageOutcome]: ...


def _load_run_triage() -> TriageFn:
    from tdr_agent.triage import run_triage

    triage: TriageFn = run_triage
    return triage


class SkillToolsRunner:
    """``Runner`` for ``skill_tools``. Each ``run`` drives its own event loop, so it can be
    called from worker threads."""

    name = "skill_tools"

    def __init__(
        self,
        *,
        triage: TriageFn | None = None,
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
        max_budget_usd: float = DEFAULT_MAX_BUDGET_USD,
        repo_root: Path | None = None,
    ) -> None:
        self.model = model
        self.effort = effort
        self.max_budget_usd = max_budget_usd
        self._triage = triage
        self._repo_root = repo_root

    def estimate_input_tokens(self, case: EvalCase) -> int | None:
        """Unknown up front: the agent decides how much telemetry to pull."""
        return None

    def cost_ceiling_usd(self, case: EvalCase) -> float:
        """The per-case budget handed to the agent (``max_budget_usd``)."""
        return self.max_budget_usd

    def run(self, case: EvalCase) -> ResultRow:
        public = case.public()
        replay_dir = resolve_scenario(public["scenario"], self._repo_root)
        triage = self._triage or _load_run_triage()
        result = asyncio.run(
            _await(
                triage(
                    public["question"],
                    replay_dir=replay_dir,
                    model=self.model,
                    effort=self.effort,
                    max_budget_usd=self.max_budget_usd,
                )
            )
        )
        # parse_error is not copied into error: the scorer re-parses raw_answer and
        # records the extraction failure itself.
        return ResultRow(
            case_id=case.id,
            config=self.name,
            model=self.model,
            diagnosis=result.diagnosis,
            raw_answer=result.answer,
            error=result.error,
            run=result.run.model_copy(update={"config": self.name}),
            timestamp=datetime.now(UTC),
        )


async def _await(pending: Awaitable[TriageOutcome]) -> TriageOutcome:
    return await pending
