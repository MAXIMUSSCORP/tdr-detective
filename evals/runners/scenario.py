"""Resolve a case's replay directory, refusing paths that leave the repository."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def resolve_scenario(scenario: str, repo_root: Path | None = None) -> Path:
    """``scenario`` is relative to the repository root (see ``EvalCase.scenario``)."""
    root = (repo_root or REPO_ROOT).resolve()
    path = (root / scenario).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"scenario {scenario!r} resolves outside the repository")
    return path
