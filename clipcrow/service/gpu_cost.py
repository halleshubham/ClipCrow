"""What a job's GPU time costs when a worker is a rented machine.

A worker the person owns is free. A rented one has an hourly rate (``CLIPCROW_GPU_RATES``, JSON, worker name -> INR an hour, e.g. ``{"race-3090": 45}``), and the time a task kept
it busy is charged to the job that asked for it. Time a rented machine spends waiting for work is NOT charged to a job: it is the operator's cost of keeping it running.
"""
from __future__ import annotations

import json
import os
import re
from typing import Callable

CURRENCY = "INR"
_KIND = {"transcribe": "Speech recognition", "render": "Render", "speakers": "Who is who"}


def load_rates(raw: str | None = None) -> dict[str, float]:
    """Worker name -> INR an hour from ``CLIPCROW_GPU_RATES``; anything that is not a name with a non-negative number is ignored."""
    raw = os.environ.get("CLIPCROW_GPU_RATES", "") if raw is None else raw
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k)[:40]: float(v) for k, v in data.items() if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 100000}


def rate_lookup(rates: dict[str, float]) -> Callable[[str], float]:
    """Hourly price of a worker: its own entry, else the ``"*"`` entry (one price for every machine, a home GPU included), else 0 (not charged).
    An extra render slot (``<machine>.r2``) is the same machine."""
    return lambda name: rates.get(re.sub(r"\.r\d{1,2}\Z", "", name), rates.get("*", 0.0))


def row_cost(seconds: float, rate: float) -> float:
    return round(max(0.0, seconds) / 3600.0 * max(0.0, rate), 2)


def summarise(rows: list[dict]) -> dict:
    """The GPU section of a job's usage: one line per task, a total of seconds and of money (rupees)."""
    lines = []
    for r in rows:
        try:
            seconds, rate = float(r.get("seconds", 0.0)), float(r.get("rate", 0.0))
        except (TypeError, ValueError):
            continue
        lines.append({"kind": str(r.get("kind", "")), "step": _KIND.get(str(r.get("kind", "")), str(r.get("kind", ""))), "worker": str(r.get("worker", ""))[:40],
                      "seconds": round(seconds, 1), "ok": bool(r.get("ok", True)), "rate_per_hour": rate, "est_cost": row_cost(seconds, rate)})
    return {"currency": CURRENCY, "tasks": lines, "seconds": round(sum(x["seconds"] for x in lines), 1),
            "est_cost": round(sum(x["est_cost"] for x in lines), 2), "rented": any(x["rate_per_hour"] > 0 for x in lines)}


# Rough planning numbers, NOT measurements of any one machine: GPU seconds per second of source video for speech recognition, for telling
# people apart (only when several are on screen), and seconds per finished reel for rendering; a pod that sleeps also needs a minute or two to wake.
LISTEN_PER_SECOND = 0.07
WHO_PER_SECOND = 0.03
RENDER_PER_REEL = 40.0
WAKE_SECONDS = 90.0


def estimate(duration: float, clips: int, rate: float, *, multi: bool = False, wake: bool = False) -> dict:
    """Predicted GPU time and cost (INR) of one job on a machine that costs ``rate`` an hour."""
    duration, clips = max(0.0, float(duration)), max(1, int(clips))
    seconds = duration * LISTEN_PER_SECOND + (duration * WHO_PER_SECOND if multi else 0.0) + clips * RENDER_PER_REEL + (WAKE_SECONDS if wake else 0.0)
    return {"seconds": round(seconds), "rate_per_hour": rate, "est_cost": row_cost(seconds, rate)}


MARKUP = 1.5                                      # the estimate shown to a person is the GPU cost plus 50 %


def estimate_numbers(rates: dict, pod_hourly=None) -> dict:
    """What the browser needs to estimate a job's GPU cost itself (no request): the planning numbers, the hourly price of each kind of GPU and the markup."""
    try:
        markup = max(1.0, float(os.environ.get("CLIPCROW_GPU_MARKUP") or MARKUP))
    except ValueError:
        markup = MARKUP
    home = rates.get("home", rates.get("*", 0.0))
    pod = pod_hourly if pod_hourly is not None else rates.get("rented", rates.get("*", 0.0))
    from ..costs import PRICES
    from ..planner.llm import DEFAULT_MODEL
    model = os.environ.get("CLIPCROW_MODEL") or DEFAULT_MODEL
    pin, pout = PRICES.get(model, PRICES[DEFAULT_MODEL])
    try:
        usd_inr = max(1.0, float(os.environ.get("CLIPCROW_USD_INR") or 88.0))
    except ValueError:
        usd_inr = 88.0
    # planning numbers for the language-model part (fitted to a real 61-minute Marathi job with 10 reels: scoring reads the transcript, then each reel needs a detail call and a text-refine call or two)
    llm = {"usd_in": pin, "usd_out": pout, "usd_inr": usd_inr, "in_fixed": 1000, "in_per_minute": 1200, "in_per_reel": 9500, "out_per_reel": 6400}
    return {"currency": "INR", "markup": markup, "home_rate": home, "pod_rate": pod, "listen": LISTEN_PER_SECOND, "who": WHO_PER_SECOND,
            "render": RENDER_PER_REEL, "wake": WAKE_SECONDS, "llm": llm}
