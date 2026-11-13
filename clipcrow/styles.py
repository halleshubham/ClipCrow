"""A style: how a reference video is paced, measured once and applied to a person's own clips.

A reference is MEASURED, not interpreted: cuts per minute, speech rate and length come from FFmpeg and (when available)
speech recognition. ``derive`` turns those numbers into a small closed set of bounded settings; nothing a person types or a
model writes reaches the EDL except through these fields (see ``StyleSettings``). Timing stays in code: a style only changes
how often a push-in may happen, how many words a caption group holds, and how long clips are.

The mapping from measurements to settings is a heuristic and is documented here so it can be argued with:
  * clip length: the reference's own length (min = 60% of it, max = 125%), kept inside 10..180 s
  * push-in rhythm: from cuts per minute (the visual energy of the edit): fast >= 20, medium >= 8, calm below
  * caption group size: the number of words on screen at once when the reference has captions (read from frames, see
    ``styles_look``); otherwise 4. A speech rate may refine it (``words_per_minute``), but nothing in the service measures one:
    transcribing a reel on a CPU took minutes, and a style must be ready in seconds.
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


class CaptionLookSettings(_M):
    """How the captions look. Filled from a reference's frames (phase 2) or left at ClipCrow's own defaults."""
    from_reference: bool = False                 # false: these are our defaults, not something seen in the reference
    top_px: int = Field(default=1240, ge=0, le=1800)
    size_scale: float = Field(default=1.0, ge=0.6, le=1.6)
    uppercase: bool = False
    color: str = Field(default="#FFFFFF", pattern=r"^#[0-9a-fA-F]{6}$")
    highlight_color: str = Field(default="#FFD54A", pattern=r"^#[0-9a-fA-F]{6}$")
    face: str = Field(default="unknown", pattern=r"^(heavy sans|sans|serif|unknown)$")


class StyleSettings(_M):
    """The only thing a style changes. Every field is bounded; this is what a person may edit."""
    min_clip: int = Field(default=15, ge=10, le=170)
    max_clip: int = Field(default=60, ge=15, le=180)
    caption_max_words: int = Field(default=4, ge=1, le=8)
    caption_pause_break: float = Field(default=0.15, ge=0.05, le=1.0)
    punch_ins: bool = True
    punch: PunchRhythm = Field(default_factory=PunchRhythm)
    captions: CaptionLookSettings = Field(default_factory=CaptionLookSettings)

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
    speech_share: Optional[float] = None          # share of the reference that is not silence, 0..1
    look: Optional[dict] = None                   # what the vision pass saw (styles_look.CaptionLook), or None
    breakdown: str = ""                           # a few lines a person can read
    notes: list[str] = field(default_factory=list)

    def view(self) -> dict:
        return {"duration": round(self.duration, 1), "size": f"{self.width}x{self.height}", "cuts": self.cuts,
                "cuts_per_minute": round(self.cuts_per_minute, 1), "median_shot_seconds": round(self.median_shot_seconds, 2),
                "words_per_minute": None if self.words_per_minute is None else round(self.words_per_minute),
                "loudness_lufs": None if self.loudness_lufs is None else round(self.loudness_lufs, 1),
                "speech_share": None if self.speech_share is None else round(self.speech_share, 2),
                "look": self.look, "breakdown": self.breakdown, "notes": self.notes}


MIN_CAPTION_FRAMES = 3          # of the ~6 frames shown: captions seen in fewer than this are not treated as the reference's style
MIN_TEXT_LUMINANCE = 0.175      # ClipCrow draws captions over the picture with a dark shadow: text needs 4.5:1 against black, (L + .05) / .05


def luminance(hex_color: str) -> float:
    """WCAG relative luminance of ``#RRGGBB``."""
    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def look_in_use(look: Optional[dict]) -> bool:
    """Captions count as the reference's style only when they were seen in enough frames (one frame may be a title card)."""
    if not look or not look.get("present"):
        return False
    n = look.get("frames_with_captions")
    return n is None or n >= MIN_CAPTION_FRAMES


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
    caps = CaptionLookSettings()
    look = m.look or {}
    if look_in_use(look):
        from .styles_look import BANDS, size_scale
        color, hot = look.get("color") or "#FFFFFF", look.get("highlight_color") or "#FFD54A"
        if luminance(color) < MIN_TEXT_LUMINANCE:          # dark text on a coloured box: we have no box, so keep readable text
            color = "#FFFFFF"
        if luminance(hot) < MIN_TEXT_LUMINANCE:
            hot = "#FFD54A"
        caps = CaptionLookSettings(
            from_reference=True, top_px=BANDS.get(look.get("band") or "lower", BANDS["lower"]),
            size_scale=size_scale(look.get("size")), uppercase=bool(look.get("uppercase")),
            color=color, highlight_color=hot, face=look.get("face") or "unknown")
        if look.get("words_per_line"):
            words = max(1, min(8, int(look["words_per_line"])))      # what is on screen beats a guess from speech rate
    return StyleSettings(min_clip=lo, max_clip=hi, caption_max_words=words, caption_pause_break=0.15,
                         punch_ins=on, punch=punch, captions=caps).check()


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


def speech_share(path, duration: float) -> Optional[float]:
    """How much of the reference has sound above the noise floor: 1.0 is wall-to-wall talking, lower means pauses and gaps."""
    r = _run(["ffmpeg", "-v", "info", "-i", str(path), "-vn", "-af", "silencedetect=noise=-35dB:d=0.35", "-f", "null", "-"], 300)
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    if len(starts) > len(ends):                      # silence running to the end of the file
        ends.append(duration)
    quiet = sum(max(0.0, min(e, duration) - max(s_, 0.0)) for s_, e in zip(starts, ends))
    return max(0.0, min(1.0, 1.0 - quiet / duration)) if duration > 0 else None


def measure(path, *, count_words: Optional[Callable[[str], Optional[int]]] = None,
            read_look: Optional[Callable[[str, float], Optional[dict]]] = None) -> Measurements:
    """Measure a reference. ``count_words(path)`` returns the number of spoken words, or None when it cannot.
    ``read_look(path, duration)`` returns what the captions look like (a ``styles_look.CaptionLook`` dict), or None."""
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
        m.speech_share = speech_share(path, dur)
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
    if read_look is not None:
        try:
            m.look = read_look(str(path), dur)
        except Exception:                         # the look is a bonus: pacing must survive a model that will not answer
            m.notes.append("The caption look could not be read, so ClipCrow's own caption style is used.")
        look = m.look
        if look is not None and not look.get("present"):
            m.notes.append("No captions were visible in the frames checked, so ClipCrow's own caption style is used.")
        elif look is not None and not look_in_use(look):
            m.notes.append(f"Captions showed in only {look.get('frames_with_captions')} of the frames checked, so they were not "
                           "treated as this reference's style and ClipCrow's own caption style is used.")
        elif look is not None and luminance(look.get("color") or "#FFFFFF") < MIN_TEXT_LUMINANCE:
            m.notes.append("This reference draws dark caption text on a coloured box. ClipCrow has no caption box yet, so the "
                           "captions stay light and readable; position, size and capitals are still taken from the reference.")
    m.breakdown = describe(m)
    return m


def describe(m: Measurements) -> str:
    """A few plain lines about the reference, for a person to read. Plain text: the UI inserts it as text."""
    lines = [f"{m.duration:.0f} seconds, {m.width}x{m.height}.",
             f"{m.cuts} cuts, about {m.cuts_per_minute:.0f} a minute; a typical shot lasts {m.median_shot_seconds:.1f} s."]
    lines.append("Cut hard and often." if m.cuts_per_minute >= 20 else
                 "Cut at a steady pace." if m.cuts_per_minute >= 8 else "Cut rarely: long, calm shots.")
    if m.words_per_minute:
        lines.append(f"The speaker says about {m.words_per_minute:.0f} words a minute"
                     f"{' (fast)' if m.words_per_minute >= 180 else ' (measured)' if m.words_per_minute >= 130 else ' (slow)'}.")
    if m.speech_share is not None:
        lines.append(f"Sound for about {m.speech_share * 100:.0f}% of the time"
                     f"{', few pauses' if m.speech_share >= 0.9 else ', with clear pauses' if m.speech_share < 0.75 else ''}.")
    if m.loudness_lufs is not None:
        lines.append(f"Loudness {m.loudness_lufs:.1f} LUFS.")
    look = m.look if look_in_use(m.look) else {}
    if look.get("present"):
        where = {"upper": "near the top", "middle": "in the middle", "lower": "low in the frame"}.get(look.get("band"), "")
        bits = [f"Captions sit {where}".strip()]
        if look.get("words_per_line"):
            bits.append(f"about {look['words_per_line']} words at a time")
        if look.get("size"):
            bits.append(f"a line is about {look['size']:.0f}% of the frame height")
        if look.get("uppercase"):
            bits.append("written in capitals")
        if look.get("face") and look["face"] != "unknown":
            bits.append(f"the lettering looks {look['face']}")
        lines.append(", ".join(bits) + ".")
        if look.get("note"):
            lines.append(look["note"])
    return "\n".join(lines)
