"""What the AI cost, by kind of call: read from the plans the jobs already saved (``out/edl.json``, ``ai.calls``) and from the answer cache's counters.

Counts and tokens only, never prompts or transcripts. Prices are list prices (``costs.py``), so the totals are estimates."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ..costs import estimate_cost
from ..planner.llm import purpose_models

MAX_JOBS = 500


def _when(text: str):
    try:
        d = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


def summarize(store, days: int = 30) -> dict:
    days = max(1, min(int(days), 365))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows: dict[tuple[str, str], dict] = {}
    cache = {"hits": 0, "input_tokens": 0, "output_tokens": 0, "est_usd": 0.0}
    jobs = 0
    for b in sorted(store.briefs(), key=lambda x: x.created_at, reverse=True)[:MAX_JOBS]:
        t = _when(b.created_at)
        if t is None or t < since:
            continue
        out = store.dir(b.id) / "out"
        try:
            calls = (json.loads((out / "edl.json").read_text(encoding="utf-8")).get("ai") or {}).get("calls") or []
        except (OSError, ValueError, AttributeError):
            calls = []
        try:
            stats = json.loads((out / "llm_cache" / "_stats.json").read_text())
        except (OSError, ValueError):
            stats = {}
        if not calls and not stats:
            continue
        jobs += 1
        for c in calls:
            if not isinstance(c, dict):
                continue
            kind, model = str(c.get("purpose", "")).partition(":")[0][:40], str(c.get("model", ""))[:64]
            r = rows.setdefault((kind, model), {"purpose": kind, "model": model, "calls": 0, "input_tokens": 0, "output_tokens": 0})
            r["calls"] += 1
            r["input_tokens"] += int(c.get("input_tokens") or 0)
            r["output_tokens"] += int(c.get("output_tokens") or 0)
        for kind, v in (stats.items() if isinstance(stats, dict) else []):
            if not isinstance(v, dict) or kind.startswith("_"):
                continue
            cache["hits"] += int(v.get("hits") or 0)
            cache["input_tokens"] += int(v.get("input_tokens") or 0)
            cache["output_tokens"] += int(v.get("output_tokens") or 0)
    # what the cached answers would have cost: priced at the main model (the stats do not keep one per kind), which is the cautious reading when a cheaper model is set
    from ..planner.llm import DEFAULT_MODEL
    import os
    main = os.environ.get("CLIPCROW_MODEL") or DEFAULT_MODEL
    cache["est_usd"] = round(estimate_cost(main, cache["input_tokens"], cache["output_tokens"]) or 0.0, 4)
    table = []
    for r in rows.values():
        cost = estimate_cost(r["model"], r["input_tokens"], r["output_tokens"])
        table.append({**r, "est_usd": None if cost is None else round(cost, 4)})
    table.sort(key=lambda r: (r["est_usd"] is None, -(r["est_usd"] or 0), r["purpose"]))
    known = [r["est_usd"] for r in table if r["est_usd"] is not None]
    total = round(sum(known), 4)
    return {"days": days, "jobs": jobs, "rows": table, "total_usd": total, "unpriced": len(known) < len(table),
            "per_job_usd": round(total / jobs, 4) if jobs else 0.0, "cache": cache,
            "models": {"main": main, **purpose_models_safe()}}


def purpose_models_safe() -> dict:
    try:
        return purpose_models()
    except ValueError:
        return {}
