"""The AI-usage record that ships next to a rendered clip (``<clip>.mp4.ai.json``).

It says what the AI decided, which models ran, and which assets are AI-processed or
AI-generated, and whether a platform AI-content disclosure is recommended. Today the only AI
inputs are editorial decisions and a person matte over real footage, so no disclosure is
recommended; once AI images, AI video or synthetic voice are used, ``disclosure_recommended``
turns on by itself.
"""
from __future__ import annotations

import json
from pathlib import Path

from .edl import EDL, Clip


def provenance(edl: EDL, clip: Clip) -> dict:
    generated = [a for a in edl.assets if a.ai == "generated"]
    processed = [a for a in edl.assets if a.ai == "processed"]
    usage = edl.ai
    return {
        "clip": clip.id,
        "ai_decided": clip.ai.decided if clip.ai else [],
        "ai_assets": {
            "generated": [a.model_dump(exclude_defaults=True) for a in generated],
            "processed": [a.model_dump(exclude_defaults=True) for a in processed],
        },
        "disclosure_recommended": bool(generated),
        "models": usage.models if usage else [],
        "planner_tokens": {"input": usage.input_tokens, "output": usage.output_tokens} if usage else None,
        "planner_est_cost_usd": usage.est_cost_usd if usage else None,
    }


def write_sidecar(edl: EDL, clip: Clip, video: str | Path) -> Path:
    path = Path(str(video) + ".ai.json")
    path.write_text(json.dumps(provenance(edl, clip), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def summary(edl: EDL) -> str:
    u = edl.ai
    lines = []
    if u and u.calls:
        cost = f"~${u.est_cost_usd:.4f}" if u.est_cost_usd is not None else "cost unknown (model not priced)"
        lines.append(f"planner: {len(u.calls)} call(s), {u.input_tokens} in / {u.output_tokens} out tokens, "
                     f"{', '.join(u.models)}; est. {cost}")
    else:
        lines.append("planner: no AI usage recorded")
    gen = [a.id for a in edl.assets if a.ai == "generated"]
    proc = [a.id for a in edl.assets if a.ai == "processed"]
    lines.append(f"AI-generated assets: {', '.join(gen) or 'none'}; AI-processed: {', '.join(proc) or 'none'}")
    lines.append("platform AI disclosure recommended: " + ("yes" if gen else "no"))
    for c in edl.clips:
        if c.ai:
            lines.append(f"  {c.id}: AI decided {', '.join(c.ai.decided)}")
    return "\n".join(lines)
