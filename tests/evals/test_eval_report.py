from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evals import report
from evals.scoring import aggregate

SUMMARY_KEYS = [
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
]


@pytest.fixture
def workspace(tmp_path: Path, make_case, make_row, diag, fence) -> dict[str, Path]:
    cases = [
        make_case("syn-901", primary="gpu_driver_timeout"),
        make_case("syn-902", primary="power_loss", split="test"),
    ]
    cases_path = tmp_path / "cases.jsonl"
    cases_path.write_text("".join(c.model_dump_json() + "\n" for c in cases), encoding="utf-8")

    good = fence(diag(["gpu_driver_timeout"]))
    skill_rows = [
        make_row("syn-901", raw_answer=good, latency_ms=1200),
        make_row("syn-902", raw_answer="not json", latency_ms=800, cost_usd=None),
    ]
    raw_rows = [
        make_row("syn-901", config="raw_dump", raw_answer=good, input_tokens=80000),
        make_row("syn-902", config="raw_dump", raw_answer=good, input_tokens=82000),
    ]
    results = tmp_path / "results"
    results.mkdir()
    skill_path = results / "2026-10-23-skill_tools.jsonl"
    raw_path = results / "2026-10-23-raw_dump.jsonl"
    skill_path.write_text("".join(r.model_dump_json() + "\n" for r in skill_rows), "utf-8")
    raw_path.write_text("\n".join(r.model_dump_json() for r in raw_rows) + "\n\n", "utf-8")
    return {
        "cases": cases_path,
        "skill": skill_path,
        "raw": raw_path,
        "summary": tmp_path / "out" / "summary.json",
    }


def _run(ws: dict[str, Path], *extra: str) -> int:
    return report.main(
        [
            str(ws["skill"]),
            str(ws["raw"]),
            "--cases",
            str(ws["cases"]),
            "--summary-out",
            str(ws["summary"]),
            *extra,
        ]
    )


def test_report_writes_summary_with_exact_shape(workspace, capsys) -> None:
    assert _run(workspace) == 0
    payload = json.loads(workspace["summary"].read_text(encoding="utf-8"))
    assert list(payload) == ["generated_at", "configs"]
    generated = datetime.fromisoformat(payload["generated_at"])
    assert generated.tzinfo is not None
    assert len(payload["configs"]) == 4
    for entry in payload["configs"]:
        assert list(entry) == SUMMARY_KEYS
    by_key = {(c["config"], c["split"]): c for c in payload["configs"]}
    assert by_key[("skill_tools", "dev")]["top1_accuracy"] == 1.0
    assert by_key[("skill_tools", "test")]["valid_rate"] == 0.0
    assert by_key[("skill_tools", "test")]["mean_cost_usd"] is None
    assert by_key[("raw_dump", "test")]["mean_input_tokens"] == 82000.0
    assert by_key[("raw_dump", "test")]["top1_accuracy"] == 0.0
    assert by_key[("raw_dump", "dev")]["cases"] == 1


def test_report_prints_markdown_table(workspace, capsys) -> None:
    assert _run(workspace) == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.startswith("|")]
    assert lines[0].startswith("| Config | Model | Split | N |")
    assert set(lines[1].replace("|", "").strip()) <= {"-", ":", " "}
    assert len(lines) == 2 + 4
    assert "| skill_tools | model-a | test | 1 | 0.0% |" in out
    assert "| raw_dump | model-a | dev | 1 | 100.0% |" in out
    assert "| n/a (0/1) |" in out  # missing cost renders explicitly, with coverage
    assert "one case moves a rate by" in out


def test_render_markdown_matches_summaries(make_case, make_row, diag, fence) -> None:
    rows = [make_row(raw_answer=fence(diag(["gpu_driver_timeout"])), latency_ms=1500)]
    text = report.render_markdown(aggregate(rows, [make_case()]))
    assert "| skill_tools | model-a | dev | 1 | 100.0% | 100.0% |" in text
    assert "1.50" in text  # latency rendered in seconds
    assert "| 0.0100 (1/1) |" in text


def test_summary_payload_is_json_serialisable(make_case, make_row) -> None:
    summaries = aggregate([make_row()], [make_case()])
    stamp = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    payload = report.summary_payload(summaries, stamp)
    assert payload["generated_at"] == "2026-10-23T12:00:00+00:00"
    assert json.loads(json.dumps(payload)) == payload


def test_report_default_glob_reads_results_dir(workspace, monkeypatch) -> None:
    monkeypatch.setattr(report, "RESULTS_DIR", workspace["skill"].parent)
    code = report.main(
        ["--cases", str(workspace["cases"]), "--summary-out", str(workspace["summary"])]
    )
    assert code == 0
    payload = json.loads(workspace["summary"].read_text(encoding="utf-8"))
    assert {c["config"] for c in payload["configs"]} == {"skill_tools", "raw_dump"}


def test_report_ignores_summary_json_when_globbing(workspace, monkeypatch) -> None:
    results_dir = workspace["skill"].parent
    (results_dir / "summary.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(report, "RESULTS_DIR", results_dir)
    assert (
        report.main(
            ["--cases", str(workspace["cases"]), "--summary-out", str(workspace["summary"])]
        )
        == 0
    )


def test_report_without_results_fails(tmp_path, workspace, monkeypatch, capsys) -> None:
    monkeypatch.setattr(report, "RESULTS_DIR", tmp_path / "empty")
    code = report.main(
        ["--cases", str(workspace["cases"]), "--summary-out", str(workspace["summary"])]
    )
    assert code == 1
    assert "no result files" in capsys.readouterr().err
    assert not workspace["summary"].exists()


def test_report_rejects_corrupt_line(workspace, capsys) -> None:
    with workspace["raw"].open("a", encoding="utf-8") as fh:
        fh.write('{"case_id": "syn-901"}\n')
    assert _run(workspace) == 2
    err = capsys.readouterr().err
    assert workspace["raw"].name in err
    assert ":4" in err  # line number of the corrupt row
    assert not workspace["summary"].exists()


def test_report_rejects_unknown_case(workspace, make_row, capsys) -> None:
    with workspace["raw"].open("a", encoding="utf-8") as fh:
        fh.write(make_row("syn-777", config="raw_dump").model_dump_json() + "\n")
    assert _run(workspace) == 2
    assert "syn-777" in capsys.readouterr().err
