"""Speech to a word-timed Transcript with faster-whisper, tuned for Marathi and Hindi.

    transcribe("podcast.mp4")                   # language voted, Devanagari seed, word timestamps
    transcribe("talk.mp4", language="mr")       # pin the language

Env: CLIPCROW_WHISPER_MODEL (default "small"), CLIPCROW_INDIC_MODEL (a larger model used only for
Indic audio, e.g. "large-v3"), CLIPCROW_WHISPER_DEVICE ("cpu"/"cuda"), CLIPCROW_WHISPER_COMPUTE.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Callable

from . import indic
from .planner.transcript import Transcript, TranscriptError, parse_transcript

# Decode settings: no conditioning on previous text (avoids repetition loops), VAD drops silence.
DECODE = {"beam_size": 5, "vad_filter": True, "condition_on_previous_text": False, "word_timestamps": True}
LOOP_RUN = 3          # the same segment this many times in a row is a Whisper repetition loop


@dataclass
class AsrConfig:
    model: str = field(default_factory=lambda: os.environ.get("CLIPCROW_WHISPER_MODEL", "small"))
    indic_model: str | None = field(default_factory=lambda: os.environ.get("CLIPCROW_INDIC_MODEL") or None)
    device: str = field(default_factory=lambda: os.environ.get("CLIPCROW_WHISPER_DEVICE", "cpu"))
    compute_type: str = field(default_factory=lambda: os.environ.get("CLIPCROW_WHISPER_COMPUTE", "int8"))


_models: dict[tuple, object] = {}
_lock = threading.Lock()


def _load(cfg: AsrConfig, size: str):
    key = (size, cfg.device, cfg.compute_type)
    with _lock:
        if key not in _models:
            from faster_whisper import WhisperModel   # lazy: unit tests and the planner need no model
            _models[key] = WhisperModel(size, device=cfg.device, compute_type=cfg.compute_type)
        return _models[key]


def release_models() -> None:
    """Drop cached models (frees GPU memory between jobs)."""
    with _lock:
        _models.clear()


def merge_fragments(words: list[dict]) -> list[dict]:
    """faster-whisper marks a word start with a leading space; a token without one continues the previous
    word. Join those so "YouTube" + "-Kanal" or a split Devanagari conjunct are one caption word."""
    out: list[dict] = []
    for w in words:
        text = w["w"]
        if out and text and not text.startswith(" "):
            out[-1]["w"] += text
            out[-1]["e"] = w["e"]
        else:
            out.append(dict(w))
    for w in out:
        w["w"] = w["w"].strip()
    return [w for w in out if w["w"]]


def collapse_loops(segments: list) -> list:
    """Drop the repeats of a segment that Whisper emits over and over (a known failure on silence/music)."""
    out: list = []
    run = 0
    for seg in segments:
        text = seg.text.strip()
        if out and text == out[-1].text.strip():
            run += 1
            if run >= LOOP_RUN - 1:
                continue
        else:
            run = 0
        out.append(seg)
    return out


def transcribe(media: str, language: str | None = None, *, config: AsrConfig | None = None,
               model_loader: Callable | None = None, progress: Callable[[str], None] | None = None) -> Transcript:
    cfg = config or AsrConfig()
    load = model_loader or _load
    say = progress or (lambda _m: None)
    model = load(cfg, cfg.model)

    params: dict = {"language": language} if language else indic.decode_params(model, media)
    lang = params.get("language")
    if lang:
        say(f"language: {lang}{' (script seed on)' if 'initial_prompt' in params else ''}")
    if lang in indic.INDIC_LANGS and cfg.indic_model and cfg.indic_model != cfg.model:
        say(f"Indic audio: using {cfg.indic_model}")
        model = load(cfg, cfg.indic_model)
    if lang in indic.SCRIPT_SEED and "initial_prompt" not in params:      # language was pinned by the caller
        params["initial_prompt"] = indic.SCRIPT_SEED[lang]

    segments, info = model.transcribe(media, **DECODE, **params)
    segments = collapse_loops(list(segments))
    raw = [{"w": w.word, "s": float(w.start), "e": float(w.end)}
           for seg in segments for w in (seg.words or [])]
    words = merge_fragments(raw)
    if not words:
        raise TranscriptError("no speech was recognised in this media")
    return parse_transcript({"language": getattr(info, "language", None) or lang or "en", "words": words})
