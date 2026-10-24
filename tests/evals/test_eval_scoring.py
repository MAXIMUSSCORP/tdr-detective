from __future__ import annotations

import pytest

from evals.scoring import aggregate, percentile, score_case

# --------------------------------------------------------------------------- #
# score_case
# --------------------------------------------------------------------------- #


def test_score_good_answer(make_case, make_row, diag, fence) -> None:
    case = make_case(primary="gpu_driver_timeout")
    row = make_row(raw_answer=fence(diag(["gpu_driver_timeout", "gpu_thermal"])))
    score = score_case(case, row)
    assert score.valid
    assert score.top1
    assert score.top3
    assert score.acceptable_top1
    assert score.report_complete
    assert score.cites_evidence
    assert score.error is None
    assert score.split == "dev"


def test_score_primary_second_counts_for_top3_only(make_case, make_row, diag, fence) -> None:
    case = make_case(primary="gpu_thermal")
    row = make_row(raw_answer=fence(diag(["gpu_driver_timeout", "gpu_thermal"])))
    score = score_case(case, row)
    assert score.valid
    assert not score.top1
    assert not score.acceptable_top1
    assert score.top3


def test_score_primary_fourth_misses_top3(make_case, make_row, diag, fence) -> None:
    case = make_case(primary="storage_io")
    cats = ["power_loss", "hardware_error", "gpu_thermal", "storage_io"]
    score = score_case(case, make_row(raw_answer=fence(diag(cats))))
    assert not score.top1
    assert not score.top3


def test_score_acceptable_alternative(make_case, make_row, diag, fence) -> None:
    case = make_case(primary="gpu_thermal", acceptable=["gpu_thermal", "gpu_driver_timeout"])
    score = score_case(case, make_row(raw_answer=fence(diag(["gpu_driver_timeout"]))))
    assert not score.top1
    assert score.acceptable_top1
    assert score.top3


def test_score_bad_answer(make_case, make_row, diag, fence) -> None:
    case = make_case(primary="insufficient_evidence")
    row = make_row(raw_answer=fence(diag(["gpu_driver_timeout", "power_loss", "storage_io"])))
    score = score_case(case, row)
    assert score.valid
    assert not (score.top1 or score.top3 or score.acceptable_top1)


@pytest.mark.parametrize(
    "raw",
    ["I think it was the driver.", '```json\n{"causes": [\n```', "```json\n{}\n```"],
)
def test_score_unparseable_answers_are_failures(make_case, make_row, raw: str) -> None:
    score = score_case(make_case(), make_row(raw_answer=raw))
    assert not score.valid
    assert not (score.top1 or score.top3 or score.acceptable_top1)
    assert not (score.report_complete or score.cites_evidence)
    assert score.error


def test_score_prefers_structured_diagnosis(make_case, make_row, diag) -> None:
    row = make_row(raw_answer="", diagnosis=diag(["gpu_driver_timeout"]))
    score = score_case(make_case(), row)
    assert score.valid
    assert score.top1


def test_score_runner_error_without_diagnosis(make_case, make_row) -> None:
    score = score_case(make_case(), make_row(error="timeout after 600 s"))
    assert not score.valid
    assert score.error is not None
    assert "timeout after 600 s" in score.error


@pytest.mark.parametrize(
    "report_patch",
    [
        {"title": "   "},
        {"gpu": ""},
        {"summary": ""},
        {"steps_to_reproduce": []},
        {"steps_to_reproduce": ["Play a game", " "]},
        {"evidence": [""]},
    ],
)
def test_report_completeness(make_case, make_row, diag, fence, report_patch) -> None:
    row = make_row(raw_answer=fence(diag(["gpu_driver_timeout"], bug_report=report_patch)))
    score = score_case(make_case(), row)
    assert score.valid
    assert not score.report_complete


def test_cites_evidence_requires_refs_on_top_cause(make_case, make_row, diag, fence) -> None:
    row = make_row(raw_answer=fence(diag(["gpu_driver_timeout"], refs=())))
    score = score_case(make_case(), row)
    assert score.valid
    assert not score.cites_evidence


def test_score_case_rejects_mismatched_ids(make_case, make_row) -> None:
    with pytest.raises(ValueError, match="syn-901"):
        score_case(make_case("syn-900"), make_row("syn-901"))


# --------------------------------------------------------------------------- #
# aggregation
# --------------------------------------------------------------------------- #


def test_percentile_nearest_rank() -> None:
    assert percentile([5.0], 95) == 5.0
    assert percentile([float(v) for v in range(1, 21)], 95) == 19.0
    assert percentile([float(v) for v in range(1, 101)], 95) == 95.0
    assert percentile([3.0, 1.0, 2.0], 50) == 2.0
    with pytest.raises(ValueError):
        percentile([], 95)


def test_aggregate_math(make_case, make_row, diag, fence) -> None:
    cases = [
        make_case("syn-901", primary="gpu_driver_timeout", split="dev"),
        make_case("syn-902", primary="power_loss", split="dev"),
        make_case(
            "syn-903", primary="gpu_thermal", acceptable=["gpu_thermal", "gpu_driver_timeout"]
        ),
        make_case("syn-904", primary="storage_io", split="test"),
    ]
    good = fence(diag(["gpu_driver_timeout"]))
    rows = [
        # skill_tools / dev: one exact hit, one top-3 hit, one acceptable-only hit
        make_row("syn-901", raw_answer=good, input_tokens=100, output_tokens=10, latency_ms=100),
        make_row(
            "syn-902",
            raw_answer=fence(diag(["storage_io", "power_loss"], refs=())),
            input_tokens=300,
            output_tokens=30,
            cost_usd=0.03,
            latency_ms=300,
        ),
        make_row("syn-903", raw_answer=good, input_tokens=200, output_tokens=20, latency_ms=200),
        # skill_tools / test: an unparseable answer
        make_row("syn-904", raw_answer="no idea", input_tokens=50, output_tokens=5, cost_usd=None),
        # raw_dump / dev: no cost reported anywhere
        make_row(
            "syn-901",
            config="raw_dump",
            raw_answer=good,
            input_tokens=90000,
            output_tokens=900,
            cost_usd=None,
            latency_ms=9000,
        ),
    ]
    summaries = aggregate(rows, cases)
    keyed = {(s.config, s.model, s.split): s for s in summaries}
    assert list(keyed) == [
        ("raw_dump", "model-a", "dev"),
        ("skill_tools", "model-a", "dev"),
        ("skill_tools", "model-a", "test"),
    ]

    dev = keyed[("skill_tools", "model-a", "dev")]
    assert dev.cases == 3
    assert dev.top1_accuracy == pytest.approx(1 / 3)
    assert dev.top3_accuracy == pytest.approx(1.0)
    assert dev.acceptable_top1_accuracy == pytest.approx(2 / 3)
    assert dev.valid_rate == pytest.approx(1.0)
    assert dev.report_complete_rate == pytest.approx(1.0)
    assert dev.cites_evidence_rate == pytest.approx(2 / 3)
    assert dev.mean_input_tokens == pytest.approx(200.0)
    assert dev.median_input_tokens == pytest.approx(200.0)
    assert dev.mean_output_tokens == pytest.approx(20.0)
    assert dev.median_output_tokens == pytest.approx(20.0)
    assert dev.mean_cost_usd == pytest.approx((0.01 + 0.03 + 0.01) / 3)
    assert dev.cost_reported == 3
    assert dev.mean_latency_ms == pytest.approx(200.0)
    assert dev.p95_latency_ms == pytest.approx(300.0)

    test = keyed[("skill_tools", "model-a", "test")]
    assert test.cases == 1
    assert test.valid_rate == 0.0
    assert test.top1_accuracy == 0.0
    assert test.report_complete_rate == 0.0
    assert test.mean_cost_usd is None
    assert test.cost_reported == 0

    raw = keyed[("raw_dump", "model-a", "dev")]
    assert raw.cases == 1
    assert raw.top1_accuracy == 1.0
    assert raw.mean_input_tokens == 90000.0
    assert raw.mean_cost_usd is None
    assert raw.p95_latency_ms == 9000.0


def test_aggregate_median_even_count(make_case, make_row) -> None:
    cases = [make_case(f"syn-90{i}") for i in range(4)]
    rows = [make_row(f"syn-90{i}", input_tokens=t) for i, t in enumerate([10, 40, 20, 1000])]
    (summary,) = aggregate(rows, cases)
    assert summary.median_input_tokens == pytest.approx(30.0)
    assert summary.mean_input_tokens == pytest.approx(267.5)


def test_aggregate_rejects_unknown_case(make_case, make_row) -> None:
    with pytest.raises(ValueError, match="syn-999"):
        aggregate([make_row("syn-999")], [make_case("syn-900")])


def test_aggregate_empty() -> None:
    assert aggregate([], []) == []
