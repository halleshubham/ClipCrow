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


@dataclass
class Catalog:
    plans: dict[str, Plan] = field(default_factory=dict)
    packs: dict[str, CreditPack] = field(default_factory=dict)

    def plan(self, plan_id: str) -> Plan:
        return self.plans.get(plan_id) or self.plans["free"]


DEFAULTS = {
    "plans": [
        {"id": "free", "name": "Free", "monthly_minutes": 30, "retention_days": 7, "source_days": 3},
        {"id": "pro", "name": "Pro", "monthly_minutes": 600, "retention_days": 30, "source_days": 14,
         "razorpay_plan_id": "", "price_label": ""},
    ],
    "packs": [],
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
    if "free" not in cat.plans:
        raise ValueError("the plans must include one with id 'free'")
    return cat
