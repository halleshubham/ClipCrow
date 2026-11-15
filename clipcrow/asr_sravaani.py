"""SraVaani (ARTPARK-IISc, MIT, ~430 M parameter FastConformer TDT-CTC) as an alternative recogniser for Marathi / Hindi.

Why: on real Marathi recordings Whisper throws low-confidence windows away as silence and stretches single words; SraVaani wrote every spoken
word, stumbles included (the repeats a cleanup has to see), runs on a CPU, and its TDT decoder knows the frame each token was emitted on, so it
gives word starts of its own. Opt-in: ``CLIPCROW_ASR=sravaani``. Anything that goes wrong falls back to Whisper (see ``asr.transcribe``).

  * Audio goes in windows of at most ``WINDOW_SECONDS``, cut at the quietest 100 ms near the end, so no word is split.
  * A word's start moves to where the voice begins if the model's frame is early; its end from the model is "where the next word starts"; it is pulled back to where the voice stops (``energy``), so a pause belongs to
    no word. A word never starts before the last one ended and never lasts longer than ``MAX_WORD``.
  * The model comes from a local folder (``CLIPCROW_SRAVAANI_DIR``) or the Hugging Face repo at a PINNED revision (the repo ships Python that
    ``trust_remote_code`` runs: never follow a moving branch). The repo is gated; ``HF_TOKEN`` must belong to an account that has access.
"""
from __future__ import annotations

import os
import threading
from typing import Callable

from .energy import HOP, SAMPLE_RATE, energy_db, speech_threshold, voiced_runs

LANGS = {"mr", "hi"}
REPO = "ARTPARK-IISc/SraVaani-1.0"
REVISION = "a7254684e699e464fb70d10ad301e28fa0330cad"
WINDOW_SECONDS = 28.0
SEARCH_FROM = 18.0               # a cut is looked for between this many seconds into a window and its end
MAX_WORD = 1.5

_model = None
_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get("CLIPCROW_ASR", "").strip().lower() == "sravaani"


def _load():
    global _model
    with _lock:
        if _model is None:
            from transformers import AutoModel
            src = os.environ.get("CLIPCROW_SRAVAANI_DIR")
            kw = {"trust_remote_code": True}
            if not src:
                src, kw["revision"] = os.environ.get("CLIPCROW_SRAVAANI_REPO", REPO), REVISION
            _model = AutoModel.from_pretrained(src, **kw).eval()
        return _model


def release() -> None:
    global _model
    with _lock:
        _model = None


def split_points(db, total: float) -> list[float]:
    """Window boundaries in seconds: each cut is the quietest 100 ms in the last part of a window."""
    import numpy as np
    cuts = [0.0]
    span = int(0.1 / HOP)
    while total - cuts[-1] > WINDOW_SECONDS:
        a, b = int((cuts[-1] + SEARCH_FROM) / HOP), int((cuts[-1] + WINDOW_SECONDS - 0.2) / HOP)
        seg = np.asarray(db[a:b + span])
        if len(seg) <= span:
            cuts.append(cuts[-1] + WINDOW_SECONDS)
            continue
        sums = np.convolve(seg, np.ones(span) / span, "valid")
        sums = sums[: b - a]
        quiet = np.flatnonzero(sums <= sums.min() + 0.5)           # the middle of the quietest stretch, not its first frame
        cuts.append((a + int(quiet[len(quiet) // 2]) + span // 2) * HOP)
    cuts.append(total)
    return cuts


def tighten(words: list[dict], db, thr: float) -> list[dict]:
    """End each word where the voice stops (not at the next word's start); order and ``MAX_WORD`` are enforced."""
    out: list[dict] = []
    for i, w in enumerate(words):
        s = w["s"] if not out else max(w["s"], out[-1]["e"])
        limit = words[i + 1]["s"] if i + 1 < len(words) else w["e"]
        limit = min(max(limit, s + 0.05), s + MAX_WORD)
        runs = [r for r in voiced_runs(db, thr, s, limit)]
        if runs and 0.1 < runs[0][0] - s <= 0.3:               # the model's start is a frame early: the word begins where the voice does
            s = runs[0][0] - 0.02
        e = min(limit, runs[-1][1]) if runs else min(limit, s + 0.25)
        out.append({"w": w["w"], "s": s, "e": max(e, s + 0.05)})
    return out


def transcribe_words(media: str, *, say: Callable[[str], None] = lambda _m: None, model=None,
                     audio_loader: Callable | None = None, check: Callable[[], None] = lambda: None) -> list[dict]:
    """``[{"w", "s", "e"}]`` for the whole of ``media``."""
    import tempfile
    import wave

    import numpy as np
    from .asr import load_audio
    audio = (audio_loader or load_audio)(media)
    total = len(audio) / SAMPLE_RATE
    db = energy_db(audio)
    thr = speech_threshold(db)
    model = model or _load()
    cuts = split_points(db, total)
    words: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        for k in range(len(cuts) - 1):
            check()
            a, b = cuts[k], cuts[k + 1]
            chunk = audio[int(a * SAMPLE_RATE):int(b * SAMPLE_RATE)]
            if len(chunk) < 0.3 * SAMPLE_RATE:
                continue
            path = os.path.join(tmp, f"w{k}.wav")
            with wave.open(path, "wb") as f:
                f.setnchannels(1)
                f.setsampwidth(2)
                f.setframerate(SAMPLE_RATE)
                f.writeframes((np.clip(chunk, -1, 1) * 32767).astype(np.int16).tobytes())
            hyp = model.transcribe([path], return_hypotheses=True, timestamps=True)[0]
            for w in (getattr(hyp, "timestamp", None) or {}).get("word", []):
                if str(w["word"]).strip():
                    words.append({"w": str(w["word"]), "s": float(w["start"]) + a, "e": float(w["end"]) + a})
            say(f"transcribed {int(b)} of {int(total)} s (SraVaani)")
    return tighten(words, db, thr)
