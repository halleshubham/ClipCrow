"""Who speaks when: voice turns of a recording, found from the audio alone.

    audio (16 kHz mono) -> voiced 1.5 s windows -> a voice embedding per window -> clusters = voices -> turns

The embedding comes from a speaker-verification model (``microsoft/wavlm-base-plus-sv``, MIT, through ``transformers``, which the
image has already). Clustering is average-linkage on cosine similarity, written here in numpy (a recording has a few hundred
windows), with a cap on the number of voices. The result is ``[(start, end, voice)]``; a voice is a number, not a name.

Nothing here knows about faces: ``seats.py`` matches voices to places on the screen. The embedder is a one-method object so a test
(or a better model) can stand in for it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Protocol

from .energy import HOP, SAMPLE_RATE, energy_db, speech_threshold, voiced_runs

WINDOW = 1.5               # seconds of audio per embedding
MIN_VOICED = 0.9           # a window needs this much voiced audio to be embedded (a breath is not a voice)
MAX_WINDOWS = 900          # a very long recording is embedded on a stride, so the cost stays bounded
SAME_VOICE = 0.60          # average cosine similarity at which two groups of windows are one voice
MAX_VOICES = 6
MIN_GROUP = 3              # a group of fewer windows than this joins the closest voice (or is dropped when none is close)
ATTACH = 0.45              # ... if it is at least this similar
MODEL = "microsoft/wavlm-base-plus-sv"
MODEL_REVISION = None      # pin a revision here once one has been checked on real recordings


@dataclass(frozen=True)
class Turn:
    start: float
    end: float
    voice: int


class Embedder(Protocol):
    def embed(self, chunks: list) -> "object":
        """One L2-normalised vector per 16 kHz mono float chunk, as an array of shape (n, d)."""


class WavLMEmbedder:
    """Speaker embeddings from WavLM-base-plus-sv (x-vector head). Loaded on first use; ``release()`` frees it."""

    def __init__(self, batch: int = 8, device: str = "auto"):
        self.batch, self.device = batch, device                       # "auto": the GPU when torch can see one (a worker), else the CPU
        self._model = self._fe = None

    def _load(self):
        if self._model is None:
            from transformers import AutoFeatureExtractor, WavLMForXVector
            kw = {"revision": MODEL_REVISION} if MODEL_REVISION else {}
            self._fe = AutoFeatureExtractor.from_pretrained(MODEL, **kw)
            import torch
            if self.device == "auto":
                self.device = "cuda" if torch.cuda.is_available() else "cpu"
            self._model = WavLMForXVector.from_pretrained(MODEL, **kw).eval().to(self.device)
        return self._model

    def embed(self, chunks: list):
        import numpy as np
        import torch
        model = self._load()
        out = []
        with torch.no_grad():
            for i in range(0, len(chunks), self.batch):
                inp = self._fe([np.asarray(c, dtype=np.float32) for c in chunks[i:i + self.batch]], sampling_rate=SAMPLE_RATE,
                               return_tensors="pt", padding=True)
                inp = {k: v.to(self.device) for k, v in inp.items()}
                out.append(torch.nn.functional.normalize(model(**inp).embeddings, dim=-1).cpu().numpy())
        return np.concatenate(out) if out else np.zeros((0, 1), np.float32)

    def release(self) -> None:
        self._model = self._fe = None
        import gc
        gc.collect()


def default_embedder() -> Embedder | None:
    """The model embedder, or None when voices are switched off (``CLIPCROW_SPEAKERS=0``) or ``transformers``/``torch`` are not installed."""
    if os.environ.get("CLIPCROW_SPEAKERS", "1") == "0":
        return None
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except ImportError:
        return None
    return WavLMEmbedder()


def voiced_windows(db, thr: float, total: float, *, window: float = WINDOW, max_windows: int = MAX_WINDOWS) -> list[tuple[float, float]]:
    """Non-overlapping ``window``-second spans that hold enough voiced audio, on a stride when there are more than ``max_windows``."""
    runs = voiced_runs(db, thr, 0.0, total, min_gap=0.25)
    spans: list[tuple[float, float]] = []
    t = 0.0
    while t + window <= total + 1e-6:
        voiced = sum(max(0.0, min(e, t + window) - max(s, t)) for s, e in runs)
        if voiced >= MIN_VOICED:
            spans.append((round(t, 3), round(t + window, 3)))
        t += window
    if len(spans) > max_windows:
        step = len(spans) / max_windows
        spans = [spans[int(i * step)] for i in range(max_windows)]
    return spans


def cluster(emb, *, same: float = SAME_VOICE, max_voices: int = MAX_VOICES, min_group: int = MIN_GROUP, attach: float = ATTACH) -> list[int]:
    """A voice number per row of ``emb`` (rows are unit vectors). Voice 0 is the one with the most windows; -1 means "no clear voice"."""
    import numpy as np
    n = len(emb)
    if n == 0:
        return []
    sim = np.asarray(emb) @ np.asarray(emb).T
    groups = [[i] for i in range(n)]
    while len(groups) > 1:
        best = (-2.0, 0, 0)
        for a in range(len(groups)):
            for b in range(a + 1, len(groups)):
                s = float(sim[np.ix_(groups[a], groups[b])].mean())
                if s > best[0]:
                    best = (s, a, b)
        if best[0] < same and len(groups) <= max_voices:
            break
        _, a, b = best
        groups[a] += groups[b]
        del groups[b]
    groups.sort(key=len, reverse=True)
    big = [g for g in groups if len(g) >= min_group] or groups[:1]
    labels = [-1] * n
    for v, g in enumerate(big):
        for i in g:
            labels[i] = v
    for g in groups:
        if g in big:
            continue
        sims = [float(sim[np.ix_(g, b)].mean()) for b in big]
        k = max(range(len(big)), key=lambda j: sims[j])
        if sims[k] >= attach:
            for i in g:
                labels[i] = k
    return labels


def turns_from(spans: list[tuple[float, float]], labels: list[int], db, total: float, *, max_gap: float = 1.0) -> list[Turn]:
    """Consecutive windows of one voice become a turn; a turn boundary moves to the quietest moment between the two windows."""
    runs: list[list] = []
    for (s, e), v in zip(spans, labels):
        if v < 0:
            continue
        if runs and runs[-1][2] == v and s - runs[-1][1] <= max_gap:
            runs[-1][1] = e
        else:
            runs.append([s, e, v])
    for a, b in zip(runs, runs[1:]):                                          # a hand-over happens in a pause: put the boundary there
        lo, hi = a[1] - 0.5 * WINDOW, min(b[0] + 0.5 * WINDOW, b[1])
        if hi - lo > 0.2 and b[0] - a[1] <= max_gap:
            i0, i1 = max(0, int(lo / HOP)), min(len(db) - 1, int(hi / HOP))
            if i1 > i0:
                import numpy as np
                cut = (i0 + int(np.argmin(db[i0:i1 + 1]))) * HOP
                a[1], b[0] = min(max(cut, a[0] + 0.1), a[1] + 0.5 * WINDOW), max(min(cut, b[1] - 0.1), b[0] - 0.5 * WINDOW)
    return [Turn(round(max(0.0, s), 3), round(min(total, e), 3), v) for s, e, v in runs if e > s]


def diarize(audio, embedder: Embedder, *, say: Callable[[str], None] = lambda _m: None, check: Callable[[], None] = lambda: None,
            same: float = SAME_VOICE) -> list[Turn]:
    """The voice turns of ``audio`` (16 kHz mono float). Empty when there is too little speech to tell voices apart."""
    import numpy as np
    total = len(audio) / SAMPLE_RATE
    db = energy_db(audio)
    thr = speech_threshold(db)
    spans = voiced_windows(db, thr, total)
    if len(spans) < 2 * MIN_GROUP:
        return []
    say(f"listening for who speaks ({len(spans)} stretches of voice)")
    embs = []
    quarter = 0
    for i in range(0, len(spans), 32):
        check()
        chunk = [audio[int(s * SAMPLE_RATE):int(e * SAMPLE_RATE)] for s, e in spans[i:i + 32]]
        embs.append(np.asarray(embedder.embed(chunk)))
        done = min(len(spans), i + 32)
        if done * 4 // len(spans) > quarter and done < len(spans):                       # a line at each quarter: a long wait is not a silent one
            quarter = done * 4 // len(spans)
            say(f"listening for who speaks: {done} of {len(spans)} stretches")
    emb = np.concatenate(embs)
    labels = cluster(emb, same=same)
    return turns_from(spans, labels, db, total)
