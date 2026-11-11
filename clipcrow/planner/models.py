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
    text: str


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
