"""What the model is asked to return. Kept deliberately plain (no numeric constraints, simple
Optionals) so the structured-output schema stays inside what the API accepts; every value is
re-validated and clamped in select.py."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class WindowScore(BaseModel):
    id: int
    hook: int
    completeness: int
    standalone: int
    emotion: int
    why: str


class ScoreResult(BaseModel):
    windows: list[WindowScore]


class Mark(BaseModel):
    sentence: int
    word: str


class Stat(BaseModel):
    sentence: int
    word: str      # the spoken word of the figure, copied exactly from the sentence (anchors the pill in time)
    text: str      # the figure as shown on screen


class RawClip(BaseModel):
    first_sentence: int
    last_sentence: int
    title: str
    hook_text: str
    why: str
    virality: int
    emphasis: list[Mark]
    behind: Optional[Mark] = None
    stat: Optional[Stat] = None


class DetailResult(BaseModel):
    clips: list[RawClip]


class Beat(BaseModel):
    sentence: int            # the sentence this picture belongs to (an id as given)
    kind: str                # "picture" (one the person supplied), "screenshot" (a link from the list), "image" (a generated picture) or "none"
    link: Optional[int] = None
    picture: Optional[int] = None   # for "picture": the number of the person's picture (as in P1)
    prompt: str = ""         # for "image": what to draw, plain description
    caption: str = ""        # at most a few words shown under the picture
    cue: str = ""            # for "picture": one word copied from the sentence, where the picture should appear
    crop: Optional[list[float]] = None   # for "picture": the part that matters, [x0, y0, x1, y1] as fractions (0 to 1) of the picture; None = all of it
    read_words: int = 0      # for "picture": how many words a viewer has to read in that part (0 for a photo)
    source: Optional[list[float]] = None   # for "picture": where the picture itself shows where it comes from (address bar, site logo or name, masthead, account header, watermark), [x0, y0, x1, y1]
    source_name: str = ""    # for "picture": the name of that site or platform as the picture shows it ("X", "The Hindu"), "" when none is visible


class BeatsResult(BaseModel):
    beats: list[Beat]
