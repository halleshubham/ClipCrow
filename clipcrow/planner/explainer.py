"""Explainer beats: which sentences of a clip get a picture, and of what. The model picks the sentence, the kind and
(for a screenshot) one of the links the person supplied BY NUMBER, so it cannot invent an address; for a generated
image it writes a short description. When and for how long is code: a picture starts at its sentence, away from the
hook, with room between pictures."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import prompts
from .llm import LLM, LLMError
from .models import BeatsResult
from .select import clean_text, wrong_script
from .transcript import Sentence

MAX_BEATS = 3
MIN_SHOW, MAX_SHOW = 2.0, 5.0
GAP = 1.0
PROMPT_MIN, PROMPT_MAX = 8, 300


@dataclass
class BeatPlan:
    t: float                 # clip time
    duration: float
    kind: str                # "capture" | "ai_image"
    link: Optional[int]
    prompt: str
    caption: str


def plan_beats(llm: LLM, sentences: list[Sentence], clip_start: float, clip_duration: float, links: list[str], *,
               allow_images: bool, language: str, hook_end: float, purpose: str,
               max_beats: int = MAX_BEATS) -> tuple[list[BeatPlan], list[str]]:
    """``(beats, warnings)``. Never raises for a bad or refused answer: no beats is a fine result."""
    if not links and not allow_images:
        return [], []
    if len(sentences) < 3:
        return [], []
    try:
        res = llm.generate(prompts.SYSTEM, prompts.beats_prompt(sentences, links, allow_images=allow_images,
                                                                 language=language, max_beats=max_beats),
                           BeatsResult, purpose=purpose)
    except LLMError as e:
        return [], [f"picture choices skipped: {e}"]
    by_id = {s.id: s for s in sentences}
    inner = {s.id for s in sentences[1:-1]}                 # the first and last sentence are left alone
    out: list[BeatPlan] = []
    used_links: set[int] = set()
    busy_until = max(hook_end, 0.0) - GAP
    for b in sorted((b for b in res.beats if b.sentence in inner), key=lambda b: by_id[b.sentence].start):
        if len(out) >= max_beats:
            break
        s = by_id[b.sentence]
        kind, link, prompt = None, None, ""
        if b.kind == "screenshot" and b.link is not None and 0 <= b.link < len(links) and b.link not in used_links:
            kind, link = "capture", b.link
        elif b.kind == "image" and allow_images and PROMPT_MIN <= len((b.prompt or "").strip()) and not wrong_script("", language):
            prompt = clean_text(b.prompt, PROMPT_MAX)
            kind = "ai_image" if len(prompt) >= PROMPT_MIN else None
        if kind is None:
            continue
        t0 = max(s.start - clip_start, 0.0)
        if t0 < busy_until + GAP:
            continue
        d = min(MAX_SHOW, max(MIN_SHOW, s.end - s.start), clip_duration - t0 - 0.3)
        if d < 1.5:
            continue
        caption = clean_text(b.caption, 80)
        if caption and wrong_script(caption, language):
            caption = ""
        out.append(BeatPlan(round(t0, 2), round(d, 2), kind, link, prompt, caption))
        if link is not None:
            used_links.add(link)
        busy_until = t0 + d
    return out, []
