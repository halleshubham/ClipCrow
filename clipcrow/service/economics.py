"""What a job cost to make against what it earned, and what price a target margin needs.

``markup`` is the figure the operator sets (``CLIPCROW_MARGIN_PERCENT``: price = cost x (1 + markup / 100); 100 means the price is twice the cost). The profit
share of the price is shown too (a 100% markup is a 50% profit share). Pure functions: the numbers come from the job's AI usage and metered GPU time.
"""
from __future__ import annotations

import math
import os
import statistics
from typing import Iterable, Optional

DEFAULT_MARKUP = 50.0


def target_markup() -> float:
    """The markup the operator wants, in percent: ``CLIPCROW_MARGIN_PERCENT`` (default 50), at most 1000."""
    try:
        return min(1000.0, max(0.0, float(os.environ.get("CLIPCROW_MARGIN_PERCENT") or DEFAULT_MARKUP)))
    except ValueError:
        return DEFAULT_MARKUP


def economics(revenue: float, cost: float, markup_target: float) -> dict:
    """One job: what it was charged at the list price (free-trial minutes included, at list price), what it cost, and how that compares with the target."""
    profit = revenue - cost
    markup = (revenue / cost - 1) * 100 if cost > 0 else None
    return {"revenue": round(revenue, 2), "cost": round(cost, 2), "profit": round(profit, 2),
            "markup_percent": None if markup is None else round(markup),
            "profit_share_percent": round(profit / revenue * 100) if revenue > 0 else None,
            "target_markup_percent": round(markup_target),
            "below_target": bool(revenue > 0 and cost > 0 and markup is not None and markup < markup_target)}


def suggest_price(costs_per_minute: Iterable[float], markup: float, *, step: float = 0.5) -> Optional[float]:
    """The price a minute needs so that most jobs (the 75th percentile of the cost, so not just the cheap ones) reach the target markup, rounded UP to ``step`` rupees."""
    xs = sorted(c for c in costs_per_minute if c > 0)
    if not xs:
        return None
    p75 = xs[min(len(xs) - 1, math.ceil(0.75 * len(xs)) - 1)] if len(xs) > 1 else xs[0]
    return math.ceil(p75 * (1 + markup / 100) / step) * step


def suggest_min_minutes(costs_per_job: Iterable[float], price_per_minute: float, markup: float) -> int:
    """The minimum charge (in minutes of balance) that makes even a tiny job reach the target: the median cost of the short jobs x (1 + markup), over the price per minute."""
    xs = [c for c in costs_per_job if c > 0]
    if not xs or price_per_minute <= 0:
        return 1
    return max(1, math.ceil(statistics.median(xs) * (1 + markup / 100) / price_per_minute))
