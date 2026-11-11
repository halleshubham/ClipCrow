"""Turn the model's raw answers into clips that are valid by construction.

The model only chooses sentence ids and short strings. Everything that decides where the
video is cut is deterministic: sentence boundaries, length bounds, padding, overlap removal.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from ..fonts import DEVANAGARI
from .models import Mark, RawClip
from .transcript import Sentence, Transcript
from .windows import Window

PAD_IN = 0.15
PAD_OUT = 0.30
DEVANAGARI_LANGS = {"hi", "mr", "ne", "sa"}
_PUNCT = re.compile(r"[\s\.,!?;:\"'“”‘’()\[\]{}…।॥\-–—]+")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def clean_text(text: str, limit: int) -> str:
    """Single line, no control characters, bounded length (it ends up on screen)."""
    t = unicodedata.normalize("NFC", _CTRL.sub(" ", text or "")).strip()
    t = re.sub(r"\s+", " ", t)
    return t[:limit].rstrip()


def norm_word(w: str) -> str:
    return unicodedata.normalize("NFC", _PUNCT.sub("", w or "")).casefold()


def wrong_script(text: str, language: str) -> bool:
    """True when the copy should be Devanagari but contains none (e.g. romanised Marathi)."""
    if language not in DEVANAGARI_LANGS:
        return False
    letters = [c for c in text if c.isalpha()]
    return len(letters) >= 3 and not DEVANAGARI.search(text)


@dataclass
class PlannedClip:
    first: int
    last: int
    start: float
    end: float
    title: str
    hook: str
    why: str
    score: float
    emphasis: list[int] = field(default_factory=list)   # global word indices
    behind: Optional[int] = None                          # global word index
    stat: Optional[tuple[int, str, bool]] = None          # (global word index, text, anchored to the spoken word)
    notes: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.end - self.start


def fit_bounds(first: int, last: int, win: Window, sentences: list[Sentence],
               min_s: float, max_s: float) -> Optional[tuple[int, int]]:
    first, last = max(first, win.first), min(last, win.last)
    if last < first:
        first, last = last, first
    if first < win.first or last > win.last:
        return None
    dur = lambda a, b: sentences[b].end - sentences[a].start  # noqa: E731
    while dur(first, last) > max_s and last > first:
        last -= 1
    if dur(first, last) > max_s:
        return None
    while dur(first, last) < min_s and last < win.last and dur(first, last + 1) <= max_s:
        last += 1
    while dur(first, last) < min_s and first > win.first and dur(first - 1, last) <= max_s:
        first -= 1
    return (first, last) if dur(first, last) >= min_s else None


def pad_times(first: int, last: int, sentences: list[Sentence], total: float) -> tuple[float, float]:
    s, e = sentences[first], sentences[last]
    start = s.start - PAD_IN
    if first > 0:                                   # never reach into the previous sentence
        start = max(start, (sentences[first - 1].end + s.start) / 2)
    end = e.end + PAD_OUT
    if last + 1 < len(sentences):
        end = min(end, (e.end + sentences[last + 1].start) / 2)
    return max(0.0, start), min(total, end)


def find_word(mark: Mark, sentences: list[Sentence], tr: Transcript, lo: int, hi: int) -> Optional[int]:
    """Global word index of mark.word inside its sentence (must be within sentences lo..hi)."""
    if not lo <= mark.sentence <= hi:
        return None
    s = sentences[mark.sentence]
    want = norm_word(mark.word)
    if not want:
        return None
    for i in range(s.first, s.last + 1):
        if norm_word(tr.words[i].w) == want:
            return i
    return None


def finalize(raw: RawClip, win: Window, tr: Transcript, *, min_s: float, max_s: float,
             window_score: float, language: str, use_behind: bool) -> tuple[Optional[PlannedClip], str]:
    """(clip, reason): clip is None when it had to be dropped, and reason says why."""
    sent = tr.sentences
    bounds = fit_bounds(raw.first_sentence, raw.last_sentence, win, sent, min_s, max_s)
    if bounds is None:
        return None, f"sentences {raw.first_sentence}-{raw.last_sentence} cannot fit {min_s:.0f}-{max_s:.0f}s in the window"
    first, last = bounds
    start, end = pad_times(first, last, sent, tr.duration)
    virality = max(0, min(10, raw.virality)) / 10
    clip = PlannedClip(first, last, start, end,
                       title=clean_text(raw.title, 90), hook=clean_text(raw.hook_text, 100),
                       why=clean_text(raw.why, 160), score=window_score * 0.7 + virality * 0.3)
    for m in raw.emphasis[:3]:
        i = find_word(m, sent, tr, first, last)
        if i is None:
            clip.notes.append(f"emphasis word {m.word!r} not found in its sentence")
        elif i not in clip.emphasis:
            clip.emphasis.append(i)
    if raw.behind is not None and use_behind:
        i = find_word(raw.behind, sent, tr, first, last)
        if i is None:
            clip.notes.append(f"behind word {raw.behind.word!r} not found in its sentence")
        elif tr.words[i].s - start < 1.0 or tr.words[i].s - start > 0.4 * (end - start):
            clip.notes.append("behind word is outside the 1 s - 40% window of the clip")
        else:
            clip.behind = i
    if raw.stat is not None and first <= raw.stat.sentence <= last:
        txt = clean_text(raw.stat.text, 24)
        if txt:
            i = find_word(Mark(sentence=raw.stat.sentence, word=raw.stat.word), sent, tr, first, last)
            if i is None:        # the model's word is not in the sentence: fall back to the sentence start
                clip.notes.append(f"stat word {raw.stat.word!r} not found; anchored to its sentence")
            clip.stat = (i if i is not None else sent[raw.stat.sentence].first, txt, i is not None)
    return clip, ""


def drop_overlaps(clips: list[PlannedClip], limit: int) -> list[PlannedClip]:
    """Highest score first; a clip that overlaps an accepted one by more than 0.5 s is dropped."""
    kept: list[PlannedClip] = []
    for c in sorted(clips, key=lambda c: c.score, reverse=True):
        if len(kept) >= limit:
            break
        if all(min(c.end, k.end) - max(c.start, k.start) <= 0.5 for k in kept):
            kept.append(c)
    return sorted(kept, key=lambda c: c.start)
