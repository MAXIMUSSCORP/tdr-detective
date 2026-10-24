"""Summarise eval result files.

    python -m evals.report                       # every evals/results/*.jsonl
    python -m evals.report evals/results/a.jsonl evals/results/b.jsonl

Prints a Markdown table and writes ``evals/results/summary.json``, which the timeline
UI reads. Its shape is fixed::

    {"generated_at": iso, "configs": [{"config", "model", "split", "cases",
      "top1_accuracy", "top3_accuracy", "valid_rate", "report_complete_rate",
      "mean_input_tokens", "mean_output_tokens", "mean_cost_usd",
      "mean_latency_ms", "p95_latency_ms"}]}

Exit codes: 0 on success, 1 when there is nothing to report, 2 on corrupt input.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.schema import ResultRow, load_cases
from evals.scoring import ConfigSummary, aggregate

EVALS_DIR = Path(__file__).resolve().parent
RESULTS_DIR = EVALS_DIR / "results"
CASES_PATH = EVALS_DIR / "cases.jsonl"

SUMMARY_FIELDS = (
    "config",
    "model",
    "split",
    "cases",
    "top1_accuracy",
    "top3_accuracy",
    "valid_rate",
    "report_complete_rate",
    "mean_input_tokens",
    "mean_output_tokens",
    "mean_cost_usd",
    "mean_latency_ms",
    "p95_latency_ms",
)


def load_rows(paths: Sequence[Path]) -> list[ResultRow]:
    rows: list[ResultRow] = []
    for path in paths:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                rows.append(ResultRow.model_validate_json(line))
            except ValueError as exc:
                first = str(exc).splitlines()[0]
                raise ValueError(f"{path}:{lineno}: invalid result row: {first}") from exc
    return rows


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_markdown(summaries: Sequence[ConfigSummary]) -> str:
    header = (
        "| Config | Model | Split | N | Top-1 | Top-3 | Acceptable top-1 | Valid | "
        "Report complete | Cites evidence | Mean in tok | Median in tok | Mean out tok | "
        "Median out tok | Mean cost (USD) | Mean latency (s) | p95 latency (s) |"
    )
    lines = [header, "|" + "---|" * header.count(" | ") + "---|"]
    for s in summaries:
        mean_cost = "n/a" if s.mean_cost_usd is None else f"{s.mean_cost_usd:.4f}"
        cost = f"{mean_cost} ({s.cost_reported}/{s.cases})"
        cells = [
            s.config,
            s.model,
            s.split,
            str(s.cases),
            _pct(s.top1_accuracy),
            _pct(s.top3_accuracy),
            _pct(s.acceptable_top1_accuracy),
            _pct(s.valid_rate),
            _pct(s.report_complete_rate),
            _pct(s.cites_evidence_rate),
            f"{s.mean_input_tokens:,.0f}",
            f"{s.median_input_tokens:,.0f}",
            f"{s.mean_output_tokens:,.0f}",
            f"{s.median_output_tokens:,.0f}",
            cost,
            f"{s.mean_latency_ms / 1000:.2f}",
            f"{s.p95_latency_ms / 1000:.2f}",
        ]
        lines.append("| " + " | ".join(cells) + " |")
    if summaries:
        smallest = min(s.cases for s in summaries)
        lines.append("")
        lines.append(
            f"N is the number of result rows. With N = {smallest}, one case moves a rate by "
            f"{100 / smallest:.1f} points; mean cost covers only rows that reported a cost "
            "(shown as reported/N)."
        )
    return "\n".join(lines) + "\n"


def summary_payload(summaries: Sequence[ConfigSummary], generated_at: datetime) -> dict[str, Any]:
    configs = []
    for s in summaries:
        dumped = s.model_dump()
        configs.append({key: dumped[key] for key in SUMMARY_FIELDS})
    return {"generated_at": generated_at.isoformat(), "configs": configs}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("results", nargs="*", type=Path, help="result .jsonl files")
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    parser.add_argument("--summary-out", type=Path, default=None)
    args = parser.parse_args(argv)

    paths: list[Path] = args.results or sorted(RESULTS_DIR.glob("*.jsonl"))
    if not paths:
        print(f"no result files given and none found in {RESULTS_DIR}", file=sys.stderr)
        return 1
    try:
        cases = load_cases(args.cases)
        summaries = aggregate(load_rows(paths), cases)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not summaries:
        print("no result files given and no rows in the given files", file=sys.stderr)
        return 1

    print(render_markdown(summaries), end="")
    out: Path = args.summary_out or RESULTS_DIR / "summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = summary_payload(summaries, datetime.now(UTC))
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
