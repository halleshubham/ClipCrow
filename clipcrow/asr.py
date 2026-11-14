"""Speech to a word-timed Transcript with faster-whisper, tuned for Marathi and Hindi.

    transcribe("podcast.mp4")                   # language voted, Devanagari seed, word timestamps
    transcribe("talk.mp4", language="mr")       # pin the language

Env: CLIPCROW_WHISPER_MODEL (default "small"), CLIPCROW_INDIC_MODEL (a larger model used only for
Indic audio, e.g. "large-v3"), CLIPCROW_WHISPER_DEVICE ("auto" by default: the GPU when CTranslate2 can see one, else "cpu";
or force "cpu"/"cuda"), CLIPCROW_WHISPER_COMPUTE ("auto": float16 on a GPU, int8 on a CPU).
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

# Devanagari speech (Marathi, Hindi, ...) is decoded the way the Thet reels skill found to work on real interviews: NO prompt (a prompt or a
# hotword list gets echoed back as the transcript), NO voice-activity filter (it drops speech), independent 15 s windows with the language pinned
# for each, and loop guards. A whole-file pass drifts into the related language and loops; this one is slower but keeps the speech.
WINDOWED_LANGS = {"mr", "hi", "ne", "sa"}
WINDOW_SECONDS = 15.0
SAMPLE_RATE = 16000
WINDOWED_DECODE = {
    "beam_size": 5, "best_of": 5, "temperature": [0.0, 0.2, 0.4], "word_timestamps": True, "vad_filter": False,
    "condition_on_previous_text": False, "repetition_penalty": 1.2, "no_repeat_ngram_size": 4,
    "compression_ratio_threshold": 2.2, "no_speech_threshold": 0.9, "log_prob_threshold": -1.5,
}


@dataclass
class AsrConfig:
    model: str = field(default_factory=lambda: os.environ.get("CLIPCROW_WHISPER_MODEL", "small"))
    indic_model: str | None = field(default_factory=lambda: os.environ.get("CLIPCROW_INDIC_MODEL") or None)
    device: str = field(default_factory=lambda: os.environ.get("CLIPCROW_WHISPER_DEVICE") or "auto")
    compute_type: str = field(default_factory=lambda: os.environ.get("CLIPCROW_WHISPER_COMPUTE") or "auto")


def _cuda_devices() -> int:
    try:
        import ctranslate2
        return int(ctranslate2.get_cuda_device_count())
    except Exception:                                  # no CUDA build, no driver, no GPU: all mean "use the CPU"
        return 0


def resolve_device(device: str, compute_type: str, cuda_devices: Callable[[], int] = _cuda_devices) -> tuple[str, str]:
    """``("auto", "auto")`` becomes ``("cuda", "float16")`` when a GPU is visible and ``("cpu", "int8")`` when not. A forced
    ``cuda`` is kept (and fails loudly if there is no GPU, which is what an operator who forced it wants to know)."""
    if device == "auto":
        device = "cuda" if cuda_devices() > 0 else "cpu"
    if compute_type == "auto":
        compute_type = "float16" if device == "cuda" else "int8"
    return device, compute_type


_models: dict[tuple, object] = {}
_lock = threading.Lock()


def _load(cfg: AsrConfig, size: str):
    device, compute = resolve_device(cfg.device, cfg.compute_type)
    key = (size, device, compute)
    with _lock:
        if key not in _models:
            from faster_whisper import WhisperModel   # lazy: unit tests and the planner need no model
            _models[key] = WhisperModel(size, device=device, compute_type=compute)
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


_PUNCT = str.maketrans("", "", " \t\n.,!?;:।॥-–—\"'()…")


def drop_seed_echo(segments: list, seed: str | None) -> list:
    """With a prompt in front of every window, a model can read the prompt back out over a silent or musical stretch. A segment that is
    only a piece of the seed (and long enough not to be a real short phrase) is dropped."""
    if not seed:
        return segments
    s = seed.translate(_PUNCT)
    out = []
    for seg in segments:
        n = str(getattr(seg, "text", "")).translate(_PUNCT)
        if len(n) >= 12 and n in s:
            continue
        out.append(seg)
    return out


@dataclass
class _W:
    word: str
    start: float
    end: float


@dataclass
class _Seg:
    text: str
    words: list


def load_audio(media: str):
    """The whole media as 16 kHz mono float32 samples (one ffmpeg pass; an hour is about 230 MB)."""
    import subprocess
    import numpy as np
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(media), "-vn", "-ar", str(SAMPLE_RATE), "-ac", "1", "-f", "f32le", "pipe:1"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0 or not r.stdout:
        raise TranscriptError("could not read audio from this media")
    return np.frombuffer(r.stdout, dtype=np.float32)


def _decode_kwargs(model, lang: str) -> dict:
    """The windowed settings the installed faster-whisper accepts (a test double or an older version may take fewer)."""
    kw = {k: v for k, v in WINDOWED_DECODE.items() if indic._takes(model, k)}
    kw["language"] = lang
    return kw


def transcribe_windowed(model, media: str, lang: str, *, say: Callable[[str], None] = lambda _m: None,
                        audio_loader: Callable | None = None) -> list:
    """Decode ``media`` in independent ``WINDOW_SECONDS`` windows; segments come back with absolute word times."""
    audio = (audio_loader or load_audio)(media)
    kw = _decode_kwargs(model, lang)
    total = len(audio) / SAMPLE_RATE
    out: list = []
    start, n = 0.0, 0
    while start < total:
        end = min(start + WINDOW_SECONDS, total)
        chunk = audio[int(start * SAMPLE_RATE):int(end * SAMPLE_RATE)]
        if len(chunk) >= 0.3 * SAMPLE_RATE:                       # a sliver of audio is only noise
            got, _info = model.transcribe(chunk, **kw)
            for seg in got:
                words = [_W(w.word, float(w.start) + start, float(w.end) + start) for w in (seg.words or [])]
                if words:
                    out.append(_Seg(str(seg.text), words))
        start, n = end, n + 1
        if n % 10 == 0:
            say(f"transcribed {int(start)} of {int(total)} s")
    return out


def transcribe(media: str, language: str | None = None, *, config: AsrConfig | None = None,
               model_loader: Callable | None = None, progress: Callable[[str], None] | None = None,
               windowed: bool | None = None, audio_loader: Callable | None = None) -> Transcript:
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
    use_windows = (lang in WINDOWED_LANGS) if windowed is None else (bool(windowed) and bool(lang))
    if use_windows:
        say(f"decoding {lang} in {int(WINDOW_SECONDS)} s windows, no prompt")
        segments = collapse_loops(transcribe_windowed(model, media, lang, say=say, audio_loader=audio_loader))
        info = None
    else:
        for key, value in indic.prompt_params(model, lang).items():          # a pinned language still needs its anchors
            params.setdefault(key, value)
        segments, info = model.transcribe(media, **DECODE, **params)
        segments = drop_seed_echo(collapse_loops(list(segments)), params.get("hotwords") or params.get("initial_prompt"))
    raw = [{"w": w.word, "s": float(w.start), "e": float(w.end)}
           for seg in segments for w in (seg.words or [])]
    words = merge_fragments(raw)
    if not words:
        raise TranscriptError("no speech was recognised in this media")
    return parse_transcript({"language": getattr(info, "language", None) or lang or "en", "words": words})
