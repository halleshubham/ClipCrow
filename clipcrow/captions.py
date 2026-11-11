"""Group transcript words into on-screen caption groups.

Rules (from short-form practice and the HyperFrames captions guide):
  * 1-8 words per group (default 4); break at a sentence end or a pause
  * one group on screen at a time: a group never overlaps the next
  * a caption at exactly t=0 looks pre-existing, so the first group opens >= 0.1 s in
Devanagari sentence ends (danda U+0964, double danda U+0965) count like '.'.
"""
from __future__ import annotations

from dataclasses import dataclass

SENTENCE_END = (".", "?", "!", "।", "॥", "…")
MIN_GROUP_SECONDS = 0.25
TAIL = 0.15  # keep a group a moment after its last word


@dataclass
class CaptionWord:
    text: str
    start: float          # clip-relative seconds
    end: float
    index: int            # position in the EDL transcript (for emphasis lookup)


@dataclass
class CaptionGroup:
    words: list[CaptionWord]
    start: float
    end: float


def clip_words(transcript, source_in: float, source_out: float) -> list[CaptionWord]:
    """Words that fall inside the clip window, in clip-relative time."""
    out = []
    for i, w in enumerate(transcript):
        if w.e <= source_in or w.s >= source_out:
            continue
        s = max(w.s, source_in) - source_in
        e = min(w.e, source_out) - source_in
        text = w.w.strip()
        if text:
            out.append(CaptionWord(text, s, max(e, s + 0.05), i))
    return out


def group_words(words: list[CaptionWord], clip_duration: float, *,
                max_words: int = 4, pause_break: float = 0.15) -> list[CaptionGroup]:
    groups: list[list[CaptionWord]] = []
    cur: list[CaptionWord] = []
    for w in words:
        if cur and (len(cur) >= max_words or w.start - cur[-1].end >= pause_break
                    or cur[-1].text.endswith(SENTENCE_END)):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)

    out: list[CaptionGroup] = []
    for n, g in enumerate(groups):
        start = max(g[0].start, 0.1)
        nxt = groups[n + 1][0].start if n + 1 < len(groups) else clip_duration
        end = min(g[-1].end + TAIL, nxt, clip_duration)
        if end - start < MIN_GROUP_SECONDS:
            end = min(start + MIN_GROUP_SECONDS, clip_duration)
        if end <= start:           # no room left at the very end of the clip
            continue
        out.append(CaptionGroup(g, start, end))
    return out
