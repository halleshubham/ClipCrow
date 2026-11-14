"""Transitions on hard cuts: a closed set of short looks (flash, blur, dip to black, whip) laid over the cut.

WHICH cuts get one is code, from the clip itself: the places where the cleanup joined two stretches of the recording (``Clip.seams``), a speaker change in a
tracked framing (a jump between two reframe keyframes, never a pan) and both edges of a slide shown whole. The model never picks a cut or a look. Cuts closer
than ``MIN_SPACING`` share one effect, and none is put on the first or last ``EDGE`` seconds (the cover and the end).
"""
from __future__ import annotations

from .edl import Clip

STYLES = ("flash", "blur", "dip", "whip")
MIN_SPACING = 0.6
EDGE = 0.3
MAX_CUTS = 60
JUMP = 0.1            # a keyframe jump of more than this fraction of the source width ...
JUMP_WINDOW = 0.12    # ... within this many seconds is a hard cut


def hard_cuts(clip: Clip) -> list[float]:
    cuts = list(clip.seams)
    lay = clip.layout
    if lay.framing == "track":
        for a, b in zip(lay.reframe, lay.reframe[1:]):
            if b.t - a.t <= JUMP_WINDOW and abs(b.cx - a.cx) > JUMP:
                cuts.append(b.t)
        for sp in lay.fit_spans:
            cuts += [sp.t0, sp.t1]
    out: list[float] = []
    for c in sorted(c for c in cuts if EDGE <= c <= clip.duration - EDGE):
        if not out or c - out[-1] >= MIN_SPACING:
            out.append(round(c, 3))
    return out[:MAX_CUTS]


def seams_from_cleanup(path) -> list[float]:
    """Where the cleanup joined two kept stretches, in seconds of the CLEANED video (empty when no cleanup was applied)."""
    import json
    from pathlib import Path
    try:
        rep = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if rep.get("applied", True) is False:
        return []
    out = []
    for m in (rep.get("timemap") or [])[1:]:
        try:
            out.append(float(m["out"][0]))
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    return sorted(out)


def with_seams(edl, seams: list[float]):
    """The EDL with each clip's ``seams`` set from the cleaned video's join times (clip time = video time - source_in)."""
    if not seams:
        return edl
    clips = []
    for c in edl.clips:
        mine = [round(s - c.source_in, 3) for s in seams if c.source_in + 0.05 < s < c.source_out - 0.05]
        clips.append(c.model_copy(update={"seams": mine}) if mine else c)
    return edl.model_copy(update={"clips": clips})
