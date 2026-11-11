"""Load a word-timed transcript and split it into sentences.

Accepted JSON shapes:
  * a list of {"w"|"word"|"text", "s"|"start", "e"|"end"}
  * {"language": "mr", "words": [...]}
  * {"language": "mr", "segments": [{"words": [{"word","start","end"}, ...]}]}   (Whisper / OpenShorts)
Sentence breaks: terminal punctuation (incl. Devanagari danda), a pause, or a word cap
(Whisper often emits Indic speech with no punctuation at all).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from ..captions import SENTENCE_END
from ..edl import Word

SENTENCE_PAUSE = 0.8      # seconds of silence that ends a sentence
SENTENCE_MAX_WORDS = 40   # run-on speech with no punctuation is cut here


class TranscriptError(ValueError):
    pass


@dataclass
class Sentence:
    id: int
    text: str
    start: float
    end: float
    first: int            # index of its first word in Transcript.words
    last: int             # index of its last word (inclusive)


@dataclass
class Transcript:
    words: list[Word]
    language: str
    sentences: list[Sentence]
    timing: str = "word"      # "word": real word timestamps; "cue": interpolated inside subtitle cues

    @property
    def duration(self) -> float:
        return self.words[-1].e if self.words else 0.0


def _word(d: dict) -> Word:
    text = d.get("w", d.get("word", d.get("text")))
    s = d.get("s", d.get("start"))
    e = d.get("e", d.get("end"))
    if text is None or s is None or e is None:
        raise TranscriptError(f"word without text/start/end: {d!r}")
    return Word(w=str(text).strip(), s=float(s), e=max(float(e), float(s)),
                conf=d.get("conf", d.get("probability")))


def parse_transcript(data, language: str | None = None) -> Transcript:
    lang = language
    if isinstance(data, dict):
        lang = lang or data.get("language")
        if "words" in data:
            raw = data["words"]
        elif "segments" in data:
            raw = [w for seg in data["segments"] for w in seg.get("words", [])]
        else:
            raise TranscriptError("transcript needs 'words' or 'segments'")
    else:
        raw = data
    words = [w for w in (_word(d) for d in raw) if w.w]
    if not words:
        raise TranscriptError("transcript has no words")
    words.sort(key=lambda w: (w.s, w.e))
    return Transcript(words, lang or "en", split_sentences(words))


_TIME = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")


def _secs(m) -> float:
    h, mi, s, ms = m
    return int(h or 0) * 3600 + int(mi) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_cues(text: str, language: str | None = None) -> Transcript:
    """SRT or WebVTT. Subtitle files only time whole cues, so each cue's words get times spread over the
    cue in proportion to their length. Good enough to choose and cut clips; caption highlighting is
    approximate (``Transcript.timing == "cue"``)."""
    text = text.lstrip("\ufeff")
    words: list[dict] = []
    for block in re.split(r"\r?\n\s*\r?\n", text.strip()):
        lines = [l for l in block.splitlines() if l.strip()]
        i = next((k for k, l in enumerate(lines) if "-->" in l), None)
        if i is None:
            continue
        a, _, b = lines[i].partition("-->")
        ma, mb = _TIME.search(a), _TIME.search(b)
        if not (ma and mb):
            continue
        t0, t1 = _secs(ma.groups()), _secs(mb.groups())
        body = re.sub(r"<[^>]+>|\{[^}]*\}", "", " ".join(lines[i + 1:]))      # drop <i>, <c.x>, {\an8}
        toks = body.split()
        if not toks or t1 <= t0:
            continue
        weights = [len(t) + 1 for t in toks]
        total, acc = sum(weights), 0.0
        for tok, w in zip(toks, weights):
            words.append({"w": tok, "s": t0 + (t1 - t0) * acc / total, "e": t0 + (t1 - t0) * (acc + w) / total})
            acc += w
    if not words:
        raise TranscriptError("no cues found in the subtitle file")
    tr = parse_transcript({"language": language, "words": words}, language)
    tr.timing = "cue"
    return tr


def load_transcript(path: str | Path, language: str | None = None) -> Transcript:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".srt", ".vtt") or text.lstrip("\ufeff").startswith("WEBVTT"):
        return parse_cues(text, language)
    return parse_transcript(json.loads(text), language)


def split_sentences(words: list[Word]) -> list[Sentence]:
    out: list[Sentence] = []
    start = 0
    for i, w in enumerate(words):
        nxt = words[i + 1] if i + 1 < len(words) else None
        n_words = i - start + 1
        end_here = (nxt is None or w.w.endswith(SENTENCE_END) or n_words >= SENTENCE_MAX_WORDS
                    or nxt.s - w.e >= SENTENCE_PAUSE)
        if end_here:
            chunk = words[start:i + 1]
            out.append(Sentence(len(out), " ".join(x.w for x in chunk), chunk[0].s, chunk[-1].e, start, i))
            start = i + 1
    return out
