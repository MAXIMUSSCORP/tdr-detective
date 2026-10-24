"""Token prices for eval cost accounting: the single place cost is computed.

Source: the claude-api skill's "Current Models" table, cached 2026-10-06. Claude Opus 5.5
(``claude-opus-5-5``) costs $4.00 per million input tokens, $20.00 per million output tokens
and $0.20 per million cache-read tokens. Cache writes (5-minute TTL) are billed at 1.25x
the input rate, per the same skill's prompt-caching notes. The raw_dump baseline does not
request caching, so it should never see cache writes; they are priced anyway, so that a
nonzero count cannot pass as free.

Update ``PRICES_AS_OF`` together with the table. A model missing from the table is an
error, never a zero cost.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

PRICES_AS_OF = "2026-10-06"
PRICES_SOURCE = "claude-api skill, Current Models table (cached 2026-10-06)"

_PER_TOKEN = 1 / 1_000_000


@dataclass(frozen=True)
class ModelPricing:
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float
    cache_write_per_mtok: float


PRICING: Mapping[str, ModelPricing] = {
    "claude-opus-5-5": ModelPricing(
        input_per_mtok=4.00,
        output_per_mtok=20.00,
        cache_read_per_mtok=0.20,
        cache_write_per_mtok=4.00 * 1.25,
    ),
}


class UnknownModelPricing(KeyError):
    """Raised for a model with no entry in ``PRICING``."""


def pricing_for(model: str) -> ModelPricing:
    try:
        return PRICING[model]
    except KeyError:
        raise UnknownModelPricing(
            f"no price for model {model!r}; add it to evals/pricing.py ({PRICES_SOURCE})"
        ) from None


def cost_usd(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """USD cost of one request. ``input_tokens`` excludes cache reads and cache writes."""
    p = pricing_for(model)
    total = (
        input_tokens * p.input_per_mtok
        + output_tokens * p.output_per_mtok
        + cache_read_tokens * p.cache_read_per_mtok
        + cache_write_tokens * p.cache_write_per_mtok
    ) * _PER_TOKEN
    return round(total, 6)
