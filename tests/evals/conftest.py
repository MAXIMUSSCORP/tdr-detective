"""Shared builders for the eval harness tests."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from evals.schema import EvalCase, ResultRow
from pc_diag_mcp.models import Diagnosis, RootCauseCategory, RunStats

T0 = datetime(2026, 10, 14, 1, 40, tzinfo=UTC)


def diagnosis_dict(
    categories: Sequence[str],
    *,
    refs: Sequence[str] = ("ev-1",),
    bug_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    n = len(categories)
    causes = [
        {
            "category": cat,
            "confidence": round(0.9 - i * (0.8 / max(n, 1)), 3),
            "rationale": f"rationale for {cat}",
            "evidence_refs": list(refs) if i == 0 else [],
        }
        for i, cat in enumerate(categories)
    ]
    report: dict[str, Any] = {
        "title": "Display driver reset during gameplay",
        "summary": "The GPU driver stopped responding and recovered.",
        "os_build": "22631.4317",
        "gpu": "AMD Radeon RX 6800 XT",
        "driver_version": "31.0.24033.1003",
        "occurred_at": "2026-10-14T01:43:08Z",
        "steps_to_reproduce": ["Play a game for 30 minutes"],
        "expected": "The game keeps rendering.",
        "actual": "The screen went black for two seconds.",
        "evidence": ["Display 4101 at 01:43:08Z"],
        "severity": "high",
    }
    if bug_report:
        report.update(bug_report)
    return {
        "window": {"start": "2026-10-14T01:30:00Z", "end": "2026-10-14T02:00:00Z"},
        "causes": causes,
        "next_steps": ["Update the display driver"],
        "bug_report": report,
    }


def fenced(payload: dict[str, Any] | str) -> str:
    body = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
    return f"Here is my analysis.\n\n```json\n{body}\n```\n"


@pytest.fixture
def make_case() -> Callable[..., EvalCase]:
    def _make(
        case_id: str = "syn-900",
        primary: str = "gpu_driver_timeout",
        acceptable: Sequence[str] | None = None,
        split: str = "dev",
    ) -> EvalCase:
        return EvalCase.model_validate(
            {
                "id": case_id,
                "split": split,
                "question": "My screen went black around 9:42 tonight.",
                "scenario": f"evals/scenarios/{case_id}",
                "expected": {
                    "primary": primary,
                    "acceptable": list(acceptable) if acceptable is not None else [primary],
                },
                "tags": [],
                "source": "synthetic",
            }
        )

    return _make


@pytest.fixture
def make_row() -> Callable[..., ResultRow]:
    def _make(
        case_id: str = "syn-900",
        *,
        config: str = "skill_tools",
        model: str = "model-a",
        raw_answer: str = "",
        diagnosis: dict[str, Any] | None = None,
        error: str | None = None,
        input_tokens: int = 1000,
        output_tokens: int = 100,
        cost_usd: float | None = 0.01,
        latency_ms: int = 1000,
    ) -> ResultRow:
        return ResultRow(
            case_id=case_id,
            config=config,
            model=model,
            diagnosis=Diagnosis.model_validate(diagnosis) if diagnosis else None,
            raw_answer=raw_answer,
            error=error,
            run=RunStats(
                config=config,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                latency_ms=latency_ms,
            ),
            timestamp=T0,
        )

    return _make


@pytest.fixture
def diag() -> Callable[..., dict[str, Any]]:
    return diagnosis_dict


@pytest.fixture
def fence() -> Callable[[dict[str, Any] | str], str]:
    return fenced


@pytest.fixture
def all_categories() -> list[str]:
    return [c.value for c in RootCauseCategory]
