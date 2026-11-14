"""Loudness of 16 kHz mono audio per 10 ms, and where it is voiced. Shared by the cleanup (what is silence) and the recogniser (where speech went
unrecognised)."""
from __future__ import annotations

HOP = 0.01                       # seconds per energy frame
SAMPLE_RATE = 16000


def energy_db(audio):
    """Loudness per 10 ms of 16 kHz mono float samples, in dB."""
    import numpy as np
    h = int(SAMPLE_RATE * HOP)
    n = len(audio) // h
    fr = np.asarray(audio[: n * h], dtype=np.float32).reshape(n, h)
    return 20 * np.log10(np.sqrt((fr ** 2).mean(1)) + 1e-9)


def speech_threshold(db) -> float:
    import numpy as np
    floor, peak = np.percentile(db, 10), np.percentile(db, 95)
    return float(floor + max(6.0, min(18.0, (peak - floor) * 0.35)))


def voiced_runs(db, thr: float, t0: float, t1: float, min_gap: float = 0.06) -> list[tuple[float, float]]:
    """Stretches of ``[t0, t1)`` louder than ``thr``; runs closer than ``min_gap`` seconds are one."""
    a, b = max(0, int(t0 / HOP)), min(len(db), int(t1 / HOP))
    runs: list[list[float]] = []
    cur = None
    for k in range(a, b):
        if db[k] > thr:
            if cur and k * HOP - cur[1] <= min_gap:
                cur[1] = (k + 1) * HOP
            else:
                if cur:
                    runs.append(cur)
                cur = [k * HOP, (k + 1) * HOP]
    if cur:
        runs.append(cur)
    return [(s, e) for s, e in runs]
