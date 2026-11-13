"""Read a reference video's CAPTION LOOK from a handful of frames, with one Claude vision call.

The model only describes what it sees, in a closed schema of bounded fields (``CaptionLook``); nothing it writes reaches
the HTML. Colours are hex, checked against a pattern; a band is one of three words; the size is a number that is clamped.
A font is never taken from the model: it names a look ("heavy sans", "serif") and ``family_for`` maps that to one of the
families ClipCrow ships, so Devanagari copy keeps its own font (CLAUDE.md: Marathi/Hindi stays Devanagari).

The frames are sampled evenly from the middle of the reference, where captions are most likely to be on screen; each is
scaled down, so one call carries a few hundred KB, not a video.
"""
from __future__ import annotations

import base64
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from .fonts import DEFAULT_DEVANAGARI

FRAMES = 6
FRAME_WIDTH = 480
_HEX = re.compile(r"^#[0-9a-fA-F]{6}\Z")

SYSTEM = """You are shown frames from a short social video, in order. Describe ONLY the burnt-in captions
(the subtitle words that appear over the picture as the person speaks). Ignore titles, logos, watermarks,
stickers, comments and any text that is part of the filmed scene.

Report what you actually see. If there are no captions in these frames, say so with present=false and leave
everything else at its default. Never guess a colour you cannot see: when unsure, leave the field out."""

USER = """Describe the captions in these frames.

- band: where the caption sits vertically: "upper", "middle" or "lower".
- size: how tall one line of caption text is, as a percentage of the frame height (roughly 3 to 12).
- words_per_line: how many words are on screen at once, typically.
- color: the main caption colour, as #RRGGBB.
- highlight_color: the colour of the emphasised or currently-spoken word, if one word is coloured differently.
- uppercase: true only if the Latin caption text is written in CAPITAL LETTERS.
- frames_with_captions: in how many of the frames you were shown do you see captions at all (0 if none).
- face: the look of the lettering: "heavy sans", "sans", "serif" or "unknown"."""


class CaptionLook(BaseModel):
    """What the model is allowed to say. Every field is bounded; unknown fields are refused."""
    model_config = ConfigDict(extra="forbid")
    present: bool = False
    frames_with_captions: Optional[int] = Field(default=None, ge=0, le=12)
    band: str = Field(default="lower", pattern=r"^(upper|middle|lower)$")
    size: Optional[float] = Field(default=None, ge=1.0, le=20.0)
    words_per_line: Optional[int] = Field(default=None, ge=1, le=12)
    color: Optional[str] = None
    highlight_color: Optional[str] = None
    uppercase: bool = False
    face: str = Field(default="unknown", pattern=r"^(heavy sans|sans|serif|unknown)$")
    note: str = Field(default="", max_length=200)

    def clean(self) -> "CaptionLook":
        """Drop anything that is not a colour: a model may still write a word where hex was asked for."""
        return self.model_copy(update={
            "color": self.color if self.color and _HEX.match(self.color) else None,
            "highlight_color": self.highlight_color if self.highlight_color and _HEX.match(self.highlight_color) else None,
            "note": " ".join(self.note.split())[:200],
        })


# A caption band's top edge in a 1920-tall frame. "lower" is the ClipCrow default, clear of the face.
BANDS = {"upper": 300, "middle": 820, "lower": 1240}
# What a described face means for us. Devanagari copy overrides this later (fonts.effective_family).
FACES = {"heavy sans": ("Noto Sans", 700), "sans": ("Noto Sans", 400), "serif": ("Noto Serif", 700),
         "unknown": (DEFAULT_DEVANAGARI, 700)}


def family_for(face: str, devanagari: bool) -> tuple[str, int]:
    family, weight = FACES.get(face, FACES["unknown"])
    if devanagari:
        family = "Noto Sans Devanagari" if family.startswith("Noto Sans") else DEFAULT_DEVANAGARI
    return family, weight


def size_scale(size_pct: Optional[float]) -> float:
    """A caption line's height as a share of the frame, turned into a multiplier of our fitted size.

    Our own captions sit at roughly 5% of the frame height, so that is 1.0. Clamped to CaptionStyle's own range."""
    if not size_pct:
        return 1.0
    return round(max(0.6, min(1.6, size_pct / 5.0)), 2)


def grab_frames(video, duration: float, *, n: int = FRAMES, width: int = FRAME_WIDTH) -> list[bytes]:
    """``n`` JPEG frames spread over the middle 80% of the video, smallest first in time order."""
    out: list[bytes] = []
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(n):
            t = duration * (0.1 + 0.8 * (i + 0.5) / n)
            dest = Path(tmp) / f"f{i}.jpg"
            r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
                                "-vf", f"scale={width}:-2", "-q:v", "4", "-y", str(dest)],
                               capture_output=True, text=True, timeout=120)
            if r.returncode == 0 and dest.is_file():
                out.append(dest.read_bytes())
    return out


def read_look(video, duration: float, llm, *, frames: Optional[list[bytes]] = None) -> CaptionLook:
    """One vision call. Raises ``planner.llm.LLMError`` (through ``llm.generate``) when the model cannot answer."""
    shots = grab_frames(video, duration) if frames is None else frames
    if not shots:
        raise ValueError("no frames could be read from the reference")
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                            "data": base64.b64encode(f).decode()}} for f in shots]
    content.append({"type": "text", "text": USER})
    return llm.generate(SYSTEM, content, CaptionLook, purpose="style:captions").clean()
