from __future__ import annotations

import pytest
from pydantic import ValidationError

from evals.schema import EvalCase, ResultRow, Runner


def _case_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "syn-001",
        "split": "dev",
        "question": "PC rebooted by itself while gaming.",
        "scenario": "evals/scenarios/syn-001",
        "expected": {"primary": "power_loss", "acceptable": ["power_loss", "hardware_error"]},
        "tags": ["power"],
        "source": "synthetic",
    }
    payload.update(overrides)
    return payload


def test_eval_case_accepts_valid_payload() -> None:
    case = EvalCase.model_validate(_case_payload())
    assert case.expected.primary == "power_loss"
    assert [c.value for c in case.expected.acceptable] == ["power_loss", "hardware_error"]


def test_eval_case_requires_primary_in_acceptable() -> None:
    with pytest.raises(ValidationError, match="primary"):
        EvalCase.model_validate(
            _case_payload(expected={"primary": "power_loss", "acceptable": ["hardware_error"]})
        )


def test_eval_case_rejects_duplicate_acceptable() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        EvalCase.model_validate(
            _case_payload(expected={"primary": "power_loss", "acceptable": ["power_loss"] * 2})
        )


@pytest.mark.parametrize(
    "override",
    [
        {"split": "train"},
        {"source": "made-up"},
        {"question": ""},
        {"id": "Bad Id"},
        {"unexpected": 1},
        {"expected": {"primary": "not_a_category", "acceptable": ["not_a_category"]}},
    ],
)
def test_eval_case_rejects_bad_fields(override: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        EvalCase.model_validate(_case_payload(**override))


def test_result_row_round_trips_with_and_without_diagnosis(make_row, diag) -> None:
    for row in (make_row(), make_row(diagnosis=diag(["gpu_driver_timeout"]))):
        restored = ResultRow.model_validate_json(row.model_dump_json())
        assert restored == row


def test_result_row_requires_aware_timestamp(make_row) -> None:
    payload = make_row().model_dump(mode="json")
    payload["timestamp"] = "2026-10-14T01:40:00"
    with pytest.raises(ValidationError):
        ResultRow.model_validate(payload)


def test_runner_protocol_is_structural(make_case, make_row) -> None:
    class EchoRunner:
        name = "echo"

        def run(self, case: EvalCase) -> ResultRow:
            return make_row(case.id)

    runner = EchoRunner()
    assert isinstance(runner, Runner)
    assert runner.run(make_case()).case_id == "syn-900"
    assert not isinstance(object(), Runner)


def test_public_view_hides_labels_and_tags() -> None:
    case = EvalCase.model_validate(_case_payload())
    assert case.public() == {
        "id": "syn-001",
        "question": "PC rebooted by itself while gaming.",
        "scenario": "evals/scenarios/syn-001",
    }
