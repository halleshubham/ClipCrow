"""Indic-aware Whisper decoding: language voting and native-script seed prompts.

Whisper identifies closely related languages (Hindi / Marathi / Urdu / Nepali) unreliably from a
single 30 s window, and without a prior it drifts into the wrong script (Hindi in Arabic letters,
Marathi in Latin). Two fixes, both from the OpenShorts fork this project grew out of:

  * vote on the language over up to three windows of the media, and
  * pass a native-script prompt so the decoder stays in the right Unicode block AND the right language.

Devanagari speech (``asr.WINDOWED_LANGS``) no longer uses the seed at all: a prompt or hotword list is read back as the transcript, and a pinned language
in independent 15 s windows is what keeps Marathi Marathi. The seed still serves the other scripts (Urdu), where ``initial_prompt`` keeps the letters in the
right Unicode block; ``hotwords`` carries it into every window there, because ``condition_on_previous_text=False`` applies ``initial_prompt`` to the first only.

``decode_params(model, media)`` returns the extra faster-whisper kwargs; any failure returns ``{}``
so the caller falls back to Whisper's own detection.
"""
from __future__ import annotations

import inspect
import subprocess

INDIC_LANGS = {"hi", "mr", "ne", "sa", "ur"}

# Longer prompts anchor the decoder better than single words (Hindi vs Urdu needs real context).
SCRIPT_SEED = {
    "hi": "नमस्ते। यह वीडियो हिंदी भाषा में है। आज हम हिंदी में बात करेंगे।",
    "mr": "नमस्कार। हे व्हिडिओ मराठी भाषेत आहे। आज आपण मराठीत बोलणार आहोत। तुम्ही काय म्हणता? आम्ही सांगतो, ते अजून झाले नाही।",
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


# Words that settle Marathi against Hindi once there is text. Only words that belong to ONE of the two (तो, की, जो, का, होता are both) and that every few sentences use.
MARATHI_MARKERS = frozenset("""आहे आहेत आहोत आहात नाही नाहीत नाहीये नाहीयेत आणि काय म्हणजे म्हणून आपण आम्ही तुम्ही मी पण तर होतं होतो होती झालं झाला झाली केलं केला
केली असतं असते नसतं मध्ये त्यांनी त्यांच्या आपल्या माझं माझी तुमचं आमचं म्हणतात म्हणाले करतात करतो करते होऊ ज्या ज्याला कुणी कोणी असं तसं एक ही हे हा""".split())
HINDI_MARKERS = frozenset("""है हैं हूँ हो नहीं और क्या में था थे थी लेकिन यह वह हम मैं आप कि से को के रहा रही रहे करना कर होगा गया गई गए हुआ हुई हुए सकता सकती चाहिए
बहुत अपने उनके इसके इसलिए जिसे तुम उसे उन्हें इन्हें कुछ सब""".split())
for _both in ("हो", "के", "को", "कर", "एक", "ही", "हे", "हा", "से", "सब"):               # a marker must not be a word the other language also uses
    MARATHI_MARKERS = MARATHI_MARKERS - {_both}
    HINDI_MARKERS = HINDI_MARKERS - {_both}
MARKER_MIN = 8             # at least this many marker words, and ...
MARKER_RATIO = 3.0         # ... this many times more of one language's than of the other's, before text overrules a label


def language_from_text(text: str) -> str | None:
    """"mr" or "hi" when the words themselves are clearly one of the two (Whisper's guess from sound alone often is not), else None."""
    import re
    tokens = re.findall(r"[\u0900-\u097F]+", text)
    mr = sum(1 for t in tokens if t in MARATHI_MARKERS)
    hi = sum(1 for t in tokens if t in HINDI_MARKERS)
    if mr >= MARKER_MIN and mr >= MARKER_RATIO * max(hi, 1):
        return "mr"
    if hi >= MARKER_MIN and hi >= MARKER_RATIO * max(mr, 1):
        return "hi"
    return None


def settle_hi_mr(model, media: str, lang: str, *, duration=None, window=None) -> str:
    """Whisper tells Hindi from Marathi by sound, a few windows at a time, and often says Hindi for Marathi. Decode up to three windows in the language it chose and
    read the WORDS: when they are clearly the other one, that is the language. Any failure leaves ``lang`` as it was."""
    if lang not in ("hi", "mr"):
        return lang
    try:
        duration = duration or media_duration
        window = window or audio_window
        total = duration(media) or 60
        offsets = [0] + ([int(total / 3)] if total > 90 else []) + ([int(2 * total / 3)] if total > 180 else [])
        text = []
        for off in offsets:
            samples = window(media, off)
            if len(samples) < 4000:
                continue
            segments, _ = model.transcribe(samples, language=lang, beam_size=1, condition_on_previous_text=False, word_timestamps=False)
            text.append(" ".join(getattr(s, "text", "") for s in segments))
        return language_from_text(" ".join(text)) or lang
    except Exception:
        return lang


def _takes(model, name: str) -> bool:
    """Does ``model.transcribe`` accept the keyword ``name``? (A faster-whisper older than the option, or a test double, may not.)"""
    try:
        params = inspect.signature(model.transcribe).parameters
    except (TypeError, ValueError, AttributeError):
        return False
    return name in params or any(p.kind is p.VAR_KEYWORD for p in params.values())


def prompt_params(model, lang: str) -> dict:
    """The decoder anchors for ``lang``: the seed as ``initial_prompt`` (first window) and, where the installed faster-whisper has
    it, as ``hotwords`` (every window). ``{}`` for a language with no seed."""
    seed = SCRIPT_SEED.get(lang)
    if not seed:
        return {}
    out = {"initial_prompt": seed}
    if _takes(model, "hotwords"):
        out["hotwords"] = seed
    return out


def decode_params(model, media: str, *, vote=None) -> dict:
    """``{"language", "initial_prompt"[, "hotwords"]}`` for Indic audio, ``{"language"}`` when confident, else ``{}``."""
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
    return {"language": lang, **prompt_params(model, lang)}
