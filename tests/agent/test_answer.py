from __future__ import annotations

import json

import pytest

from tdr_agent.answer import extract_diagnosis

# --------------------------------------------------------------------------- #
# extract_diagnosis
# --------------------------------------------------------------------------- #


def test_extract_valid_block(diag, fence) -> None:
    diagnosis, error = extract_diagnosis(fence(diag(["gpu_driver_timeout", "gpu_thermal"])))
    assert error is None
    assert diagnosis is not None
    assert diagnosis.causes[0].category == "gpu_driver_timeout"


def test_extract_uses_last_json_block(diag, fence) -> None:
    text = fence(diag(["power_loss"])) + "\nRevised:\n" + fence(diag(["storage_io"]))
    diagnosis, _ = extract_diagnosis(text)
    assert diagnosis is not None
    assert diagnosis.causes[0].category == "storage_io"


def test_extract_missing_block_is_reported_not_raised(diag) -> None:
    diagnosis, error = extract_diagnosis(
        "The cause is the GPU driver. " + json.dumps(diag(["gpu_thermal"]))
    )
    assert diagnosis is None
    assert error is not None
    assert "no fenced" in error


def test_extract_unlabelled_fence_is_not_accepted(diag) -> None:
    text = "```\n" + json.dumps(diag(["gpu_thermal"])) + "\n```"
    assert extract_diagnosis(text)[0] is None


def test_extract_malformed_json(fence) -> None:
    diagnosis, error = extract_diagnosis(fence('{"window": {"start": "2026-10-14T01:30:00Z",'))
    assert diagnosis is None
    assert error is not None
    assert "invalid JSON" in error


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["causes"].reverse(),  # ranking no longer descending
        lambda d: d.update(causes=[]),
        lambda d: d["causes"][0].update(category="cosmic_rays"),
        lambda d: d["bug_report"].update(occurred_at="2026-10-14T01:43:08"),  # naive time
        lambda d: d["bug_report"].update(evidence=[]),
        lambda d: d.update(extra_field=1),
    ],
)
def test_extract_schema_violations(diag, fence, mutate) -> None:
    payload = diag(["gpu_driver_timeout", "gpu_thermal", "power_loss"])
    mutate(payload)
    diagnosis, error = extract_diagnosis(fence(payload))
    assert diagnosis is None
    assert error is not None
    assert "validation failed" in error


@pytest.mark.parametrize("garbage", ["", "```json\n[1, 2]\n```", "```json\nnull\n```", "```json"])
def test_extract_never_raises(garbage: str) -> None:
    diagnosis, error = extract_diagnosis(garbage)
    assert diagnosis is None
    assert error
