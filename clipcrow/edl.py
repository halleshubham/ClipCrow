"""The Edit Decision List: the contract between the planner (LLM or human) and
the renderer. The planner never touches pixels; it emits this, the compiler turns
it into a HyperFrames project, the renderer produces the video.

Time conventions:
  * transcript words are in SOURCE time (seconds from the start of the source file)
  * a clip selects [source_in, source_out) of the source
  * overlay times are CLIP-relative (0 = first frame of the clip), so they survive
    re-cutting; an overlay may instead anchor to a transcript word
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EDLError(ValueError):
    """The EDL is well-formed JSON but cannot be rendered."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# Values that end up inside generated CSS/HTML are restricted to safe shapes: the
# EDL may come from an LLM or a user, so nothing in it can break out of a style
# attribute or an id.
HexColor = Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")]
SafeId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")]
FontFamily = Annotated[str, Field(pattern=r"^[A-Za-z0-9 _-]{1,60}$")]


class CallUsage(_Model):
    """One LLM call, as billed."""
    purpose: str
    model: str
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class AIUsage(_Model):
    """What the planner spent. ``est_cost_usd`` is an estimate from list prices, not an invoice."""
    provider: str = "anthropic"
    calls: list[CallUsage] = Field(default_factory=list)
    est_cost_usd: Optional[float] = None

    @property
    def input_tokens(self) -> int:
        return sum(c.input_tokens for c in self.calls)

    @property
    def output_tokens(self) -> int:
        return sum(c.output_tokens for c in self.calls)

    @property
    def models(self) -> list[str]:
        return sorted({c.model for c in self.calls})


AssetOrigin = Literal["upload", "stock", "matte", "ai_image", "ai_video", "tts", "render"]
# How much AI touched an asset: none, processed (a model transformed real footage, e.g. a person
# matte) or generated (the content itself is synthetic: AI image/video/voice). Platforms ask creators
# to disclose the last kind.
AIInvolvement = Literal["none", "processed", "generated"]
_REQUIRED_INVOLVEMENT = {"ai_image": "generated", "ai_video": "generated", "tts": "generated",
                         "matte": "processed", "upload": "none", "stock": "none", "render": "none"}


class Asset(_Model):
    id: SafeId
    kind: Literal["video", "image", "audio"]
    origin: AssetOrigin
    ai: AIInvolvement = "none"
    tool: str = ""            # e.g. "u2net_human_seg", a provider name
    model: str = ""
    prompt: str = ""          # for generated assets: what was asked

    @model_validator(mode="after")
    def _honest(self):
        want = _REQUIRED_INVOLVEMENT[self.origin]
        if self.ai != want:
            raise ValueError(f"asset {self.id!r}: origin {self.origin!r} is ai={want!r}, not {self.ai!r}")
        return self


class ClipAI(_Model):
    """What the AI decided for this clip (for review and for the usage record)."""
    decided: list[str] = Field(default_factory=list)   # e.g. ["cut points", "title", "hook"]
    why: str = ""
    score: Optional[float] = None
    notes: list[str] = Field(default_factory=list)


class Word(_Model):
    w: str
    s: float = Field(ge=0)
    e: float = Field(ge=0)
    speaker: Optional[str] = None
    conf: Optional[float] = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def _ordered(self):
        if self.e < self.s:
            raise ValueError(f"word {self.w!r} ends before it starts")
        return self


class Language(_Model):
    primary: str = "en"
    code_switch: list[str] = Field(default_factory=list)
    script: Literal["Deva", "Latn", "mixed"] = "Latn"


class Source(_Model):
    uri: str
    duration: float = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: float = Field(default=30, gt=0)


class Keyframe(_Model):
    """Where the crop window is centred: ``cx`` is a fraction of the SOURCE width, ``t`` is clip time."""
    t: float = Field(ge=0)
    cx: float = Field(ge=0, le=1)


class Layout(_Model):
    # passthrough: the footage as given (reframing happened upstream).
    # cutout_behind: a person matte (alpha WebM) is painted above the text layer,
    # so `text_behind` overlays sit behind the speaker. `cutout_uri`, when given,
    # must already be aligned to the CLIP (frame 0 = clip start); when absent the
    # pipeline generates it from the prepared clip (clipcrow.matte).
    mode: Literal["passthrough", "cutout_behind"] = "passthrough"
    cutout_uri: Optional[str] = None
    # How the source frame is fitted to the vertical canvas:
    #   cover    centre crop (also right for sources that are already vertical)
    #   track    crop window follows ``reframe`` keyframes (a face-tracked reframe)
    #   fit_blur whole frame fitted to the width over a blurred copy of itself (no face to follow)
    framing: Literal["cover", "track", "fit_blur"] = "cover"
    reframe: list[Keyframe] = Field(default_factory=list, max_length=4000)

    @model_validator(mode="after")
    def _framing(self):
        if self.framing == "track" and not self.reframe:
            raise ValueError("framing 'track' needs reframe keyframes")
        if any(b.t < a.t for a, b in zip(self.reframe, self.reframe[1:])):
            raise ValueError("reframe keyframes must be in time order")
        return self


class CaptionStyle(_Model):
    enabled: bool = True
    max_words: int = Field(default=4, ge=1, le=8)
    pause_break: float = Field(default=0.15, ge=0)       # seconds of silence that ends a group
    font_family: FontFamily = "Noto Serif Devanagari"            # resolved to a file by clipcrow.fonts
    font_weight: int = 700
    color: HexColor = "#FFFFFF"
    highlight_color: HexColor = "#FFD54A"
    top_px: int = Field(default=1240, ge=0)              # portrait caption band, clear of the face
    emphasis_words: list[int] = Field(default_factory=list)  # indices into transcript.words


class _OverlayBase(_Model):
    id: SafeId
    t_in: Optional[float] = Field(default=None, ge=0)
    duration: float = Field(default=2.5, gt=0)
    trigger_word_index: Optional[int] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _anchored(self):
        if self.t_in is None and self.trigger_word_index is None:
            raise ValueError(f"overlay {self.id!r} needs t_in or trigger_word_index")
        return self


class TextBehind(_OverlayBase):
    kind: Literal["text_behind"] = "text_behind"
    text: str
    color: HexColor = "#FFD54A"
    top_px: int = 520
    font_size: int = Field(default=330, ge=40, le=600)


class LowerThird(_OverlayBase):
    kind: Literal["lower_third"] = "lower_third"
    title: str
    subtitle: str = ""
    top_px: int = 1440


class Hook(_OverlayBase):
    """The first-seconds hook line, as a banner below the platform's status bar."""
    kind: Literal["hook"] = "hook"
    text: str = Field(max_length=120)
    top_px: int = 200


class StatPill(_OverlayBase):
    kind: Literal["stat_pill"] = "stat_pill"
    text: str
    top_px: int = 220


Overlay = Annotated[Union[TextBehind, LowerThird, StatPill, Hook], Field(discriminator="kind")]


class Clip(_Model):
    id: SafeId
    source_in: float = Field(ge=0)
    source_out: float = Field(gt=0)
    title: str = ""
    layout: Layout = Field(default_factory=Layout)
    captions: CaptionStyle = Field(default_factory=CaptionStyle)
    overlays: list[Overlay] = Field(default_factory=list)
    ai: Optional[ClipAI] = None

    @property
    def duration(self) -> float:
        return self.source_out - self.source_in

    @model_validator(mode="after")
    def _window(self):
        if self.source_out <= self.source_in:
            raise ValueError(f"clip {self.id!r}: source_out must be after source_in")
        if self.duration > 180:
            raise ValueError(f"clip {self.id!r}: {self.duration:.0f}s is not a short")
        ids = [o.id for o in self.overlays]
        if len(ids) != len(set(ids)):
            raise ValueError(f"clip {self.id!r}: overlay ids must be unique")
        if self.layout.mode != "cutout_behind" and any(o.kind == "text_behind" for o in self.overlays):
            raise ValueError(f"clip {self.id!r}: text_behind overlays need layout.mode 'cutout_behind'")
        return self


class RenderSettings(_Model):
    width: int = 1080
    height: int = 1920
    fps: int = 30
    quality: Literal["draft", "standard", "high"] = "standard"


class EDL(_Model):
    version: Literal["1.0"] = "1.0"
    job_id: SafeId
    source: Source
    language: Language = Field(default_factory=Language)
    transcript: list[Word] = Field(default_factory=list)
    clips: list[Clip] = Field(min_length=1)
    render: RenderSettings = Field(default_factory=RenderSettings)
    assets: list[Asset] = Field(default_factory=list)
    ai: Optional[AIUsage] = None

    @model_validator(mode="after")
    def _clips_inside_source(self):
        for c in self.clips:
            if c.source_out > self.source.duration + 0.05:
                raise ValueError(
                    f"clip {c.id!r} ends at {c.source_out}s, past the source ({self.source.duration}s)")
        ids = [c.id for c in self.clips]
        if len(ids) != len(set(ids)):
            raise ValueError("clip ids must be unique")
        aids = [a.id for a in self.assets]
        if len(aids) != len(set(aids)):
            raise ValueError("asset ids must be unique")
        return self


def load_edl(path: str | Path) -> EDL:
    return EDL.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def json_schema() -> dict:
    return EDL.model_json_schema()
