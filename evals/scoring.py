"""Score model answers against eval cases and aggregate per configuration.

Scoring rules (per case):

* ``valid``: a ``Diagnosis`` was obtained, either from ``ResultRow.diagnosis`` or from
  the *last* fenced ```` ```json ```` block in ``raw_answer``. Anything else (no block,
  bad JSON, schema violation, runner error) is a scored failure, never an exception.
* ``top1``: ``causes[0]`` is the expected primary category.
* ``acceptable_top1``: ``causes[0]`` is any acceptable category.
* ``top3``: any acceptable category (which includes the primary) is in ``causes[:3]``.
* ``report_complete``: every BugReport string is non-blank, with at least one evidence
  entry and at least one reproduction step, none of them blank.
* ``cites_evidence``: ``causes[0].evidence_refs`` is non-empty.

An invalid answer scores ``False`` on every metric.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, ValidationError

from evals.schema import EvalCase, ResultRow
from pc_diag_mcp.models import BugReport, Diagnosis

_JSON_FENCE = re.compile(r"```json[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
_ERROR_LIMIT = 500


@dataclass(frozen=True)
class Extraction:
    diagnosis: Diagnosis | None
    error: str | None


def extract_diagnosis(raw_answer: str) -> Extraction:
    """Parse the last fenced ```json block of ``raw_answer`` as a ``Diagnosis``."""
    blocks = _JSON_FENCE.findall(raw_answer)
    if not blocks:
        return Extraction(None, "no fenced ```json block found")
    try:
        payload = json.loads(blocks[-1])
    except json.JSONDecodeError as exc:
        return Extraction(None, f"invalid JSON in ```json block: {exc}")
    try:
        return Extraction(Diagnosis.model_validate(payload), None)
    except ValidationError as exc:
        detail = str(exc).replace("\n", " ")[:_ERROR_LIMIT]
        return Extraction(None, f"Diagnosis validation failed: {detail}")


class CaseScore(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    config: str
    model: str
    split: str
    valid: bool
    top1: bool
    top3: bool
    acceptable_top1: bool
    report_complete: bool
    cites_evidence: bool
    error: str | None


def _report_complete(report: BugReport) -> bool:
    strings = [
        report.title,
        report.summary,
        report.os_build,
        report.gpu,
        report.driver_version,
        report.expected,
        report.actual,
    ]
    lists = [report.steps_to_reproduce, report.evidence]
    return all(s.strip() for s in strings) and all(
        lst and all(item.strip() for item in lst) for lst in lists
    )


def score_case(case: EvalCase, row: ResultRow) -> CaseScore:
    if row.case_id != case.id:
        raise ValueError(f"result row {row.case_id!r} scored against case {case.id!r}")

    diagnosis = row.diagnosis
    error = row.error
    if diagnosis is None:
        extraction = extract_diagnosis(row.raw_answer)
        diagnosis = extraction.diagnosis
        if extraction.error:
            error = f"{error}; {extraction.error}" if error else extraction.error

    common = {"case_id": case.id, "config": row.config, "model": row.model, "split": case.split}
    if diagnosis is None:
        return CaseScore(
            **common,
            valid=False,
            top1=False,
            top3=False,
            acceptable_top1=False,
            report_complete=False,
            cites_evidence=False,
            error=error,
        )

    ranked = [c.category for c in diagnosis.causes]
    acceptable = set(case.expected.acceptable)
    return CaseScore(
        **common,
        valid=True,
        top1=ranked[0] == case.expected.primary,
        top3=any(c in acceptable for c in ranked[:3]),
        acceptable_top1=ranked[0] in acceptable,
        report_complete=_report_complete(diagnosis.bug_report),
        cites_evidence=bool(diagnosis.causes[0].evidence_refs),
        error=error,
    )


class ConfigSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    config: str
    model: str
    split: str
    cases: int
    top1_accuracy: float
    top3_accuracy: float
    acceptable_top1_accuracy: float
    valid_rate: float
    report_complete_rate: float
    cites_evidence_rate: float
    mean_input_tokens: float
    median_input_tokens: float
    mean_output_tokens: float
    median_output_tokens: float
    mean_cost_usd: float | None
    cost_reported: int
    mean_latency_ms: float
    p95_latency_ms: float


def percentile(values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile: the smallest value with at least ``pct``% of values <= it."""
    if not values:
        raise ValueError("percentile of an empty sequence")
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def _rate(flags: Sequence[bool]) -> float:
    return sum(flags) / len(flags)


def aggregate(rows: Iterable[ResultRow], cases: Iterable[EvalCase]) -> list[ConfigSummary]:
    """Summarise rows per (config, model, split), sorted by that key.

    Every row counts as one sample, so repeated trials of a case weigh proportionally.
    ``mean_cost_usd`` averages only rows that report a cost and is ``None`` if none do;
    ``cost_reported`` says how many rows that was, so configs can be compared fairly.
    """
    by_id = {c.id: c for c in cases}
    groups: dict[tuple[str, str, str], list[tuple[ResultRow, CaseScore]]] = defaultdict(list)
    for row in rows:
        case = by_id.get(row.case_id)
        if case is None:
            raise ValueError(f"result row references unknown case {row.case_id!r}")
        groups[(row.config, row.model, case.split)].append((row, score_case(case, row)))

    summaries: list[ConfigSummary] = []
    for (config, model, split), items in sorted(groups.items()):
        scores = [s for _, s in items]
        runs = [r.run for r, _ in items]
        inputs = [float(r.input_tokens) for r in runs]
        outputs = [float(r.output_tokens) for r in runs]
        latencies = [float(r.latency_ms) for r in runs]
        costs = [r.cost_usd for r in runs if r.cost_usd is not None]
        summaries.append(
            ConfigSummary(
                config=config,
                model=model,
                split=split,
                cases=len(items),
                top1_accuracy=_rate([s.top1 for s in scores]),
                top3_accuracy=_rate([s.top3 for s in scores]),
                acceptable_top1_accuracy=_rate([s.acceptable_top1 for s in scores]),
                valid_rate=_rate([s.valid for s in scores]),
                report_complete_rate=_rate([s.report_complete for s in scores]),
                cites_evidence_rate=_rate([s.cites_evidence for s in scores]),
                mean_input_tokens=statistics.fmean(inputs),
                median_input_tokens=statistics.median(inputs),
                mean_output_tokens=statistics.fmean(outputs),
                median_output_tokens=statistics.median(outputs),
                mean_cost_usd=statistics.fmean(costs) if costs else None,
                cost_reported=len(costs),
                mean_latency_ms=statistics.fmean(latencies),
                p95_latency_ms=percentile(latencies, 95),
            )
        )
    return summaries
