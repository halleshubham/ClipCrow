#!/usr/bin/env python3
"""What did finished jobs cost to make? Reads the AI usage (tokens, at the model's price) and the metered GPU time of every finished job from a running server
and prints rupees per job and per minute of video, so the price per minute (`plans.json`, `payg`) can be set from evidence.

    CLIPCROW_URL=https://clipcrow.example CLIPCROW_SESSION=<your clipcrow_session cookie> python scripts/job_costs.py [--usd-inr 88] [--margin 100] [--price 8] [--min-minutes 3]

The token is read from the environment (never printed). In accounts mode use an operator's session instead: this script is for the single-token mode or a copy of
the data. Not counted: the server's own CPU, storage and bandwidth, Razorpay's fee (about 2% plus GST on the fee), failed or repeated runs.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import urllib.request


def get(base: str, session: str, path: str):
    req = urllib.request.Request(base + path, headers={"Cookie": f"clipcrow_session={session}", "User-Agent": "clipcrow-job-costs/1"})
    with urllib.request.urlopen(req, timeout=60) as r:          # noqa: S310 (the operator's own server)
        return json.loads(r.read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--usd-inr", type=float, default=88.0)
    ap.add_argument("--margin", type=float, default=None, help="target markup on cost, in percent (100 means price = twice the cost); default CLIPCROW_MARGIN_PERCENT or 50")
    ap.add_argument("--price", type=float, default=8.0, help="the price per minute now, in rupees")
    ap.add_argument("--min-minutes", type=int, default=3, help="the minimum charge now, in minutes")
    a = ap.parse_args()
    base, token = os.environ.get("CLIPCROW_URL", "").rstrip("/"), os.environ.get("CLIPCROW_SESSION", "")
    if not base or not token:
        raise SystemExit("set CLIPCROW_URL and CLIPCROW_SESSION (the value of your clipcrow_session cookie: browser dev tools, Application, Cookies)")
    jobs = get(base, token, "/api/jobs")
    rows = []
    for j in jobs:
        if j.get("status") != "done":
            continue
        try:
            u = get(base, token, f"/api/jobs/{j['id']}/usage")
        except OSError:
            continue
        minutes = ((j.get("source") or {}).get("duration") or 0) / 60
        gpu = sum((t.get("est_cost") or 0) for t in ((u.get("gpu") or {}).get("tasks") or []))
        rows.append({"minutes": minutes, "clips": len(j.get("clips") or []), "clean": bool((j.get("params") or {}).get("clean")),
                     "ai": (u.get("est_cost_usd") or 0) * a.usd_inr, "gpu": gpu})
    print(f"{'minutes':>8} {'clips':>5} {'AI Rs':>8} {'GPU Rs':>8} {'Rs/min':>8}  kind")
    for r in sorted(rows, key=lambda r: (r["clean"], r["minutes"])):
        per = (r["ai"] + r["gpu"]) / r["minutes"] if r["minutes"] else 0
        print(f"{r['minutes']:8.1f} {r['clips']:5d} {r['ai']:8.1f} {r['gpu']:8.1f} {per:8.2f}  {'raw take' if r['clean'] else 'long form'}")
    for name, group in (("long form", [r for r in rows if not r["clean"] and r["minutes"] >= 15]), ("raw takes", [r for r in rows if r["clean"]])):
        if group:
            per = [(r["ai"] + r["gpu"]) / r["minutes"] for r in group if r["minutes"]]
            print(f"{name}: {len(group)} jobs, median Rs {statistics.median(per):.2f} a minute, range Rs {min(per):.2f} to Rs {max(per):.2f}")
    markup = a.margin if a.margin is not None else float(os.environ.get("CLIPCROW_MARGIN_PERCENT") or 50)
    long_ = sorted((r["ai"] + r["gpu"]) / r["minutes"] for r in rows if not r["clean"] and r["minutes"] >= 15)
    shorts = [r["ai"] + r["gpu"] for r in rows if r["minutes"] < 5]
    print(f"\nTarget markup {markup:g}% (price = cost x {1 + markup / 100:g}); price now Rs {a.price:g} a minute, minimum {a.min_minutes} minutes")
    if long_:
        p75 = long_[min(len(long_) - 1, -(-3 * len(long_) // 4) - 1)]
        need = -(-(p75 * (1 + markup / 100) * 2) // 1) / 2                      # rounded up to half a rupee
        print(f"  long recordings: the 75th percentile cost is Rs {p75:.2f} a minute, so the price needs to be about Rs {need:g} a minute"
              + (" (the current price is enough)" if a.price >= need else " (the current price is BELOW it)"))
    if shorts:
        med = statistics.median(shorts)
        least = max(1, -(-(med * (1 + markup / 100)) // a.price))
        print(f"  short takes: the median cost is Rs {med:.2f} a job, so the minimum charge needs to be about {least:g} minutes"
              + (" (the current minimum is enough)" if a.min_minutes >= least else " (the current minimum is BELOW it)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
