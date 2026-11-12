"""Where the picture pushes in. Timing is code, never the model: a punch-in starts at a sentence start, so the
push lands with a new thought, and the camera snaps back as the sentence ends."""
from __future__ import annotations

import re

HOOK_CLEAR = 2.6          # seconds at the start left alone (the hook banner is on screen)
TAIL_CLEAR = 2.0          # and at the end
MIN_GAP = 1.0             # picture at rest between two punch-ins
MIN_LEN, MAX_LEN = 1.5, 4.0
MAX_PER_CLIP = 6
SCALES = (1.12, 1.18)     # alternate, so it does not feel mechanical
END_MARK = re.compile(r"[.?!।॥…]$")


def plan_punch_ins(spans: list[tuple[float, float]], duration: float) -> list[dict]:
    """``spans``: (start, end) of each sentence in clip time. Returns PunchIn dicts."""
    out: list[dict] = []
    busy_until = HOOK_CLEAR - MIN_GAP
    for start, end in spans:
        if len(out) >= MAX_PER_CLIP:
            break
        t = max(start, 0.0)
        if t < busy_until + MIN_GAP or t > duration - TAIL_CLEAR:
            continue
        length = min(MAX_LEN, end - t, duration - t - 0.3)
        if length < MIN_LEN:
            continue
        out.append({"t": round(t, 2), "duration": round(length, 2), "scale": SCALES[len(out) % 2]})
        busy_until = t + length
    return out


def sentence_spans(words, source_in: float, source_out: float, *, gap: float = 0.8) -> list[tuple[float, float]]:
    """Sentence spans (clip time) from edl-style words (``w``, ``s``, ``e``): a sentence ends at terminal
    punctuation or a pause longer than ``gap`` seconds."""
    inside = [w for w in words if w.e > source_in and w.s < source_out]
    spans: list[tuple[float, float]] = []
    start = None
    for i, w in enumerate(inside):
        if start is None:
            start = w.s
        nxt = inside[i + 1] if i + 1 < len(inside) else None
        if END_MARK.search(w.w.strip()) or nxt is None or nxt.s - w.e > gap:
            spans.append((max(start, source_in) - source_in, min(w.e, source_out) - source_in))
            start = None
    return spans
