"""Explainer beats: which sentences of a clip get a picture, and of what. The model picks the sentence, the kind and
(for a screenshot) one of the links the person supplied BY NUMBER, so it cannot invent an address; for a generated
image it writes a short description. When and for how long is code: a picture starts at its sentence, away from the
hook, with room between pictures."""
from __future__ import annotations

import base64
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
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
    picture: Optional[int] = None    # kind "upload": the index of the person's picture


def thumbnail(path: str | Path, side: int = 1024) -> bytes:
    """A JPEG of at most ``side`` pixels for the model to look at (the stored picture is what gets rendered)."""
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "t.jpg"
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-frames:v", "1", "-vf",
                            f"scale='min({side},iw)':'min({side},ih)':force_original_aspect_ratio=decrease", "-q:v", "4", str(dest)],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0 or not dest.is_file():
            raise ValueError("a picture could not be prepared for the model")
        return dest.read_bytes()


def _content(text: str, pictures: list[bytes]):
    """The model's input: each picture labelled [P0], [P1], ... then the instructions (plain text when there are none)."""
    if not pictures:
        return text
    blocks: list[dict] = []
    for i, jpeg in enumerate(pictures):
        blocks.append({"type": "text", "text": f"[P{i}]"})
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(jpeg).decode()}})
    blocks.append({"type": "text", "text": text})
    return blocks


def plan_beats(llm: LLM, sentences: list[Sentence], clip_start: float, clip_duration: float, links: list[str], *,
               allow_images: bool, language: str, hook_end: float, purpose: str,
               max_beats: int = MAX_BEATS, pictures: Optional[list[bytes]] = None) -> tuple[list[BeatPlan], list[str]]:
    """``(beats, warnings)``. Never raises for a bad or refused answer: no beats is a fine result. ``pictures`` are JPEGs of the person's own
    pictures; the model looks at them and picks one by number for a sentence."""
    pictures = pictures or []
    if not links and not allow_images and not pictures:
        return [], []
    max_beats = max(max_beats, len(pictures))
    if len(sentences) < 3:
        return [], []
    try:
        text = prompts.beats_prompt(sentences, links, allow_images=allow_images, language=language, max_beats=max_beats,
                                    pictures=len(pictures))
        res = llm.generate(prompts.SYSTEM, _content(text, pictures), BeatsResult, purpose=purpose)
    except LLMError as e:
        return [], [f"picture choices skipped: {e}"]
    by_id = {s.id: s for s in sentences}
    inner = {s.id for s in sentences[1:-1]}                 # the first and last sentence are left alone
    out: list[BeatPlan] = []
    used_links: set[int] = set()
    used_pictures: set[int] = set()
    busy_until = max(hook_end, 0.0) - GAP
    for b in sorted((b for b in res.beats if b.sentence in inner), key=lambda b: by_id[b.sentence].start):
        if len(out) >= max_beats:
            break
        s = by_id[b.sentence]
        kind, link, prompt, picture = None, None, "", None
        if b.kind == "picture" and b.picture is not None and 0 <= b.picture < len(pictures) and b.picture not in used_pictures:
            kind, picture = "upload", b.picture
        elif b.kind == "screenshot" and b.link is not None and 0 <= b.link < len(links) and b.link not in used_links:
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
        out.append(BeatPlan(round(t0, 2), round(d, 2), kind, link, prompt, caption, picture))
        if link is not None:
            used_links.add(link)
        if picture is not None:
            used_pictures.add(picture)
        busy_until = t0 + d
    return out, []
