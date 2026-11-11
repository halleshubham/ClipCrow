"""Estimated LLM cost from list prices (USD per million tokens: input, output).

An estimate for budgeting and the usage record, not an invoice: prices change, caching and
batch discounts are ignored, and models not in the table give ``None``. Prices as published
2026-11-03; update them here.
"""
from __future__ import annotations

PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-2": (8.0, 40.0),
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-5-5": (0.10, 0.50),     # prompts up to 100K tokens
    "claude-haiku-4-5": (1.0, 5.0),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    p = PRICES.get(model)
    if p is None:
        return None
    return (input_tokens * p[0] + output_tokens * p[1]) / 1_000_000


def estimate_total(calls) -> float | None:
    """Sum over calls; None if any call's model has no known price (a partial sum would mislead)."""
    total = 0.0
    for c in calls:
        cost = estimate_cost(c.model, c.input_tokens, c.output_tokens)
        if cost is None:
            return None
        total += cost
    return round(total, 6)
