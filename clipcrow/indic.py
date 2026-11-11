"""Indic-aware Whisper decoding: language voting and native-script seed prompts.

Whisper identifies closely related languages (Hindi / Marathi / Urdu / Nepali) unreliably from a
single 30 s window, and without a prior it drifts into the wrong script (Hindi in Arabic letters,
Marathi in Latin). Two fixes, both from the OpenShorts fork this project grew out of:

  * vote on the language over up to three windows of the media, and
  * pass a native-script ``initial_prompt`` so the decoder stays in the right Unicode block.

``decode_params(model, media)`` returns the extra faster-whisper kwargs; any failure returns ``{}``
so the caller falls back to Whisper's own detection.
"""
from __future__ import annotations

import subprocess

INDIC_LANGS = {"hi", "mr", "ne", "sa", "ur"}

# Longer prompts anchor the decoder better than single words (Hindi vs Urdu needs real context).
SCRIPT_SEED = {
    "hi": "नमस्ते। यह वीडियो हिंदी भाषा में है। आज हम हिंदी में बात करेंगे।",
    "mr": "नमस्कार। हे व्हिडिओ मराठी भाषेत आहे। आज आपण मराठीत बोलणार आहोत।",
    "ne": "नमस्ते। यो भिडियो नेपाली भाषामा छ।",
    "sa": "नमस्ते। एतत् संस्कृतभाषायां वीडियो अस्ति।",
    "ur": "السلام علیکم۔ یہ ویڈیو اردو زبان میں ہے۔",
    "ta": "வணக்கம். இந்த வீடியோ தமிழ் மொழியில் உள்ளது.",
    "te": "నమస్కారం. ఈ వీడియో తెలుగు భాషలో ఉంది.",
    "kn": "ನಮಸ್ಕಾರ. ಈ ವೀಡಿಯೋ ಕನ್ನಡ ಭಾಷೆಯಲ್ಲಿದೆ.",
    "ml": "നമസ്കാരം. ഈ വീഡിയോ മലയാളത്തിലാണ്.",
}
WINDOW = 30  # seconds per detection window


def media_duration(path: str) -> float | None:
    r = subprocess.run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def audio_window(path: str, offset: float, seconds: float = WINDOW):
    """16 kHz mono float32 samples of a window of the media."""
    import numpy as np
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(offset), "-i", str(path), "-t", str(seconds),
                        "-ar", "16000", "-ac", "1", "-f", "f32le", "pipe:1"],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return np.frombuffer(r.stdout, dtype=np.float32)


def _parse(detected) -> tuple[str, float] | None:
    """faster-whisper's detect_language() answer differs between versions: normalise it."""
    if isinstance(detected, tuple) and detected:
        return str(detected[0]), float(detected[1]) if len(detected) > 1 else 0.0
    if isinstance(detected, list) and detected:
        head = detected[0]
        if isinstance(head, (list, tuple)) and len(head) >= 2:
            return str(head[0]), float(head[1])
        return str(head), 0.0
    return None


def vote_language(model, media: str, *, duration=None, window=None):
    """Majority vote over up to three windows; ties go to the more confident language."""
    duration = duration or media_duration        # looked up at call time so tests can replace them
    window = window or audio_window
    total = duration(media) or 60
    offsets = [0]
    if total > 90:
        offsets.append(int(total / 3))
    if total > 180:
        offsets.append(int(2 * total / 3))
    votes: dict[str, int] = {}
    best: dict[str, float] = {}
    for off in offsets:
        samples = window(media, off)
        if len(samples) < 4000:          # under 0.25 s of audio: skip an empty window
            continue
        got = _parse(model.detect_language(samples))
        if got is None:
            continue
        lang, prob = got
        votes[lang] = votes.get(lang, 0) + 1
        best[lang] = max(best.get(lang, 0.0), prob)
    if not votes:
        return None
    lang = max(votes, key=lambda k: (votes[k], best[k]))
    return lang, best[lang]


def decode_params(model, media: str, *, vote=None) -> dict:
    """``{"language", "initial_prompt"}`` for Indic audio, ``{"language"}`` when confident, else ``{}``."""
    try:
        got = (vote or vote_language)(model, media)
    except Exception:
        return {}
    if got is None:
        return {}
    lang, prob = got
    seed = SCRIPT_SEED.get(lang)
    if seed is None and prob < 0.6:      # unsure about a non-Indic language: let Whisper decide
        return {}
    return {"language": lang, **({"initial_prompt": seed} if seed else {})}
