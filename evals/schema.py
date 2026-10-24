"""Eval data contracts: cases, result rows and the runner protocol.

``cases.jsonl`` holds one :class:`EvalCase` per line. A runner turns a case into a
:class:`ResultRow`; result files under ``evals/results/`` hold one row per line.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pc_diag_mcp.models import Diagnosis, RootCauseCategory, RunStats, UtcDatetime


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Expected(_Model):
    """Ground truth. ``acceptable`` always contains ``primary``.

    A case is ambiguous (multi-factor) when ``acceptable`` lists more than one category.
    """

    primary: RootCauseCategory
    acceptable: list[RootCauseCategory] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> Expected:
        if self.primary not in self.acceptable:
            raise ValueError("primary must be listed in acceptable")
        if len(set(self.acceptable)) != len(self.acceptable):
            raise ValueError("acceptable contains duplicate categories")
        return self


class EvalCase(_Model):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    split: Literal["dev", "test"]
    question: str = Field(min_length=1)
    scenario: str = Field(description="Replay directory, relative to the repository root.")
    expected: Expected
    tags: list[str] = Field(default_factory=list)
    source: Literal["synthetic", "recorded"]

    def public(self) -> dict[str, str]:
        """The only fields a runner may show the model. ``expected`` and ``tags`` are labels."""
        return {"id": self.id, "question": self.question, "scenario": self.scenario}


class ResultRow(_Model):
    """One model answer for one case under one configuration."""

    case_id: str
    config: str
    model: str
    diagnosis: Diagnosis | None
    raw_answer: str
    error: str | None
    run: RunStats
    timestamp: UtcDatetime


@runtime_checkable
class Runner(Protocol):
    """Runs one configuration (for example ``raw_dump`` or ``skill_tools``) on a case.

    Implementations must build the model's input from ``case.public()`` only.
    """

    name: str

    def run(self, case: EvalCase) -> ResultRow: ...


def load_cases(path: Path) -> list[EvalCase]:
    """Read ``cases.jsonl``. Blank lines are ignored; a bad line raises ``ValueError``."""
    cases: list[EvalCase] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            cases.append(EvalCase.model_validate_json(line))
        except ValueError as exc:
            raise ValueError(f"{path}:{lineno}: invalid case: {exc}") from exc
    return cases
