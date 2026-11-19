"""Plans and credit packs. The numbers below are PLACEHOLDERS for development: set your own in a JSON file named by
``CLIPCROW_PLANS`` (same shape as ``DEFAULTS``). A plan's price lives in Razorpay (the plan/subscription you create there),
not here; ``price_label`` is only the text the page shows. Nothing is charged by ClipCrow itself.

``monthly_minutes`` is minutes of SOURCE video processed per calendar month (UTC). ``retention_days``: finished jobs
are deleted after that many days; ``source_days``: the uploaded original is deleted after that many days (a clip can
no longer be edited without it, but stays downloadable until the job expires).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Plan:
    id: str
    name: str
    monthly_minutes: float
    retention_days: int
    source_days: int
    razorpay_plan_id: str = ""          # the plan created in the Razorpay dashboard (subscriptions)
    price_label: str = ""               # text only, e.g. "₹999 / month"; leave empty to show no price


@dataclass(frozen=True)
class CreditPack:
    id: str
    minutes: float
    amount_paise: int                   # what the payment link asks for (Razorpay amounts are in paise)
    label: str


@dataclass(frozen=True)
class Payg:
    """Pay as you go: a prepaid balance in minutes of video (the price, the minimum charge, the bonus and the free minutes are all configurable: plans.json, or the environment: CLIPCROW_PRICE_PER_MINUTE in rupees, CLIPCROW_MIN_JOB_MINUTES, CLIPCROW_SIGNUP_BONUS_MINUTES), recharged with any amount from ``min_recharge_paise`` to ``max_recharge_paise``.
    One minute of video uploaded costs one minute of balance. A bigger recharge earns bonus minutes (``bonus_tiers``: (from paise, extra percent)).
    New accounts get ``signup_bonus_minutes`` once. Minutes bought never expire."""
    price_paise_per_minute: int = 800
    min_job_minutes: int = 3                # the least a job is charged, however short the video: a tiny job costs the AI almost as much as a bigger one
    min_recharge_paise: int = 10000
    max_recharge_paise: int = 2_000_000
    signup_bonus_minutes: float = 10.0
    bonus_tiers: tuple = ((200_000, 5), (500_000, 10))

    def bonus_percent(self, amount_paise: int) -> int:
        return max([pct for start, pct in self.bonus_tiers if amount_paise >= start] or [0])

    def minutes_for(self, amount_paise: int) -> float:
        """The minutes a payment of this size buys, bonus included, rounded down to a tenth of a minute. Computed from the PAID amount, never from what a client says."""
        base = amount_paise / self.price_paise_per_minute
        return int(base * (100 + self.bonus_percent(amount_paise)) / 100 * 10) / 10

    def view(self) -> dict:
        """What the pages show (rupees, not paise). No secrets here: this is public."""
        r = lambda paise: paise // 100
        return {"price_per_minute": self.price_paise_per_minute / 100, "min_recharge": r(self.min_recharge_paise), "max_recharge": r(self.max_recharge_paise),
                "signup_bonus_minutes": self.signup_bonus_minutes, "min_job_minutes": self.min_job_minutes,
                "tiers": [{"from": r(a), "bonus_percent": pct} for a, pct in self.bonus_tiers],
                "quick": [{"rupees": r(a), "minutes": self.minutes_for(a), "bonus_percent": self.bonus_percent(a)}
                          for a in (30_000, 50_000, 100_000, 200_000, 500_000) if self.min_recharge_paise <= a <= self.max_recharge_paise]}


@dataclass
class Catalog:
    plans: dict[str, Plan] = field(default_factory=dict)
    packs: dict[str, CreditPack] = field(default_factory=dict)
    payg: Optional[Payg] = None

    def plan(self, plan_id: str) -> Plan:
        return self.plans.get(plan_id) or self.plans["free"]


DEFAULTS = {
    # ONE plan: pay as you go. No subscription; a prepaid balance in minutes of video (see ``Payg``). Change the numbers in a plans.json named by CLIPCROW_PLANS.
    "plans": [
        {"id": "free", "name": "Pay as you go", "monthly_minutes": 0, "retention_days": 30, "source_days": 7, "price_label": "₹8 per minute of video"},
    ],
    "packs": [],
    "payg": {"price_paise_per_minute": 800, "min_job_minutes": 3, "min_recharge_paise": 10000, "max_recharge_paise": 2000000, "signup_bonus_minutes": 10,
             "bonus_tiers": [[200000, 5], [500000, 10]]},
}


def load_catalog(path: str | Path | None = None) -> Catalog:
    path = path or os.environ.get("CLIPCROW_PLANS")
    data = json.loads(Path(path).read_text(encoding="utf-8")) if path else DEFAULTS
    cat = Catalog()
    for p in data.get("plans", []):
        plan = Plan(**p)
        if plan.monthly_minutes < 0 or plan.retention_days < 0 or plan.source_days < 0:
            raise ValueError(f"plan {plan.id!r}: numbers must not be negative")
        cat.plans[plan.id] = plan
    for k in data.get("packs", []):
        pack = CreditPack(**k)
        if pack.minutes <= 0 or pack.amount_paise < 100:
            raise ValueError(f"pack {pack.id!r}: needs minutes > 0 and at least 100 paise")
        cat.packs[pack.id] = pack
    if data.get("payg"):
        raw = dict(data["payg"])
        raw["bonus_tiers"] = tuple(sorted((int(a), int(pct)) for a, pct in raw.get("bonus_tiers", [])))
        env = os.environ
        if env.get("CLIPCROW_PRICE_PER_MINUTE"):
            raw["price_paise_per_minute"] = round(float(env["CLIPCROW_PRICE_PER_MINUTE"]) * 100)
        if env.get("CLIPCROW_MIN_JOB_MINUTES"):
            raw["min_job_minutes"] = int(env["CLIPCROW_MIN_JOB_MINUTES"])
        if env.get("CLIPCROW_SIGNUP_BONUS_MINUTES"):
            raw["signup_bonus_minutes"] = float(env["CLIPCROW_SIGNUP_BONUS_MINUTES"])
        payg = Payg(**raw)
        if payg.price_paise_per_minute < 1 or not 100 <= payg.min_recharge_paise <= payg.max_recharge_paise or payg.signup_bonus_minutes < 0 or payg.min_job_minutes < 1 \
                or any(a < 0 or not 0 <= pct <= 100 for a, pct in payg.bonus_tiers):
            raise ValueError("payg: a price of at least 1 paise a minute, 100 <= min <= max recharge, a bonus of 0 or more, tiers of 0 to 100 percent")
        cat.payg = payg
    if "free" not in cat.plans:
        raise ValueError("the plans must include one with id 'free'")
    return cat
