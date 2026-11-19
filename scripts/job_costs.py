#!/usr/bin/env python3
"""What did finished jobs cost to make? Reads the AI usage (tokens, at the model's price) and the metered GPU time of every finished job from a running server
and prints rupees per job and per minute of video, so the price per minute (`plans.json`, `payg`) can be set from evidence.

    CLIPCROW_URL=https://clipcrow.example CLIPCROW_TOKEN=... python scripts/job_costs.py [--usd-inr 88]

The token is read from the environment (never printed). In accounts mode use an operator's session instead: this script is for the single-token mode or a copy of
the data. Not counted: the server's own CPU, storage and bandwidth, Razorpay's fee (about 2% plus GST on the fee), failed or repeated runs.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import urllib.request


def get(base: str, token: str, path: str):
    req = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {token}", "User-Agent": "clipcrow-job-costs/1"})
    with urllib.request.urlopen(req, timeout=60) as r:          # noqa: S310 (the operator's own server)
        return json.loads(r.read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--usd-inr", type=float, default=88.0)
    a = ap.parse_args()
    base, token = os.environ.get("CLIPCROW_URL", "").rstrip("/"), os.environ.get("CLIPCROW_TOKEN", "")
    if not base or not token:
        raise SystemExit("set CLIPCROW_URL and CLIPCROW_TOKEN")
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
