from __future__ import annotations

import pytest

from evals import pricing


def test_opus_5_5_rates_and_source_date() -> None:
    rates = pricing.pricing_for("claude-opus-5-5")
    assert rates.input_per_mtok == 4.00
    assert rates.output_per_mtok == 20.00
    assert rates.cache_read_per_mtok == 0.20
    assert pricing.PRICES_AS_OF == "2026-10-06"


def test_cost_combines_every_token_kind() -> None:
    cost = pricing.cost_usd(
        "claude-opus-5-5",
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_read_tokens=500_000,
        cache_write_tokens=0,
    )
    assert cost == pytest.approx(4.00 + 2.00 + 0.10)


def test_cost_of_cache_writes_uses_write_rate() -> None:
    rates = pricing.pricing_for("claude-opus-5-5")
    cost = pricing.cost_usd(
        "claude-opus-5-5", input_tokens=0, output_tokens=0, cache_write_tokens=1_000_000
    )
    assert cost == pytest.approx(rates.cache_write_per_mtok)


def test_unknown_model_is_an_error_not_a_free_run() -> None:
    with pytest.raises(pricing.UnknownModelPricing, match="claude-unknown"):
        pricing.cost_usd("claude-unknown", input_tokens=1, output_tokens=1)
