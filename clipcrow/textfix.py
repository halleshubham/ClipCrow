"""A correction pass over Marathi / Hindi speech-to-text output.

Whisper's Devanagari text is "Hindi-flavoured and wrong in details" even when it hears the right language (``नहीं`` for ``नाही``, ``हुता`` for
``होता``). A person reads the garbled words and writes what was said; this module does that with one model call per batch of sentences.

What is trusted and what is not:
  * the model sees only sentence ids and text, and returns only text per id (a closed schema; nothing is executed or interpolated);
  * word TIMES never come from the model. Same word count: each word keeps its time. Different count: the words are spread over the
    sentence's own span in proportion to their length;
  * an answer is used only if it still looks like the same sentence (length, Devanagari share); otherwise the original words stay;
  * any failure keeps the raw transcript, which the caller saves next to the corrected one.
"""
from __future__ import annotations

from typing import Callable

from pydantic import BaseModel

from .planner.transcript import Transcript, parse_transcript

REFINE_LANGS = {"mr", "hi"}
BATCH = 25                       # sentences per call: small enough to answer in full, large enough to share context
MIN_RATIO, MAX_RATIO = 0.5, 1.8  # corrected length / original length (characters, spaces ignored)
MIN_DEVANAGARI = 0.5             # the share of Devanagari letters among letters must not fall below half the original's

SYSTEM = """You correct the speech-to-text output of a {language} video. The text was produced by Whisper, which hears the sounds roughly right but
writes Hindi-flavoured, garbled words (for example नहीं instead of नाही, हुता instead of होता, a wrong ending, a wrong word that sounds alike).

For every numbered sentence, return what the speaker most likely said, in natural spoken {language}, written in Devanagari.
Rules:
- Keep the meaning and the order. Do not add, remove, summarise, translate or comment. One sentence in, one sentence out, same id.
- Fix Hindi spellings and Hindi words that {language} does not use ("है", "नहीं", "क्या") into the {language} the speaker would say.
- Keep English words the speaker uses (GPS, IIT, bank), written as they are, and keep numbers as spoken.
- Names and numbers you cannot tell: leave the words as they are rather than guess.
- If a sentence is too garbled to reconstruct, return it unchanged.
- No quotation marks or explanations around a sentence; plain text only."""

LANGUAGE_NAME = {"mr": "Marathi", "hi": "Hindi"}


class Fixed(BaseModel):
    id: int
    text: str


class Fixes(BaseModel):
    items: list[Fixed]


def _letters(text: str) -> list[str]:
    return [c for c in text if c.isalpha()]


def _devanagari_share(text: str) -> float:
    letters = _letters(text)
    return sum("ऀ" <= c <= "ॿ" for c in letters) / len(letters) if letters else 0.0


def acceptable(original: str, fixed: str) -> bool:
    """Is ``fixed`` still plausibly the same sentence? (The model's answer is untrusted.)"""
    fixed = fixed.strip()
    if not fixed or "\n" in fixed or len(fixed) > 4000:
        return False
    a, b = len(original.replace(" ", "")), len(fixed.replace(" ", ""))
    if a == 0 or not (MIN_RATIO <= b / a <= MAX_RATIO):
        return False
    return _devanagari_share(fixed) >= MIN_DEVANAGARI * _devanagari_share(original)


def _retime(words: list, new_tokens: list[str]) -> list[dict]:
    """New words over the sentence's span: 1:1 keeps every time; otherwise split the span in proportion to word length."""
    if len(new_tokens) == len(words):
        return [{"w": t, "s": w.s, "e": w.e} for t, w in zip(new_tokens, words)]
    t0, t1 = words[0].s, words[-1].e
    weights = [len(t) + 1 for t in new_tokens]
    total, acc, out = sum(weights), 0, []
    for t, wt in zip(new_tokens, weights):
        out.append({"w": t, "s": t0 + (t1 - t0) * acc / total, "e": t0 + (t1 - t0) * (acc + wt) / total})
        acc += wt
    return out


def refine(tr: Transcript, llm, *, say: Callable[[str], None] = lambda _m: None) -> tuple[Transcript, int]:
    """The corrected transcript and how many sentences changed. Batches that fail keep their original words."""
    name = LANGUAGE_NAME.get(tr.language)
    if name is None or not tr.sentences:
        return tr, 0
    system = SYSTEM.format(language=name)
    fixed: dict[int, str] = {}
    for i in range(0, len(tr.sentences), BATCH):
        batch = tr.sentences[i:i + BATCH]
        user = "\n".join(f"{s.id}: {s.text}" for s in batch)
        try:
            got = llm.generate(system, user, Fixes, purpose="text-refine")
        except Exception as e:                                     # a refusal, a rate limit, a timeout: the raw words are still good
            say(f"text correction skipped for sentences {batch[0].id}-{batch[-1].id}: {type(e).__name__}")
            continue
        ids = {s.id for s in batch}
        for item in got.items:
            if item.id in ids and item.id not in fixed:
                fixed[item.id] = item.text
    words: list[dict] = []
    changed = 0
    for s in tr.sentences:
        old = tr.words[s.first:s.last + 1]
        new = fixed.get(s.id)
        if new is not None and acceptable(s.text, new) and new.split() != [w.w for w in old]:
            words.extend(_retime(old, new.split()))
            changed += 1
        else:
            words.extend({"w": w.w, "s": w.s, "e": w.e} for w in old)
    if not changed:
        return tr, 0
    out = parse_transcript({"language": tr.language, "words": words})
    out.timing = tr.timing
    return out, changed
