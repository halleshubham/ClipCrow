"""A style: how a reference video is paced, measured once and applied to a person's own clips.

A reference is MEASURED, not interpreted: cuts per minute, speech rate and length come from FFmpeg and (when available)
speech recognition. ``derive`` turns those numbers into a small closed set of bounded settings; nothing a person types or a
model writes reaches the EDL except through these fields (see ``StyleSettings``). Timing stays in code: a style only changes
how often a push-in may happen, how many words a caption group holds, and how long clips are.

The mapping from measurements to settings is a heuristic and is documented here so it can be argued with:
  * clip length: the reference's own length (min = 60% of it, max = 125%), kept inside 10..180 s
  * push-in rhythm: from cuts per minute (the visual energy of the edit): fast >= 20, medium >= 8, calm below
  * caption group size: from speech rate (words per minute): fast speech gets longer groups
Phase 2 reads the caption look itself from frames; this module never does.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Optional

from pydantic import BaseModel, ConfigDict, Field

MAX_REFERENCE_SECONDS = 180.0
MIN_REFERENCE_SECONDS = 5.0
MAX_REFERENCE_BYTES = 200 * 1024 * 1024
SCENE_THRESHOLD = 0.3


class StyleError(ValueError):
    """A reference or a setting that cannot be used; the message is safe to show a person."""


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PunchRhythm(_M):
    """How often the picture may push in. Defaults equal ``punch``'s own constants."""
    min_gap: float = Field(default=1.0, ge=0.5, le=8.0)
    max_per_clip: int = Field(default=6, ge=0, le=12)


class StyleSettings(_M):
    """The only thing a style changes. Every field is bounded; this is what a person may edit."""
    min_clip: int = Field(default=15, ge=10, le=170)
    max_clip: int = Field(default=60, ge=15, le=180)
    caption_max_words: int = Field(default=4, ge=1, le=8)
    caption_pause_break: float = Field(default=0.15, ge=0.05, le=1.0)
    punch_ins: bool = True
    punch: PunchRhythm = Field(default_factory=PunchRhythm)

    def check(self) -> "StyleSettings":
        if self.min_clip >= self.max_clip:
            raise StyleError("The shortest clip must be shorter than the longest.")
        return self


@dataclass
class Measurements:
    duration: float
    width: int = 0
    height: int = 0
    cuts: int = 0
    cuts_per_minute: float = 0.0
    median_shot_seconds: float = 0.0
    words_per_minute: Optional[float] = None      # None: speech was not measured (no recognition, or no speech)
    loudness_lufs: Optional[float] = None
    notes: list[str] = field(default_factory=list)

    def view(self) -> dict:
        return {"duration": round(self.duration, 1), "size": f"{self.width}x{self.height}", "cuts": self.cuts,
                "cuts_per_minute": round(self.cuts_per_minute, 1), "median_shot_seconds": round(self.median_shot_seconds, 2),
                "words_per_minute": None if self.words_per_minute is None else round(self.words_per_minute),
                "loudness_lufs": None if self.loudness_lufs is None else round(self.loudness_lufs, 1), "notes": self.notes}


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def derive(m: Measurements) -> StyleSettings:
    """Pure: the same measurements always give the same settings."""
    lo = int(_clamp(round(m.duration * 0.6), 10, 170))
    hi = int(_clamp(round(m.duration * 1.25), lo + 5, 180))
    if m.cuts_per_minute >= 20:
        punch, on = PunchRhythm(min_gap=1.0, max_per_clip=6), True
    elif m.cuts_per_minute >= 8:
        punch, on = PunchRhythm(min_gap=2.0, max_per_clip=4), True
    else:
        punch, on = PunchRhythm(min_gap=3.5, max_per_clip=2), m.cuts_per_minute >= 2
    wpm = m.words_per_minute
    words = 4 if wpm is None else (5 if wpm >= 180 else 4 if wpm >= 130 else 3)
    return StyleSettings(min_clip=lo, max_clip=hi, caption_max_words=words, caption_pause_break=0.15,
                         punch_ins=on, punch=punch).check()


# --- measuring (FFmpeg) ---------------------------------------------------------------------------------------------

def _run(cmd: list[str], timeout: int = 240) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def probe(path) -> dict:
    r = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], 60)
    if r.returncode != 0:
        raise StyleError("That file could not be read as a video.")
    import json
    try:
        d = json.loads(r.stdout)
        v = next(s for s in d["streams"] if s.get("codec_type") == "video")
        duration = float(d["format"]["duration"])
    except (KeyError, StopIteration, ValueError, TypeError):
        raise StyleError("That file could not be read as a video.")
    return {"duration": duration, "width": int(v.get("width") or 0), "height": int(v.get("height") or 0),
            "has_audio": any(s.get("codec_type") == "audio" for s in d["streams"])}


def detect_cuts(path, duration: float) -> list[float]:
    r = _run(["ffmpeg", "-v", "info", "-i", str(path), "-an", "-vf",
              f"scale=320:-2,select='gt(scene,{SCENE_THRESHOLD})',showinfo", "-f", "null", "-"], 600)
    return sorted(t for t in (float(x) for x in re.findall(r"pts_time:([\d.]+)", r.stderr)) if 0.3 < t < duration - 0.3)


def loudness(path) -> Optional[float]:
    r = _run(["ffmpeg", "-v", "info", "-i", str(path), "-vn", "-af", "ebur128=peak=none", "-f", "null", "-"], 300)
    found = re.findall(r"\n\s*I:\s*(-?[\d.]+) LUFS", r.stderr)
    return float(found[-1]) if found else None


def measure(path, *, count_words: Optional[Callable[[str], Optional[int]]] = None) -> Measurements:
    """Measure a reference. ``count_words(path)`` returns the number of spoken words, or None when it cannot."""
    info = probe(path)
    dur = info["duration"]
    if dur < MIN_REFERENCE_SECONDS:
        raise StyleError(f"The reference must be at least {MIN_REFERENCE_SECONDS:.0f} seconds long.")
    if dur > MAX_REFERENCE_SECONDS:
        raise StyleError(f"The reference can be at most {MAX_REFERENCE_SECONDS / 60:.0f} minutes long.")
    cuts = detect_cuts(path, dur)
    edges = [0.0, *cuts, dur]
    shots = sorted(b - a for a, b in zip(edges, edges[1:]))
    m = Measurements(duration=dur, width=info["width"], height=info["height"], cuts=len(cuts),
                     cuts_per_minute=len(cuts) / (dur / 60.0), median_shot_seconds=shots[len(shots) // 2])
    if info["has_audio"]:
        m.loudness_lufs = loudness(path)
        n = None
        if count_words is not None:
            try:
                n = count_words(str(path))
            except Exception:                     # recognition is optional: a failure must not lose the pacing numbers
                m.notes.append("Speech could not be measured, so caption size uses the default.")
        if n is not None and n > 0:
            m.words_per_minute = n / (dur / 60.0)
    else:
        m.notes.append("The reference has no sound, so only the picture was measured.")
    return m
