"""Tests for ``python -m evals.run`` with fake runners (no model calls)."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from evals import run as eval_run
from evals.schema import EvalCase, ResultRow
from pc_diag_mcp.models import Diagnosis, RunStats

TODAY = date(2026, 10, 23)


def write_cases(path: Path, specs: list[tuple[str, str]]) -> Path:
    lines = []
    for case_id, split in specs:
        lines.append(
            json.dumps(
                {
                    "id": case_id,
                    "split": split,
                    "question": f"What happened in {case_id}?",
                    "scenario": f"evals/scenarios/{case_id}",
                    "expected": {"primary": "storage_io", "acceptable": ["storage_io"]},
                    "tags": [],
                    "source": "synthetic",
                }
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _answer(diag: Callable[..., dict[str, Any]], fence: Callable[[Any], str]) -> None:
    ANSWER["diagnosis"] = diag(["storage_io"])
    ANSWER["text"] = fence(ANSWER["diagnosis"])


ANSWER: dict[str, Any] = {}


@pytest.fixture
def cases_file(tmp_path: Path) -> Path:
    return write_cases(
        tmp_path / "cases.jsonl",
        [("syn-001", "test"), ("syn-002", "dev"), ("syn-003", "dev"), ("syn-004", "dev")],
    )


class FakeRunner:
    """A runner that answers instantly; per-case behaviour is configurable."""

    def __init__(
        self,
        name: str = "raw_dump",
        *,
        cost: float | None = 0.5,
        ceiling: float = 1.0,
        tokens: int | None = 1234,
        fail: set[str] | None = None,
        on_run: Callable[[EvalCase], None] | None = None,
    ) -> None:
        self.name = name
        self.model = "claude-opus-5-5"
        self.cost = cost
        self.ceiling = ceiling
        self.tokens = tokens
        self.fail = fail or set()
        self.on_run = on_run
        self.ran: list[str] = []
        self.lock = threading.Lock()

    def estimate_input_tokens(self, case: EvalCase) -> int | None:
        return self.tokens

    def cost_ceiling_usd(self, case: EvalCase) -> float:
        return self.ceiling

    def run(self, case: EvalCase) -> ResultRow:
        with self.lock:
            self.ran.append(case.id)
        if self.on_run:
            self.on_run(case)
        if case.id in self.fail:
            raise RuntimeError(f"boom in {case.id}")
        return ResultRow(
            case_id=case.id,
            config=self.name,
            model=self.model,
            diagnosis=Diagnosis.model_validate(ANSWER["diagnosis"]),
            raw_answer=ANSWER["text"],
            error=None,
            run=RunStats(
                config=self.name,
                model=self.model,
                input_tokens=100,
                output_tokens=10,
                cost_usd=self.cost,
                latency_ms=5,
            ),
            timestamp=datetime(2026, 10, 23, 12, 0, tzinfo=UTC),
        )


def invoke(
    runner: FakeRunner,
    cases_file: Path,
    results_dir: Path,
    *args: str,
    seen: dict[str, Any] | None = None,
) -> int:
    def factory(options: eval_run.RunOptions) -> FakeRunner:
        if seen is not None:
            seen["options"] = options
        return runner

    argv = [
        "--config",
        runner.name,
        "--cases",
        str(cases_file),
        "--results-dir",
        str(results_dir),
        *args,
    ]
    return eval_run.main(argv, runner_factory=factory, today=TODAY)


def read_rows(path: Path) -> list[ResultRow]:
    return [
        ResultRow.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def results_path(results_dir: Path, config: str = "raw_dump") -> Path:
    return results_dir / f"2026-10-23-{config}.jsonl"


# --------------------------------------------------------------------------- #
# Case selection
# --------------------------------------------------------------------------- #


def test_split_dev_runs_only_dev_cases(cases_file: Path, tmp_path: Path) -> None:
    runner = FakeRunner()
    assert invoke(runner, cases_file, tmp_path / "out", "--split", "dev") == 0
    assert sorted(runner.ran) == ["syn-002", "syn-003", "syn-004"]


def test_split_all_and_limit(cases_file: Path, tmp_path: Path) -> None:
    runner = FakeRunner()
    invoke(runner, cases_file, tmp_path / "out", "--split", "all", "--limit", "2")
    assert sorted(runner.ran) == ["syn-001", "syn-002"]


def test_case_filter_is_repeatable(cases_file: Path, tmp_path: Path) -> None:
    runner = FakeRunner()
    invoke(
        runner,
        cases_file,
        tmp_path / "out",
        "--split",
        "all",
        "--case",
        "syn-003",
        "--case",
        "syn-001",
    )
    assert sorted(runner.ran) == ["syn-001", "syn-003"]


def test_unknown_case_id_is_a_usage_error(
    cases_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeRunner()
    assert invoke(runner, cases_file, tmp_path / "out", "--case", "syn-999") == 2
    assert "syn-999" in capsys.readouterr().err
    assert runner.ran == []


def test_no_matching_cases_exits_1(tmp_path: Path) -> None:
    cases = write_cases(tmp_path / "cases.jsonl", [("syn-001", "test")])
    runner = FakeRunner()
    assert invoke(runner, cases, tmp_path / "out", "--split", "dev") == 1
    assert runner.ran == []


def test_options_reach_the_runner_factory(cases_file: Path, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}
    invoke(
        FakeRunner(),
        cases_file,
        tmp_path / "out",
        "--model",
        "claude-opus-5-5",
        "--effort",
        "high",
        "--max-input-tokens",
        "90000",
        "--max-case-cost-usd",
        "0.4",
        seen=seen,
    )
    options = seen["options"]
    assert options.config == "raw_dump"
    assert options.effort == "high"
    assert options.max_input_tokens == 90_000
    assert options.max_case_cost_usd == 0.4


def test_default_effort_is_medium_for_both_configs() -> None:
    parser = eval_run.build_parser()
    for config in ("raw_dump", "skill_tools"):
        args = parser.parse_args(["--config", config])
        assert args.effort == "medium"
        assert args.model == "claude-opus-5-5"
        assert args.max_cost_usd == 15.0
        assert args.concurrency == 2


# --------------------------------------------------------------------------- #
# Row writing
# --------------------------------------------------------------------------- #


def test_rows_are_appended_to_dated_config_file(cases_file: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    invoke(FakeRunner(), cases_file, out, "--split", "dev")
    invoke(FakeRunner(), cases_file, out, "--split", "test")
    rows = read_rows(results_path(out))
    assert sorted(r.case_id for r in rows) == ["syn-001", "syn-002", "syn-003", "syn-004"]
    assert all(r.config == "raw_dump" for r in rows)


def test_each_row_is_on_disk_before_the_next_case_starts(cases_file: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    observed: list[int] = []

    def check(case: EvalCase) -> None:
        path = results_path(out)
        observed.append(len(read_rows(path)) if path.exists() else 0)

    runner = FakeRunner(on_run=check)
    invoke(runner, cases_file, out, "--split", "dev", "--concurrency", "1")
    assert observed == [0, 1, 2]


def test_concurrent_run_writes_every_row_once(cases_file: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    runner = FakeRunner(ceiling=0.0, cost=0.0)
    invoke(runner, cases_file, out, "--split", "all", "--concurrency", "3")
    rows = read_rows(results_path(out))
    assert sorted(r.case_id for r in rows) == ["syn-001", "syn-002", "syn-003", "syn-004"]


def test_runner_exception_becomes_an_error_row(
    cases_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    runner = FakeRunner(fail={"syn-003"})
    assert invoke(runner, cases_file, out, "--split", "dev", "--concurrency", "1") == 0
    rows = {r.case_id: r for r in read_rows(results_path(out))}
    assert set(rows) == {"syn-002", "syn-003", "syn-004"}
    failed = rows["syn-003"]
    assert failed.error == "RuntimeError: boom in syn-003"
    assert failed.diagnosis is None and failed.raw_answer == ""
    assert failed.config == "raw_dump" and failed.model == "claude-opus-5-5"
    assert failed.run.cost_usd is None
    assert "syn-003" in capsys.readouterr().out


def test_run_where_every_case_fails_exits_3(cases_file: Path, tmp_path: Path) -> None:
    runner = FakeRunner(fail={"syn-002", "syn-003", "syn-004"})
    assert invoke(runner, cases_file, tmp_path / "out", "--split", "dev") == 3
    assert len(read_rows(results_path(tmp_path / "out"))) == 3


def test_write_failure_does_not_abort_the_run(
    cases_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = {"n": 0}
    real_append = eval_run._append

    def flaky(handle: Any, row: ResultRow) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk full")
        real_append(handle, row)

    monkeypatch.setattr(eval_run, "_append", flaky)
    runner = FakeRunner()
    out = tmp_path / "out"
    assert invoke(runner, cases_file, out, "--split", "dev", "--concurrency", "1") == 0
    assert len(runner.ran) == 3
    assert len(read_rows(results_path(out))) == 2
    err = capsys.readouterr().err
    assert "disk full" in err and '"case_id":"syn-002"' in err


def test_cost_above_ceiling_is_counted_and_warned(
    cases_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeRunner(cost=2.0, ceiling=1.0)
    invoke(
        runner,
        cases_file,
        tmp_path / "out",
        "--split",
        "all",
        "--max-cost-usd",
        "2.5",
        "--concurrency",
        "1",
    )
    assert len(runner.ran) == 1
    assert "above its ceiling" in capsys.readouterr().out


def test_error_text_never_contains_the_api_key(
    cases_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-ant-test-0123456789abcdef"  # noqa: S105 (fake value)
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)

    class LeakyRunner(FakeRunner):
        def run(self, case: EvalCase) -> ResultRow:
            raise RuntimeError(f"auth failed for key {secret}")

    out = tmp_path / "out"
    invoke(LeakyRunner(), cases_file, out, "--case", "syn-002")
    text = results_path(out).read_text(encoding="utf-8")
    assert secret not in text
    assert "[redacted]" in text


def test_ceiling_failure_becomes_an_error_row(cases_file: Path, tmp_path: Path) -> None:
    class CountFails(FakeRunner):
        def cost_ceiling_usd(self, case: EvalCase) -> float:
            raise ConnectionError("count_tokens unreachable")

    out = tmp_path / "out"
    runner = CountFails()
    invoke(runner, cases_file, out, "--case", "syn-002")
    (row,) = read_rows(results_path(out))
    assert row.error == "ConnectionError: count_tokens unreachable"
    assert runner.ran == []


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #


def test_budget_stops_new_cases_before_it_would_be_exceeded(
    cases_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    runner = FakeRunner(cost=1.0, ceiling=1.0)
    code = invoke(
        runner, cases_file, out, "--split", "all", "--max-cost-usd", "2.5", "--concurrency", "1"
    )
    assert code == 0
    assert len(runner.ran) == 2
    assert len(read_rows(results_path(out))) == 2
    printed = capsys.readouterr().out
    assert "budget" in printed and "2 case(s) not run" in printed


def test_budget_uses_actual_cost_once_known(cases_file: Path, tmp_path: Path) -> None:
    # Ceiling 1.0 but real cost 0.1: 4 cases fit in 1.5 because spend is re-measured.
    runner = FakeRunner(cost=0.1, ceiling=1.0)
    invoke(
        runner,
        cases_file,
        tmp_path / "out",
        "--split",
        "all",
        "--max-cost-usd",
        "1.5",
        "--concurrency",
        "1",
    )
    assert len(runner.ran) == 4


def test_budget_reserves_ceilings_of_cases_in_flight(cases_file: Path, tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    concurrent_peak: list[int] = []
    active = [0]
    lock = threading.Lock()

    def hold(case: EvalCase) -> None:
        with lock:
            active[0] += 1
            concurrent_peak.append(active[0])
        started.set()
        release.wait(timeout=0.2)
        with lock:
            active[0] -= 1

    runner = FakeRunner(cost=1.0, ceiling=1.0, on_run=hold)
    invoke(
        runner,
        cases_file,
        tmp_path / "out",
        "--split",
        "all",
        "--max-cost-usd",
        "1.5",
        "--concurrency",
        "4",
    )
    # Only one 1.0 ceiling fits in 1.5 at a time, and after it costs 1.0 no second one fits.
    assert max(concurrent_peak) == 1
    assert len(runner.ran) == 1


def test_unknown_cost_counts_as_its_ceiling(cases_file: Path, tmp_path: Path) -> None:
    runner = FakeRunner(cost=None, ceiling=1.0)
    invoke(
        runner,
        cases_file,
        tmp_path / "out",
        "--split",
        "all",
        "--max-cost-usd",
        "2.0",
        "--concurrency",
        "1",
    )
    assert len(runner.ran) == 2


# --------------------------------------------------------------------------- #
# Dry run
# --------------------------------------------------------------------------- #


def test_dry_run_prints_plan_and_calls_no_model(
    cases_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    runner = FakeRunner(tokens=31_337, ceiling=0.45)
    assert invoke(runner, cases_file, out, "--split", "dev", "--dry-run") == 0
    assert runner.ran == []
    assert not out.exists()
    printed = capsys.readouterr().out
    for case_id in ("syn-002", "syn-003", "syn-004"):
        assert case_id in printed
    assert "31,337" in printed
    assert "94,011" in printed  # 3 x 31,337
    assert "$1.35" in printed  # 3 x 0.45 worst case
    assert "claude-opus-5-5" in printed and "medium" in printed
    assert str(results_path(out)) in printed


def test_dry_run_without_token_estimates(
    cases_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeRunner("skill_tools", tokens=None)
    assert invoke(runner, cases_file, tmp_path / "out", "--case", "syn-002", "--dry-run") == 0
    printed = capsys.readouterr().out
    assert "n/a" in printed
    assert runner.ran == []


def test_dry_run_reports_a_failed_estimate(
    cases_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class CountFails(FakeRunner):
        def estimate_input_tokens(self, case: EvalCase) -> int | None:
            raise ConnectionError("offline")

    runner = CountFails()
    assert invoke(runner, cases_file, tmp_path / "out", "--case", "syn-002", "--dry-run") == 0
    assert "ConnectionError: offline" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Runner factory
# --------------------------------------------------------------------------- #


def test_default_factory_builds_both_configs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-unused-in-this-test")
    base = {
        "model": "claude-opus-5-5",
        "effort": "low",
        "max_input_tokens": 1000,
        "max_case_cost_usd": 0.5,
    }
    raw = eval_run.build_runner(eval_run.RunOptions(config="raw_dump", **base))
    tools = eval_run.build_runner(eval_run.RunOptions(config="skill_tools", **base))
    assert raw.name == "raw_dump" and tools.name == "skill_tools"
    assert raw.model == tools.model == "claude-opus-5-5"
