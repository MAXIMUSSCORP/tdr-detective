"""Run one eval configuration over the cases and append result rows.

    python -m evals.run --config raw_dump --split dev
    python -m evals.run --config skill_tools --split test --limit 5 --max-cost-usd 5
    python -m evals.run --config raw_dump --split all --dry-run

Rows go to ``evals/results/<YYYY-MM-DD>-<config>.jsonl`` (UTC date) and are appended and
flushed one at a time, so a crash keeps every finished case. A runner or API exception
becomes a row with ``error`` set; it never stops the run.

Budget: before a case starts, its worst-case cost (``cost_ceiling_usd``: counted input
plus a full ``max_tokens`` of output for raw_dump, ``max_budget_usd`` for skill_tools) is
added to what has been spent and what is reserved by cases in flight. If that would pass
``--max-cost-usd``, no new case starts; cases still in flight finish and are recorded. A
row that reports no cost counts as its ceiling.

``--dry-run`` prints the plan and the estimated input tokens (``count_tokens`` for
raw_dump, which is not a model call) and writes nothing.

Credentials come from the environment (``ANTHROPIC_API_KEY`` or the SDK's default chain)
and are never printed. Every row is redacted as a whole before it is written or echoed:
credential values from the environment (and from a Google credentials file) and anything
shaped like an Anthropic key (``sk-ant-...``) become ``[redacted]``.

``--case ID`` searches every split unless ``--split`` is given; otherwise the default split
is ``dev``.

Exit codes: 0 when the run (or dry run) completed, 1 when no case matched the filters,
2 on bad arguments or unreadable cases, 3 when every case that ran ended in an error
(for example a missing or rejected API key).
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import IO, Literal, Protocol, cast

from evals.schema import EvalCase, ResultRow, Runner, load_cases
from pc_diag_mcp.models import RunStats
from tdr_agent.redact import redact

EVALS_DIR = Path(__file__).resolve().parent
CASES_PATH = EVALS_DIR / "cases.jsonl"
RESULTS_DIR = EVALS_DIR / "results"

CONFIGS = ("raw_dump", "skill_tools")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"


class EvalRunner(Runner, Protocol):
    """A ``Runner`` the harness can plan and budget."""

    model: str

    def estimate_input_tokens(self, case: EvalCase) -> int | None: ...

    def cost_ceiling_usd(self, case: EvalCase) -> float: ...


@dataclass(frozen=True)
class RunOptions:
    config: str
    model: str
    effort: str
    max_input_tokens: int
    max_case_cost_usd: float


RunnerFactory = Callable[[RunOptions], EvalRunner]


def build_runner(options: RunOptions) -> EvalRunner:
    """Construct the runner for ``options.config``. Clients are created on first use."""
    if options.config == "raw_dump":
        from evals.runners.raw_dump import Effort, RawDumpRunner

        return RawDumpRunner(
            model=options.model,
            effort=cast(Effort, options.effort),
            max_input_tokens=options.max_input_tokens,
        )
    if options.config == "skill_tools":
        from evals.runners.skill_tools import SkillToolsRunner

        return SkillToolsRunner(
            model=options.model,
            effort=options.effort,
            max_budget_usd=options.max_case_cost_usd,
        )
    raise ValueError(f"unknown config {options.config!r}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals.run",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", required=True, choices=CONFIGS)
    parser.add_argument(
        "--split",
        choices=("dev", "test", "all"),
        default=None,
        help="default: dev, or all splits when --case is given",
    )
    parser.add_argument("--limit", type=_positive_int, default=None, help="first N cases")
    parser.add_argument(
        "--case", action="append", default=None, metavar="ID", help="only this case (repeatable)"
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--effort", choices=EFFORTS, default=DEFAULT_EFFORT)
    parser.add_argument(
        "--max-cost-usd", type=_non_negative_float, default=15.0, help="budget for the whole run"
    )
    parser.add_argument("--concurrency", type=_positive_int, default=2)
    parser.add_argument(
        "--max-input-tokens",
        type=_positive_int,
        default=200_000,
        help="raw_dump: refuse a case whose counted input is above this",
    )
    parser.add_argument(
        "--max-case-cost-usd",
        type=_non_negative_float,
        default=1.0,
        help="skill_tools: per-case agent budget (max_budget_usd)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan; call no model")
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    return parser


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _non_negative_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("must be a finite, non-negative number")
    return value


def select_cases(
    cases: Sequence[EvalCase], split: str, ids: Sequence[str] | None, limit: int | None
) -> list[EvalCase]:
    """Filter by split, then by id (in file order), then take the first ``limit``."""
    chosen = [c for c in cases if split == "all" or c.split == split]
    if ids:
        known = {c.id for c in cases}
        missing = [i for i in ids if i not in known]
        if missing:
            raise KeyError(", ".join(missing))
        wanted = set(ids)
        chosen = [c for c in chosen if c.id in wanted]
    return chosen[:limit] if limit is not None else chosen


def redact_row(row: ResultRow) -> ResultRow:
    """The row with every field redacted (answer, diagnosis and error alike)."""
    text = row.model_dump_json()
    clean = redact(text)
    return row if clean == text else ResultRow.model_validate_json(clean)


def error_row(runner: EvalRunner, case: EvalCase, exc: BaseException) -> ResultRow:
    row = ResultRow(
        case_id=case.id,
        config=runner.name,
        model=runner.model,
        diagnosis=None,
        raw_answer="",
        error=redact(f"{type(exc).__name__}: {exc}"),
        run=RunStats(
            config=runner.name, model=runner.model, input_tokens=0, output_tokens=0, latency_ms=0
        ),
        timestamp=datetime.now(UTC),
    )
    return redact_row(row)


def _safe_run(runner: EvalRunner, case: EvalCase) -> ResultRow:
    try:
        row = runner.run(case)
    except Exception as exc:
        return error_row(runner, case, exc)
    if row.case_id != case.id:
        return error_row(runner, case, RuntimeError(f"runner returned row for {row.case_id!r}"))
    return redact_row(row)


def _append(handle: IO[str], row: ResultRow) -> None:
    handle.write(redact(row.model_dump_json()) + "\n")
    handle.flush()
    os.fsync(handle.fileno())


def _describe(row: ResultRow) -> str:
    cost = "n/a" if row.run.cost_usd is None else f"${row.run.cost_usd:.4f}"
    if row.error:
        return f"error: {redact(row.error)[:200]} (cost {cost})"
    top = row.diagnosis.causes[0].category.value if row.diagnosis else "unparsed"
    return f"ok: {top} (cost {cost}, {row.run.latency_ms / 1000:.1f} s)"


StopReason = Literal["budget"] | None


def execute(
    runner: EvalRunner,
    cases: Sequence[EvalCase],
    out_path: Path,
    *,
    budget_usd: float,
    concurrency: int,
    log: Callable[[str], None] = print,
) -> tuple[list[ResultRow], StopReason]:
    """Run ``cases`` under the budget guard, appending each row to ``out_path`` on arrival."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pending = deque(cases)
    in_flight: dict[Future[ResultRow], tuple[EvalCase, float]] = {}
    rows: list[ResultRow] = []
    spent = 0.0
    stop: StopReason = None
    total = len(cases)

    with out_path.open("a", encoding="utf-8") as handle, ThreadPoolExecutor(concurrency) as pool:

        def record(row: ResultRow, ceiling: float) -> None:
            nonlocal spent
            try:
                _append(handle, row)
            except OSError as exc:
                # Keep going so in-flight (billed) cases still finish; keep the row visible.
                print(f"error: could not write {out_path}: {exc}", file=sys.stderr)
                print(redact(row.model_dump_json()), file=sys.stderr)
            rows.append(row)
            cost = row.run.cost_usd
            if cost is not None and cost > ceiling:
                log(
                    f"warning: {row.case_id} cost ${cost:.4f}, above its ceiling "
                    f"${ceiling:.4f}; counting the actual cost"
                )
            spent += ceiling if cost is None else cost
            log(f"[{len(rows)}/{total}] {row.case_id} {_describe(row)}; spent ${spent:.4f}")

        while True:
            while pending and stop is None and len(in_flight) < concurrency:
                case = pending[0]
                try:
                    ceiling = runner.cost_ceiling_usd(case)
                except Exception as exc:
                    pending.popleft()
                    record(error_row(runner, case, exc), 0.0)
                    continue
                reserved = sum(c for _, c in in_flight.values())
                if spent + reserved + ceiling > budget_usd:
                    if in_flight:
                        break  # in-flight cases may cost less than reserved; re-check later
                    stop = "budget"
                    log(
                        f"budget: {case.id} could cost up to ${ceiling:.4f}; with "
                        f"${spent:.4f} spent that would pass ${budget_usd:.2f}. "
                        "Starting no new cases."
                    )
                    break
                pending.popleft()
                in_flight[pool.submit(_safe_run, runner, case)] = (case, ceiling)
            if not in_flight:
                break
            done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
            for future in done:
                _, ceiling = in_flight.pop(future)
                record(future.result(), ceiling)

    if stop is not None:
        log(f"budget: {len(pending)} case(s) not run")
    return rows, stop


def dry_run(
    runner: EvalRunner,
    cases: Sequence[EvalCase],
    options: RunOptions,
    out_path: Path,
    *,
    budget_usd: float,
    concurrency: int,
) -> None:
    print(
        f"plan: config={options.config} model={options.model} effort={options.effort} "
        f"cases={len(cases)} concurrency={concurrency} budget=${budget_usd:.2f}"
    )
    print(f"results: {out_path} (append)")
    tokens_total = 0
    counted = 0
    ceiling_total = 0.0
    for case in cases:
        try:
            tokens = runner.estimate_input_tokens(case)
            ceiling = runner.cost_ceiling_usd(case)
        except Exception as exc:
            reason = redact(f"{type(exc).__name__}: {exc}")
            print(f"  {case.id}  {case.split:<4}  estimate failed: {reason}")
            continue
        ceiling_total += ceiling
        if tokens is None:
            shown = "n/a (decided by the agent's tool calls)"
        else:
            counted += 1
            tokens_total += tokens
            shown = f"{tokens:,}"
        print(f"  {case.id}  {case.split:<4}  input tokens {shown}  worst case ${ceiling:.4f}")
    print(
        f"total input tokens {tokens_total:,} ({counted}/{len(cases)} counted); "
        f"worst-case cost ${ceiling_total:.2f}; budget ${budget_usd:.2f}"
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    runner_factory: RunnerFactory = build_runner,
    today: date | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        all_cases = load_cases(args.cases)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    split = args.split or ("all" if args.case else "dev")
    try:
        cases = select_cases(all_cases, split, args.case, args.limit)
    except KeyError as exc:
        print(f"error: unknown case id(s): {exc.args[0]}", file=sys.stderr)
        return 2
    if not cases:
        print(f"no cases match split={split} case={args.case}", file=sys.stderr)
        return 1

    options = RunOptions(
        config=args.config,
        model=args.model,
        effort=args.effort,
        max_input_tokens=args.max_input_tokens,
        max_case_cost_usd=args.max_case_cost_usd,
    )
    try:
        runner = runner_factory(options)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    day = (today or datetime.now(UTC).date()).isoformat()
    out_path = args.results_dir / f"{day}-{args.config}.jsonl"
    if args.dry_run:
        dry_run(
            runner,
            cases,
            options,
            out_path,
            budget_usd=args.max_cost_usd,
            concurrency=args.concurrency,
        )
        return 0

    print(
        f"running {len(cases)} case(s): config={options.config} model={options.model} "
        f"effort={options.effort} budget=${args.max_cost_usd:.2f} -> {out_path}"
    )
    rows, _ = execute(
        runner, cases, out_path, budget_usd=args.max_cost_usd, concurrency=args.concurrency
    )
    errors = sum(1 for r in rows if r.error)
    print(f"wrote {len(rows)} row(s) to {out_path} ({errors} with errors)")
    if rows and errors == len(rows):
        print("error: every case failed; see the error field of each row", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
