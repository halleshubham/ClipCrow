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
import unicodedata
from pathlib import Path
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EDLError(ValueError):
    """The EDL is well-formed JSON but cannot be rendered."""


def latin_digits(text: str) -> str:
    """Every decimal digit of any script (Devanagari ३३, Arabic-Indic, ...) written as 0-9: on screen, numbers are always 33%, 1998, 1,00,300/-."""
    if text.isascii():
        return text
    return "".join(str(unicodedata.decimal(c)) if unicodedata.category(c) == "Nd" and not c.isascii() else c for c in text)


def _latinise(value):
    if isinstance(value, str):
        return latin_digits(value)
    if isinstance(value, list):
        return [_latinise(v) for v in value]
    if isinstance(value, dict):
        return {k: _latinise(v) for k, v in value.items()}
    return value


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _latin_digits(cls, data):
        return _latinise(data) if isinstance(data, dict) else data


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


AssetOrigin = Literal["upload", "stock", "matte", "ai_image", "ai_video", "tts", "render", "capture"]
# How much AI touched an asset: none, processed (a model transformed real footage, e.g. a person
# matte) or generated (the content itself is synthetic: AI image/video/voice). Platforms ask creators
# to disclose the last kind.
AIInvolvement = Literal["none", "processed", "generated"]
_REQUIRED_INVOLVEMENT = {"ai_image": "generated", "ai_video": "generated", "tts": "generated",
                         "matte": "processed", "upload": "none", "stock": "none", "render": "none",
                         "capture": "none"}          # capture: a screenshot of a web page the user pointed at


class Asset(_Model):
    id: SafeId
    kind: Literal["video", "image", "audio"]
    origin: AssetOrigin
    ai: AIInvolvement = "none"
    tool: str = ""            # e.g. "u2net_human_seg", a provider name
    model: str = ""
    prompt: str = Field(default="", max_length=600)   # for generated assets: what was asked
    uri: str = Field(default="", max_length=300)      # the file, relative to the media root ("" until it has been fetched)
    source_url: str = Field(default="", max_length=2000)   # a captured page's address (recorded, never fetched at render)
    alt: str = Field(default="", max_length=200)
    ref: str = Field(default="", pattern=r"^([0-9a-f]{12})?$")   # an upload: which of the person's pictures (its id), copied into the job before render

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


class SplitPoint(_Model):
    """One window of a two-person split: where the face is, as fractions of the SOURCE frame."""
    cx: float = Field(ge=0, le=1)
    cy: float = Field(ge=0, le=1)


class FitSpan(_Model):
    """A stretch of clip time where the WHOLE source frame is shown (fitted to the width over a blurred copy) instead of the tracked crop: a slide,
    a title card or other on-screen graphics that a vertical crop would cut in half. Both ends are hard cuts."""
    t0: float = Field(ge=0)
    t1: float = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self):
        if self.t1 <= self.t0:
            raise ValueError("a fit span must end after it starts")
        return self


class PunchIn(_Model):
    """A slow push-in on the footage (and the cutout and the text behind it, together) that snaps back at the end.
    ``t`` is clip time. ``anchor_y`` is where the push aims, as a fraction of the frame height (a face sits near 0.4)."""
    t: float = Field(ge=0)
    duration: float = Field(ge=0.5, le=8)
    scale: float = Field(default=1.15, ge=1.03, le=1.5)
    anchor_y: float = Field(default=0.4, ge=0, le=1)


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
    #   split    two people stacked: the top half follows ``split[0]``, the bottom half ``split[1]``
    # A speaker change in a ``track`` framing is just a jump between two keyframes: a hard cut.
    framing: Literal["cover", "track", "fit_blur", "split"] = "cover"
    reframe: list[Keyframe] = Field(default_factory=list, max_length=4000)
    split: list[SplitPoint] = Field(default_factory=list, max_length=2)
    punch_ins: list[PunchIn] = Field(default_factory=list, max_length=12)
    # ``track`` only: stretches shown whole (see FitSpan), in time order, not overlapping.
    fit_spans: list[FitSpan] = Field(default_factory=list, max_length=40)
    # Where the face is in the vertical frame over the whole clip, as (x0, y0, x1, y1) fractions of the canvas: what the logo keeps clear of. None: not known.
    face_zone: Optional[tuple[float, float, float, float]] = None

    @field_validator("face_zone")
    @classmethod
    def _zone_in_the_frame(cls, v):
        if v is not None and not (all(0.0 <= x <= 1.0 for x in v) and v[0] < v[2] and v[1] < v[3]):
            raise ValueError("face_zone must be (x0, y0, x1, y1) inside the frame, x0 < x1 and y0 < y1")
        return v

    @model_validator(mode="after")
    def _framing(self):
        if self.fit_spans and self.framing != "track":
            raise ValueError("fit_spans belong to framing 'track'")
        if any(b.t0 < a.t1 for a, b in zip(self.fit_spans, self.fit_spans[1:])):
            raise ValueError("fit_spans must be in time order and must not overlap")
        if self.framing == "track" and not self.reframe:
            raise ValueError("framing 'track' needs reframe keyframes")
        if any(b.t < a.t + a.duration for a, b in zip(self.punch_ins, self.punch_ins[1:])):
            raise ValueError("punch_ins must be in time order and must not overlap")
        if (self.framing == "split") != (len(self.split) == 2):
            raise ValueError("framing 'split' needs exactly two split points (and only then)")
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
    size_scale: float = Field(default=1.0, ge=0.6, le=1.6)   # multiplies the fitted size (a style may set it)
    uppercase: bool = False                              # Latin only: Devanagari has no case
    emphasis_words: list[int] = Field(default_factory=list)  # indices into transcript.words
    animation: Literal["pop", "fade", "slide", "karaoke"] = "pop"   # how a caption group moves; "pop" is the original look (same pixels as before this field existed)


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


class MediaCard(_OverlayBase):
    """A picture or video shown over the footage for a few seconds (explainer mode): ``full`` replaces the whole
    frame (B-roll cutaway, the audio and captions carry on), ``panel`` is a rounded card in the upper part. The
    file is an ``Asset`` of this EDL, so what is shown, and whether the AI made it, is on the record."""
    kind: Literal["media"] = "media"
    asset: SafeId
    placement: Literal["full", "panel"] = "full"
    top_px: int = Field(default=200, ge=0)
    height_px: int = Field(default=900, ge=200, le=1800)
    motion: Literal["none", "zoom_in", "pan_left", "pan_right"] = "zoom_in"
    caption: str = Field(default="", max_length=80)


Overlay = Annotated[Union[TextBehind, LowerThird, StatPill, Hook, MediaCard], Field(discriminator="kind")]


class Cover(_Model):
    """A bold title shown for the first few hundred milliseconds, so that the frame a platform (or a person) picks as
    the poster of the video carries the title. It sits on top of the footage and does not move the timeline."""
    text: str = Field(max_length=80)
    enabled: bool = True
    duration_ms: int = Field(default=250, ge=100, le=1500)
    background: Literal["frame", "color"] = "frame"     # frame: the footage, darkened; color: a solid colour
    color: HexColor = "#FFFFFF"                         # the title
    accent: HexColor = "#FFD54A"                        # the last line is drawn in this
    bg_color: HexColor = "#111111"


LogoPosition = Literal["tl", "tr", "bl", "br"]


class Logo(_Model):
    """The brand's logo (an image Asset of this EDL) in a corner, from the end of the cover to the end of the clip."""
    asset: SafeId
    enabled: bool = True
    position: LogoPosition = "tr"
    size_pct: int = Field(default=18, ge=5, le=40)      # width as a percentage of the frame width
    opacity: float = Field(default=0.9, ge=0.1, le=1.0)
    margin_px: int = Field(default=60, ge=0, le=300)
    top_px: Optional[int] = Field(default=None, ge=0, le=1900)   # set by the placement that keeps the logo off the face: the logo's top edge, down the left or right side (None: the corner's own height)
    shrink: float = Field(default=1.0, ge=0.3, le=1.0)           # (clips made before the logo stopped shrinking carry a value below 1; new ones never do)
    faded_opacity: Optional[float] = Field(default=None, ge=0.05, le=1.0)   # ... and the lower opacity used when no spot is clear of the face: the logo keeps its size and fades
    side_px: Optional[int] = Field(default=None, ge=0, le=300)   # ... and a narrower side margin when that was needed (None: ``margin_px``)


BannerPlatform = Literal["youtube", "instagram", "facebook", "tiktok", "x", "linkedin", "telegram", "whatsapp"]


class Banner(_Model):
    """A small "watch the full video" banner low in the frame: a line of words over a pill with the platform's mark and the channel name. Plain data, no file."""
    text: str = Field(default="", max_length=60)               # "" = the words for the language ("Watch full video:" / "पूर्ण व्हिडीओ पहा:")
    handle: str = Field(min_length=1, max_length=40)           # the channel name, shown as given
    platform: BannerPlatform = "youtube"
    enabled: bool = True
    top_px: int = Field(default=1440, ge=0, le=1800)           # the top edge, on the 1080x1920 canvas: under the captions, above the platform's own buttons
    seconds: float = Field(default=0.0, ge=0.0, le=60.0)       # 0 = shown from the start of the clip; else only for the last this many seconds

    @field_validator("text", "handle")
    @classmethod
    def _no_control(cls, v: str) -> str:
        if any(ord(c) < 32 for c in v):
            raise ValueError("no control characters")
        return v.strip()


class Outro(_Model):
    """A branding video (a video Asset of this EDL) appended after the clip. Done after the render, with FFmpeg."""
    asset: SafeId
    enabled: bool = True
    max_seconds: float = Field(default=6.0, gt=0, le=30)
    gap_ms: int = Field(default=400, ge=0, le=1500)      # a dip before the ending: the clip fades out, a short black silent gap, the ending fades in (0 = a straight cut)


MAX_CLIP_SECONDS = 900      # a clip may run 15 minutes: shorts are 15-90 s, but a cleaned recording is one reel of 3-8 minutes


class Sfx(_Model):
    """Quiet sound cues (pop, whoosh, tick, ding) laid under the speech where a title, card, picture or push-in appears. WHICH cue goes WHERE is code
    (``cues.plan_cues``), never the model; this only switches it on and says how loud the loudest cue peaks (dBFS)."""
    enabled: bool = True
    level_db: float = Field(default=-10.0, ge=-24.0, le=-3.0)


class Music(_Model):
    """A background track (an audio Asset of this EDL, the account's own upload) under the speech. ``level_db`` is where it sits RELATIVE to the speech's
    loudness while nobody talks; while someone does it is ducked further (``music.mix``: sidechain compression), so the words stay first."""
    asset: SafeId
    enabled: bool = True
    level_db: float = Field(default=-10.0, ge=-24.0, le=-4.0)


class Transition(_Model):
    """A short effect on every hard cut of the clip (a jump cut from the cleanup, a speaker change, the edge of a slide). WHICH cuts is code
    (``transitions.hard_cuts``); this only picks one of a closed set of looks and switches it on or off."""
    enabled: bool = True
    style: Literal["flash", "blur", "dip", "whip"] = "flash"


class Clip(_Model):
    id: SafeId
    source_in: float = Field(ge=0)
    source_out: float = Field(gt=0)
    title: str = ""
    layout: Layout = Field(default_factory=Layout)
    captions: CaptionStyle = Field(default_factory=CaptionStyle)
    overlays: list[Overlay] = Field(default_factory=list)
    cover: Optional[Cover] = None
    logo: Optional[Logo] = None
    banner: Optional[Banner] = None
    outro: Optional[Outro] = None
    music: Optional[Music] = None
    sfx: Optional[Sfx] = None
    transition: Optional[Transition] = None
    seams: list[float] = Field(default_factory=list, max_length=400)   # clip times where the cleanup joined two stretches (a jump cut), in time order
    ai: Optional[ClipAI] = None

    @property
    def duration(self) -> float:
        return self.source_out - self.source_in

    @model_validator(mode="after")
    def _window(self):
        if self.source_out <= self.source_in:
            raise ValueError(f"clip {self.id!r}: source_out must be after source_in")
        if self.duration > MAX_CLIP_SECONDS:
            raise ValueError(f"clip {self.id!r}: {self.duration:.0f}s is longer than the {MAX_CLIP_SECONDS // 60} minutes a clip may run")
        if any(p.t + p.duration > self.duration + 0.01 for p in self.layout.punch_ins):
            raise ValueError(f"clip {self.id!r}: a punch-in runs past the end of the clip")
        ids = [o.id for o in self.overlays]
        if len(ids) != len(set(ids)):
            raise ValueError(f"clip {self.id!r}: overlay ids must be unique")
        if self.layout.mode != "cutout_behind" and any(o.kind == "text_behind" for o in self.overlays):
            raise ValueError(f"clip {self.id!r}: text_behind overlays need layout.mode 'cutout_behind'")
        if any(s <= 0 or s >= self.duration for s in self.seams) or any(b < a for a, b in zip(self.seams, self.seams[1:])):
            raise ValueError(f"clip {self.id!r}: seams must be inside the clip and in time order")
        if self.cover and self.cover.duration_ms / 1000 > self.duration / 2:
            raise ValueError(f"clip {self.id!r}: the cover must be shorter than half the clip")
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
        kinds = {a.id: a.kind for a in self.assets}
        for c in self.clips:
            for o in c.overlays:
                if o.kind == "media" and kinds.get(o.asset) not in ("image", "video"):
                    raise ValueError(f"clip {c.id!r}: media overlay {o.id!r} needs an image or video asset, "
                                     f"{o.asset!r} is {kinds.get(o.asset, 'not in the EDL')}")
            for what, ref, want in (("logo", c.logo, "image"), ("outro", c.outro, "video")):
                if ref is not None and kinds.get(ref.asset) != want:
                    raise ValueError(f"clip {c.id!r}: the {what} needs a {want} asset, "
                                     f"{ref.asset!r} is {kinds.get(ref.asset, 'not in the EDL')}")
        return self


def load_edl(path: str | Path) -> EDL:
    return EDL.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def json_schema() -> dict:
    return EDL.model_json_schema()
