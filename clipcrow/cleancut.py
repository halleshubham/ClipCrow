"""Fumble-free cut of raw talking footage: cut hesitation sounds, stutters, false starts, abandoned retakes and long dead air, keep A/V in exact sync.

Adapted from the ``fumble-free-cut`` skill (Hindi / Marathi / English). What is the same: the detection rules (hesitation sounds, repeated n-grams with a
guard for Hindi/Marathi reduplication, false starts, retake lines, voice in a gap that has no word), the pace table, quiet-point snapping with short fades,
the frame-exact render. What differs, on purpose:

  * There is no person to review the proposals, so only what the skill APPLIES by default is applied; its "suggested" cuts (a doubled word that may be grammar,
    a prefix that may be two real words) are left alone.
  * The skill's editorial pass (which take is the good one, is that line a restart or chatter) is ONE model call per batch of sentences that returns only line
    ids to drop. The model sees text and ids, never times; cut points are code. A bad answer can only drop whole lines inside the caps below.
  * Safety: if the plan would remove more than ``MAX_REMOVED`` of the recording, nothing is cut and the original is used.
  * The result is a new source video (``clean.mp4``) and a transcript remapped onto it, so planning, framing and rendering work on it unchanged.

Everything here is untrusted-input safe by shape: word indices come from our own detection; the model's answer is a list of integers checked against the
sentences it was shown.
"""
from __future__ import annotations

import difflib
import json
import math
import re
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

from .edl import Word
from .energy import HOP, energy_db, speech_threshold, voiced_runs  # noqa: F401  (re-exported: the tests and callers use them from here)
from .planner.transcript import Transcript, parse_transcript

PACES = {"natural": dict(max_pause=0.60, keep_pause=0.40),     # pauses up to max_pause stay; longer ones shrink to keep_pause
         "fast": dict(max_pause=0.40, keep_pause=0.28),
         "tight": dict(max_pause=0.25, keep_pause=0.18)}
MAX_REMOVED = 0.45               # more than this share of the recording cut = the detection is wrong: leave the video alone
MIN_WORDS = 20                   # too little speech to judge
MAX_MINUTES = 90                 # the audio is held in memory while it is cut
JUDGE_BATCH = 80                 # sentences per model call
JUDGE_MAX_SHARE = 0.5            # the model may drop at most this share of a batch (a whole first take can be almost half of it)
MISSED_RUN = 0.6                 # voiced audio this long with no recognised word is speech the recogniser missed: it is kept, never cut as "dead air"
RESTART_PAUSE = 0.9              # seconds of silence after which a speaker may start a passage again
LOOKBACK_WORDS = 70              # how far back the earlier try of a restarted passage may begin
PASSAGE_MIN_WORDS = 8            # a passage (what is said between two long pauses) shorter than this is not compared
PASSAGE_SHARE = 0.5              # a later passage that contains this much of an earlier one's content words (or shares this much of the two) redoes it
PASSAGE_AHEAD = 3                # ... and starts within this many passages of it
SENTENCE_MIN = 8                 # a sentence is compared with the next passage's sentences when it has this many content words
SENTENCE_SHARE = 0.45            # ... and the next passage has one that shares this much of the two (reworded, not word for word)
REDO_PASSAGES = 2                # the redo is the next passage, or the one after an aborted attempt
LEAD_IN, TAIL, FADE_MS, MIN_KEEP = 0.25, 0.6, 8, 0.12
SAME_WORD = 0.85                 # two spellings this alike count as the same word
PHRASE_WINDOW = 10               # a phrase restated within this many words is a false start
RESTATED = 0.78                  # share of two lines that is the same words, in order, for the earlier one to be an abandoned take
CONTAINED = 0.80                 # ... or this much of the EARLIER line is found again in the later one (restated with additions)
PARALLEL = (0.60, 0.90)          # three lines in a row this alike (but not near-identical) are a list said on purpose ("आज आपण X बद्दल ... आज आपण Y बद्दल ...")
REDO_WITHIN = 8.0                # seconds: a redo starts soon after the abandoned try ended; a similar line much later is a callback, not a retake
MIN_RETAKE_WORDS = 4             # shorter lines repeat for emphasis ("आम्ही लढू। आम्ही जिंकू।"), not as retakes
RECAP_FLOOR = 0.5                # a later line shorter than this share of the earlier one is a recap, not a redo

EMPHATIC = {"very", "really", "no", "bye", "ha", "yeah", "yes", "okay", "go", "more", "much", "many", "far", "long", "over", "again", "round",
            "knock", "bang", "dot", "tick", "la", "na"}
FILLER_RE = re.compile(r"(u+h+m*|u+m+|e+r+m*|e+h+|a+h+|h+m+|m+h*m+|uhm+)")
# Hesitation sounds in Devanagari, matched after the virama is dropped. "हम" (we) and "आ" (come) are real words and are NOT here.
DEV_FILLER_RE = re.compile(r"(अ+[ंँ]*म*|अ+ह+|उ+[ंँ]*म*|ऊ+[ंँ]*म*|ह[ंँ]+|हम{2,}|म{2,}|ए+ह+|आ+[ंँ]+|ऍ+)")
DEV_FILLER_SET = {"ह्म", "ह्म्म", "ह्मम", "अऽ", "हंम"}
SENT_END = re.compile(r"[.?!।॥|]$")
INDIC = {"hi", "mr"}


def is_dev(s: str) -> bool:
    return any("ऀ" <= c <= "ॿ" for c in s)


def norm(w: str) -> str:
    """Lowercase, drop punctuation and symbols but KEEP letters, matras, anusvara and virama (Devanagari)."""
    w = unicodedata.normalize("NFC", w.lower())
    w = "".join(c for c in w if c == "'" or unicodedata.category(c)[0] in "LMN")
    return re.sub(r"(.)\1{2,}", r"\1\1", w)


def is_filler(w: str) -> bool:
    n = norm(w)
    if not n:
        return False
    if is_dev(n):
        return n in DEV_FILLER_SET or DEV_FILLER_RE.fullmatch(n.replace("्", "")) is not None
    squeezed = re.sub(r"(.)\1+", r"\1", n)
    return FILLER_RE.fullmatch(squeezed) is not None or n in {"mm", "mmm"}


@dataclass
class Cut:
    kind: str                          # filler | stutter | false_start | retake | hidden_filler | judged
    reason: str
    first: int | None = None           # word indices, inclusive
    last: int | None = None
    t0: float | None = None            # or a time range (hidden fillers)
    t1: float | None = None
    text: str = ""
    apply: bool = True


# --- audio energy -----------------------------------------------------------------------------

def refine_edges(words: list[Word], db, thr: float) -> list[tuple[float, float]]:
    """Tighten a recogniser's sloppy word edges against the audio: extend a start back (<= 120 ms) and an end forward (<= 200 ms) while it is still voiced."""
    out = []
    for i, w in enumerate(words):
        lo = words[i - 1].e if i else 0.0
        hi = words[i + 1].s if i + 1 < len(words) else len(db) * HOP
        s, e = w.s, w.e
        k = min(int(s / HOP), len(db))
        while k > 0 and (s - HOP) > lo and w.s - s < 0.12 and db[k - 1] > thr:
            s -= HOP
            k -= 1
        k = int(e / HOP)
        while k < len(db) and (e + HOP) < hi and e - w.e < 0.20 and db[k] > thr:
            e += HOP
            k += 1
        out.append((round(s, 3), round(e, 3)))
    return out


# --- detection ---------------------------------------------------------------------------------

def same_word(a: str, b: str) -> bool:
    """Equal, or spelled so alike that a recogniser could have written either (नाही / नहीं-style drift, an ending that changed)."""
    return a == b or (len(a) >= 3 and len(b) >= 3 and difflib.SequenceMatcher(None, a, b).ratio() >= SAME_WORD)


def overlap(a: list[str], b: list[str]) -> tuple[int, float]:
    """(words matched in order, share of the two lists they cover) with alike-spelled words counting as equal."""
    canon: list[str] = []

    def key(t: str) -> str:
        for c in canon:
            if same_word(c, t):
                return c
        canon.append(t)
        return t
    ka, kb = [key(t) for t in a], [key(t) for t in b]
    hit = sum(m.size for m in difflib.SequenceMatcher(None, ka, kb, autojunk=False).get_matching_blocks())
    return hit, 2 * hit / max(1, len(a) + len(b))


def lines_of(words: list[Word]) -> list[tuple[int, int]]:
    """Sentence-like lines: end punctuation, a pause over 0.8 s, or 28 words."""
    out, start = [], 0
    for i, w in enumerate(words):
        gap = words[i + 1].s - w.e if i + 1 < len(words) else 9
        if SENT_END.search(w.w) or gap > 0.8 or i - start + 1 >= 28:
            out.append((start, i))
            start = i + 1
    if start < len(words):
        out.append((start, len(words) - 1))
    return out


def detect(words: list[Word], lang: str, *, db=None, thr: float | None = None) -> list[Cut]:
    """The cuts the skill applies by default. Suggestions that need a person's eye (apply=False) are returned too, so a report can show them."""
    cuts: list[Cut] = []
    N = len(words)
    nw = [norm(w.w) for w in words]
    indic = lang in INDIC
    done: set[int] = set()
    for i, w in enumerate(words):                                            # 1 hesitation sounds
        if is_filler(w.w):
            cuts.append(Cut("filler", "hesitation sound", i, i, text=w.w))
            done.add(i)
    for n in (3, 2, 1):                                                      # 2 repeats: "the the", "I was I was", "तो तो"
        for i in range(N - 2 * n + 1):
            a, b = nw[i:i + n], nw[i + n:i + 2 * n]
            if not all(a) or a != b or any(k in done for k in range(i, i + n)):
                continue
            if n == 1 and a[0] in EMPHATIC:
                continue
            gap = words[i + n].s - words[i + n - 1].e
            if gap > 1.0:
                continue
            redup = indic and n == 1 and gap < 0.15 and not words[i].w.endswith((",", "-", "…"))     # धीरे धीरे, हळू हळू: grammar, not a stutter
            cuts.append(Cut("stutter", f"repeated {n}-gram" + (" (maybe reduplication)" if redup else ""), i, i + n - 1,
                            text=" ".join(w.w for w in words[i:i + 2 * n]), apply=not redup))
            if not redup:
                done.update(range(i, i + n))
    for i in range(N - 1):                                                   # 2b a partial word the speaker marked off with a dash: "प- प्रोडक्ट"
        x, y = nw[i], nw[i + 1]
        if not x or i in done or x == y:
            continue
        marked = words[i].w.endswith(("-", "—", "–"))
        if (marked or (len(x) >= 2 and len(y) >= len(x) + 2)) and y.startswith(x) and words[i + 1].s - words[i].e < 0.6:
            cuts.append(Cut("stutter", "partial word before the full word" + ("" if marked else " (could be two real words)"), i, i,
                            text=f"{words[i].w} {words[i + 1].w}", apply=marked))
            if marked:
                done.add(i)
    lines = lines_of(words)
    protected: set[int] = set()                                              # words of a run of parallel lines: never "restarts"
    toks = [[t for t in nw[a:b + 1] if t and not is_filler(t)] for a, b in lines]
    for x in range(len(lines) - 2):
        if all(len(toks[k]) >= MIN_RETAKE_WORDS for k in (x, x + 1, x + 2)) and \
                all(PARALLEL[0] <= overlap(toks[k], toks[k + 1])[1] < PARALLEL[1] for k in (x, x + 1)):
            protected.update(range(lines[x][0], lines[x + 2][1] + 1))
    for i in range(N - 3):                                                   # 3 false start: a 3-6 word phrase restated (even changed) soon after drops the first try
        for n in (6, 5, 4, 3):
            pat = nw[i:i + n]
            if len(pat) < n or not all(pat) or i in protected:
                continue
            need = n if n == 3 else n - 1                                     # one changed word is allowed in a phrase of four or more
            hit = next((j for j in range(i + n, min(N - n + 1, i + n + PHRASE_WINDOW))
                        if nw[j:j + n] and all(nw[j:j + n]) and sum(same_word(x, y) for x, y in zip(pat, nw[j:j + n])) >= need
                        and not any(k in done for k in range(i, j))), None)
            if hit is not None:
                start = next((k for k in range(i + n, hit + 1) if same_word(nw[k], pat[0])), hit)      # an inserted word can shift the match: cut up to the restatement's FIRST word
                cuts.append(Cut("false_start", f"phrase restated at word {start}", i, start - 1, text=" ".join(w.w for w in words[i:hit + n])))
                done.update(range(i, start))
                break
    for x, (a, b) in enumerate(lines):                                       # 4 retake: a line that the next few lines say again (reworded, extended, respelled)
        mine = [t for t in nw[a:b + 1] if t and not is_filler(t)]
        if len(mine) < MIN_RETAKE_WORDS or a in protected:
            continue
        for c, d in lines[x + 1:x + 5]:
            later = [t for t in nw[c:d + 1] if t and not is_filler(t)]
            if len(later) < MIN_RETAKE_WORDS or len(later) < RECAP_FLOOR * len(mine):
                continue
            hit, share = overlap(mine, later)
            if (share >= RESTATED or hit / len(mine) >= CONTAINED) and not all(k in done for k in range(a, c)):
                if words[c].s - words[b].e > REDO_WITHIN:
                    break
                cuts.append(Cut("retake", f"an earlier take of the line at word {c} ({hit} of {len(mine)} words said again)", a, c - 1,
                                text=" ".join(w.w for w in words[a:c])[:200]))
                done.update(range(a, c))
                break
    for p in range(4, N - 3):                                                # 3b restart after a pause: the speaker stops, then says the passage again from its opening
        if words[p].s - words[p - 1].e < RESTART_PAUSE or p in protected or p - 1 in protected:
            continue
        after = [t for t in nw[p:p + 6] if t]
        if len(after) < 5:
            continue
        best = None
        for i in range(max(0, p - LOOKBACK_WORDS), p - 4):
            if not same_word(after[0], nw[i]):                              # a restart begins with the same words: a name that merely comes back is not one
                continue
            hit, _ = overlap(after, [t for t in nw[i:i + 9] if t])
            if hit >= 4 and not any(k in done for k in range(i, p)) and (best is None or hit >= best[0]):
                best = (hit, i)
        if best is not None and p - best[1] <= 2 * (N - p):                  # the earlier try is not longer than what follows it twice over
            i = best[1]
            cuts.append(Cut("restart", f"the passage starting at word {i} is said again after a {words[p].s - words[p - 1].e:.1f} s pause", i, p - 1,
                            text=" ".join(w.w for w in words[i:p])[:240]))
            done.update(range(i, p))
    chunks, start = [], 0                                                    # 3c a whole passage said again, reworded: the speaker stops, pauses and tries once more
    for i in range(1, N):
        if words[i].s - words[i - 1].e >= RESTART_PAUSE:
            chunks.append((start, i - 1))
            start = i
    chunks.append((start, N - 1))
    content = [[t for t in nw[a:b + 1] if len(t) >= 3 and not is_filler(t)] for a, b in chunks]
    for x, (a, b) in enumerate(chunks):
        if len(content[x]) < PASSAGE_MIN_WORDS or a in protected or all(k in done for k in range(a, b + 1)):
            continue
        redone = None
        for y in range(x + 1, min(x + 1 + PASSAGE_AHEAD, len(chunks))):
            if len(content[y]) < 0.6 * len(content[x]):                      # a short follow-up is a recap, not a redo
                continue
            hit, share = overlap(content[x], content[y])
            if share >= PASSAGE_SHARE or hit / len(content[x]) >= PASSAGE_SHARE + 0.1:
                redone = y
        if redone is None:
            continue
        end = chunks[redone - 1][1]
        between_ok = all(len(content[k]) <= 14 or overlap(content[k], content[redone])[1] >= 0.3 for k in range(x + 1, redone))
        if not between_ok:                                                   # something else was said in between: only the earlier try goes
            end = b
        cuts.append(Cut("retake", f"the passage at word {a} is said again, reworded, after a pause", a, end, text=" ".join(w.w for w in words[a:end + 1])[:240]))
        done.update(range(a, end + 1))
    line_chunk = [next(c for c, (ca, cb) in enumerate(chunks) if ca <= a <= cb) for a, _ in lines]                  # which passage each line is in
    line_content = [[t for t in nw[a:b + 1] if len(t) >= 3 and not is_filler(t)] for a, b in lines]
    for x, (a, b) in enumerate(lines):                                       # 3d a sentence the NEXT passage says again in other words: the earlier try goes from there
        if len(line_content[x]) < SENTENCE_MIN or a in protected or any(k in done for k in range(a, b + 1)):
            continue
        for y in range(x + 1, len(lines)):
            gap = line_chunk[y] - line_chunk[x]
            if gap < 1:
                continue
            if gap > REDO_PASSAGES:
                break
            if len(line_content[y]) < SENTENCE_MIN:
                continue
            _, share = overlap(line_content[x], line_content[y])
            if share >= SENTENCE_SHARE:
                end = chunks[line_chunk[y]][0] - 1                           # up to where the redo begins
                if end >= a and (end - a + 1) <= 0.45 * N:
                    cuts.append(Cut("retake", f"said again, reworded, after a pause (share {share:.2f})", a, end,
                                    text=" ".join(w.w for w in words[a:end + 1])[:240]))
                    done.update(range(a, end + 1))
                break
    if db is not None and thr is not None:                                   # 5 voice in a gap with no word: a hesitation the recogniser dropped
        for i in range(N - 1):
            g0, g1 = words[i].e + 0.05, words[i + 1].s - 0.05
            if g1 - g0 < 0.25:
                continue
            for s, e in voiced_runs(db, thr, g0, g1):
                if 0.15 <= e - s < MISSED_RUN:                       # a longer burst may be real words the recogniser missed: it is kept (see compute_keeps)
                    cuts.append(Cut("hidden_filler", f"voice between words {i} and {i + 1} with no word", t0=round(s - 0.03, 3), t1=round(e + 0.03, 3)))
    return cuts


# --- the editorial pass (a model drops whole lines) ----------------------------------------------

class Drop(BaseModel):
    id: int
    why: str = ""


class Drops(BaseModel):
    drop: list[Drop]


JUDGE_SYSTEM = """You are a video editor cleaning a raw {language} talking recording. You get numbered consecutive lines of what was said.
Return the ids of lines to REMOVE:
- an abandoned attempt the speaker says again, better. Keep the LAST complete take. The second try is often reworded, extended, shortened or starts with
  different words; if a later line says the same thing again with some changes, the EARLIER line is the one to remove.
- the restart phrase itself ("sorry, again", "one more time", "थांबा, पुन्हा सांगतो", "सॉरी, फिर से").
- a whole passage said twice: when the speaker stops, pauses ("[pause 3.3s]" before a line) and says the passage again from its opening, remove the FIRST
  attempt, all of its lines, up to the pause. A long pause before a line that repeats an earlier opening is the clearest sign.
- an unfinished sentence the speaker abandons mid-way and starts over (it ends in the middle of a thought, is followed by a pause or a new sentence, and
  nothing in it is needed). Remove only the fragment, never the sentence that starts over.
- off-camera chatter before or after the real talk ("is it recording?", "चालू आहे का?", "ठीक है, चलो").
Rules: a repeated idea said for emphasis is not a retake. Never remove the only place a fact appears. Never remove a line only because it is short.
When you are not sure, keep the line. Most lines are kept: return an empty list when nothing needs to go."""

LANGUAGE_NAME = {"mr": "Marathi", "hi": "Hindi", "en": "English"}


def judge_retakes(words: list[Word], lang: str, llm, *, say: Callable[[str], None] = lambda _m: None) -> list[Cut]:
    """Whole-line cuts the model picked. Only integers it was shown are accepted, at most ``JUDGE_MAX_SHARE`` of a batch, never the last line of the recording."""
    lines = lines_of(words)
    cuts: list[Cut] = []
    system = JUDGE_SYSTEM.format(language=LANGUAGE_NAME.get(lang, "spoken"))
    for i in range(0, len(lines), JUDGE_BATCH):
        batch = list(range(i, min(i + JUDGE_BATCH, len(lines))))
        def gap(k: int) -> str:                                               # a long pause is the clearest sign that the speaker stopped and started again
            a = lines[k][0]
            g = words[a].s - words[a - 1].e if a else 0.0
            return f"[pause {g:.1f}s] " if g >= 0.8 else ""
        user = "\n".join(f"{k}: {gap(k)}{' '.join(w.w for w in words[lines[k][0]:lines[k][1] + 1])}" for k in batch)
        try:
            got = llm.generate(system, user, Drops, purpose="retake-judge")
        except Exception as e:                                                 # a refusal, a timeout: the rule-based cuts still stand
            say(f"the editing pass was skipped for lines {batch[0]}-{batch[-1]}: {type(e).__name__}")
            continue
        picked, seen = [], set()
        for d in got.drop:
            if d.id in batch and d.id not in seen and d.id != len(lines) - 1:
                seen.add(d.id)
                picked.append(d)
        for d in picked[: max(1, int(JUDGE_MAX_SHARE * len(batch)))]:
            a, b = lines[d.id]
            cuts.append(Cut("judged", (d.why or "an abandoned take, a restart or chatter")[:100], a, b, text=" ".join(w.w for w in words[a:b + 1])[:200]))
    return cuts


# --- what to keep --------------------------------------------------------------------------------

def missed_speech(words: list[Word], cutw: set[int], db, thr: float | None) -> list[tuple[float, float]]:
    """Voiced stretches between two KEPT neighbouring words that have no word of their own (the recogniser missed speech there). Where the neighbour is a cut
    word the stretch belongs to a take that is being removed on purpose, so it is left to go with it."""
    if db is None or thr is None:
        return []
    out = []
    for i in range(len(words) - 1):
        if i in cutw or i + 1 in cutw:
            continue
        g0, g1 = words[i].e + 0.1, words[i + 1].s - 0.1
        if g1 - g0 < MISSED_RUN:
            continue
        out += [(s, e) for s, e in voiced_runs(db, thr, g0, g1, min_gap=0.4) if e - s >= MISSED_RUN]
    return out


def compute_keeps(words: list[Word], cuts: list[Cut], db, thr: float, pace: str, duration: float, fps: float) -> list[tuple[int, int]]:
    """Frame ranges ``[a, b)`` of the source to keep, on the frame grid: words not cut, with long pauses shrunk to the pace's length."""
    import numpy as np
    P = PACES[pace]
    post_k, pre_k = 0.6 * P["keep_pause"], 0.4 * P["keep_pause"]
    N = len(words)
    cutw: set[int] = set()
    tcuts: list[tuple[float, float]] = []
    for c in cuts:
        if not c.apply:
            continue
        if c.first is not None:
            cutw.update(range(c.first, c.last + 1))
        if c.t0 is not None:
            tcuts.append((c.t0, c.t1))
    kept = [i for i in range(N) if i not in cutw]
    if not kept:
        return []
    edges = refine_edges(words, db, thr) if db is not None else [(w.s, w.e) for w in words]

    def quiet(t: float, lo: float, hi: float) -> float:
        if db is None:
            return t
        a, b = max(0, int(lo / HOP)), min(len(db) - 1, int(hi / HOP))
        return t if b <= a else (a + int(np.argmin(db[a:b + 1]))) * HOP
    iv = []
    for n, i in enumerate(kept):
        rs, re_ = edges[i]
        first, last = n == 0, n == len(kept) - 1
        s = rs - (LEAD_IN if first else pre_k)
        e = re_ + (TAIL if last else post_k)
        if i > 0 and (i - 1) in cutw:
            s = max(s, edges[i - 1][1] + 0.02)
        if i + 1 < N and (i + 1) in cutw:
            e = min(e, edges[i + 1][0] - 0.02)
        if not first and s < rs:
            s = quiet(s, s, min(s + 0.06, rs))
        if not last and e > re_:
            e = quiet(e, max(e - 0.06, re_), e)
        iv.append([max(0.0, s), min(duration, max(e, re_)), i])
    merged = [iv[0][:]]
    for s, e, i in iv[1:]:
        p = merged[-1]
        contiguous = all(k not in cutw for k in range(p[2] + 1, i))
        gap = edges[i][0] - edges[p[2]][1]
        if contiguous and (gap <= P["max_pause"] or s <= p[1]):
            p[1], p[2] = max(p[1], e), i
        else:
            merged.append([s, e, i])
    keeps = [(s, e) for s, e, _ in merged]
    missed = missed_speech(words, cutw, db, thr)
    for a, b in missed:                                                      # speech with no words: never "dead air"
        keeps.append((max(0.0, a - 0.12), min(duration, b + 0.12)))
    keeps.sort()
    joined: list[list[float]] = []
    for s, e in keeps:
        if joined and s <= joined[-1][1] + 0.02:
            joined[-1][1] = max(joined[-1][1], e)
        else:
            joined.append([s, e])
    keeps = [(s, e) for s, e in joined]
    for ts, te in sorted(tcuts):                                             # subtract voiced hesitations the recogniser dropped
        nxt = []
        for s, e in keeps:
            if te <= s or ts >= e:
                nxt.append((s, e))
                continue
            if ts > s:
                nxt.append((s, ts))
            if te < e:
                nxt.append((te, e))
        keeps = nxt
    fk: list[list[int]] = []
    for s, e in keeps:                                                       # onto the frame grid; merge micro-cuts; drop slivers
        a, b = math.floor(s * fps + 1e-6), math.ceil(e * fps - 1e-6)
        if fk and a - fk[-1][1] <= 1:
            fk[-1][1] = max(fk[-1][1], b)
        else:
            fk.append([a, b])
    return [(a, b) for a, b in fk if (b - a) / fps >= MIN_KEEP]


def remap_words(words: list[Word], cutw: set[int], keeps: list[tuple[int, int]], fps: float) -> list[Word]:
    """The kept words on the cleaned timeline. A word belongs to the kept range it overlaps most."""
    segs, t = [], 0.0
    for a, b in keeps:
        segs.append((a / fps, b / fps, t))
        t += (b - a) / fps
    out: list[Word] = []
    for i, w in enumerate(words):
        if i in cutw:
            continue
        best = max(segs, key=lambda sg: min(w.e, sg[1]) - max(w.s, sg[0]), default=None)
        if best is None or min(w.e, best[1]) - max(w.s, best[0]) <= 0:
            continue
        s0, s1, o = best
        s = o + max(w.s, s0) - s0
        e = o + min(w.e, s1) - s0
        out.append(Word(w=w.w, s=round(s, 3), e=round(max(e, s), 3), conf=w.conf))
    return out


# --- rendering -----------------------------------------------------------------------------------

def probe(path: str) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        raise ValueError("could not read this video")
    j = json.loads(r.stdout)
    v = next((s for s in j["streams"] if s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    a = next((s for s in j["streams"] if s["codec_type"] == "audio"), None)
    if v is None:
        raise ValueError("there is no picture in this file")
    num, den = (v.get("avg_frame_rate") or v.get("r_frame_rate") or "30/1").split("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 30.0
    return {"fps": fps if fps > 0 else 30.0, "duration": float(j["format"].get("duration") or v.get("duration") or 0),
            "has_audio": a is not None, "channels": min(int(a.get("channels", 2)), 2) if a else 0,
            "v_start": float(v.get("start_time") or 0), "a_start": float(a.get("start_time") or 0) if a else 0.0}


def _run(cmd: list, check: Callable[[], None], label: str) -> None:
    """Run ffmpeg, noticing a cancel while it works."""
    p = subprocess.Popen([str(c) for c in cmd], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        while True:
            try:
                p.wait(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                check()
    except BaseException:
        p.kill()
        p.wait()
        raise
    if p.returncode != 0:
        raise ValueError(f"ffmpeg could not {label}: {(p.stderr.read() or '')[-300:].strip()}")


def render_cleaned(src: Path, keeps: list[tuple[int, int]], info: dict, dst: Path, *, check: Callable[[], None] = lambda: None) -> list[dict]:
    """Cut ``src`` to ``keeps`` (frames). Video through one select pass on the conformed frame rate; audio cut sample-exact from the same frame times, with
    an 8 ms fade at every join. Returns the source->output time map."""
    import numpy as np
    fps = info["fps"]
    sr = 48000
    tmap, t = [], 0.0
    for a, b in keeps:
        tmap.append({"src": [round(a / fps, 4), round(b / fps, 4)], "out": [round(t, 4), round(t + (b - a) / fps, 4)]})
        t += (b - a) / fps
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        sel = "+".join(f"between(n,{a},{b - 1})" for a, b in keeps)
        (d / "v.txt").write_text(f"[0:v:0]setpts=PTS-STARTPTS,fps={fps},select='{sel}',setpts=N/FRAME_RATE/TB[v]")
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", src]
        if info["has_audio"]:
            ch = max(1, info["channels"])
            r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-map", "0:a:0", "-vn", "-ac", str(ch), "-ar", str(sr), "-f", "s16le", "pipe:1"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if r.returncode != 0:
                raise ValueError("ffmpeg could not read the audio")
            x = np.frombuffer(r.stdout, dtype=np.int16).reshape(-1, ch)
            off = info["a_start"] - info["v_start"]                           # audio relative to video time zero
            if abs(off) > 0.001:
                n = int(round(abs(off) * sr))
                x = np.vstack([np.zeros((n, ch), np.int16), x]) if off > 0 else x[n:]
            fade = max(1, int(FADE_MS / 1000 * sr))
            ramp = np.linspace(0, 1, fade, dtype=np.float32)[:, None]
            parts = []
            for a, b in keeps:
                s0, s1 = int(round(a / fps * sr)), int(round(b / fps * sr))
                seg = x[s0:s1].astype(np.float32)
                if len(seg) < s1 - s0:
                    seg = np.vstack([seg, np.zeros((s1 - s0 - len(seg), ch), np.float32)])
                if len(seg) > 2 * fade:
                    seg[:fade] *= ramp
                    seg[-fade:] *= ramp[::-1]
                parts.append(seg)
                check()
            y = np.clip(np.vstack(parts), -32768, 32767).astype(np.int16)
            (d / "a.raw").write_bytes(y.tobytes())
            cmd += ["-f", "s16le", "-ar", sr, "-ac", ch, "-i", d / "a.raw"]
        tail = ["-map", "[v]"]
        if info["has_audio"]:
            tail += ["-map", "1:a:0", "-c:a", "aac", "-b:a", "160k"]
        tail += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", dst]
        # The graph is long (one range per cut), so it goes in a file. The option is -filter_complex_script before ffmpeg 7 and -/filter_complex from 7 on.
        try:
            _run(cmd + ["-filter_complex_script", d / "v.txt"] + tail, check, "cut the video")
        except ValueError as e:
            if "Unrecognized option" not in str(e):
                raise
            _run(cmd + ["-/filter_complex", d / "v.txt"] + tail, check, "cut the video")
    return tmap


# --- the whole step ----------------------------------------------------------------------------------

@dataclass
class CleanResult:
    video: Path
    transcript: Transcript
    summary: str
    report: dict = field(default_factory=dict)


def _clock(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def clean_video(video: Path, tr: Transcript, out: Path, *, pace: str = "natural", llm=None, audio_loader: Callable | None = None,
                say: Callable[[str], None] = lambda _m: None, check: Callable[[], None] = lambda: None) -> CleanResult | None:
    """``out/clean.mp4`` and the transcript on its timeline, or None when the recording should be left as it is (too little speech, too much to cut,
    or nothing to cut)."""
    if pace not in PACES:
        raise ValueError("pace must be natural, fast or tight")
    if len(tr.words) < MIN_WORDS or tr.timing != "word":
        say("not enough timed speech to clean up; the video is used as it is")
        return None
    info = probe(str(video))
    if info["duration"] > MAX_MINUTES * 60:
        say(f"the cleanup handles recordings up to {MAX_MINUTES} minutes; the video is used as it is")
        return None
    db = thr = None
    try:
        from .asr import load_audio
        db = energy_db((audio_loader or load_audio)(str(video)))
        thr = speech_threshold(db)
    except Exception:                                                        # no audio track: the word-based cuts still work, pauses use word times
        db = thr = None
    lang = tr.language
    cuts = detect(tr.words, lang, db=db, thr=thr)
    if llm is not None:
        cuts += judge_retakes(tr.words, lang, llm, say=say)
    check()
    keeps = compute_keeps(tr.words, cuts, db, thr if thr is not None else 0.0, pace, info["duration"], info["fps"])
    if not keeps:
        return None
    kept_s = sum(b - a for a, b in keeps) / info["fps"]
    removed = 1 - kept_s / max(info["duration"], 0.001)
    cutw = {k for c in cuts if c.apply and c.first is not None for k in range(c.first, c.last + 1)}
    by = {}
    for c in cuts:
        if c.apply:
            by[c.kind] = by.get(c.kind, 0) + 1
    if removed > MAX_REMOVED:
        say(f"the cleanup would remove {removed:.0%} of the recording, which is too much to trust; the video is used as it is")
        return None
    saved = info["duration"] - kept_s
    if saved < 0.5 and not by:
        say("nothing to clean up; the video is used as it is")
        return None
    missed = missed_speech(tr.words, cutw, db, thr)
    if missed:
        say(f"{len(missed)} stretch(es) of speech had no recognised words ({sum(b - a for a, b in missed):.0f} s): kept as they are, without captions")
    out.mkdir(parents=True, exist_ok=True)
    dst = out / "clean.mp4"
    tmap = render_cleaned(Path(video), keeps, info, dst, check=check)
    words = remap_words(tr.words, cutw, keeps, info["fps"])
    if len(words) < MIN_WORDS // 2:
        dst.unlink(missing_ok=True)
        return None
    new = parse_transcript({"language": tr.language, "words": [w.model_dump(exclude_none=True) for w in words]})
    report = {"pace": pace, "before_seconds": round(info["duration"], 2), "after_seconds": round(kept_s, 2), "by_kind": by, "timemap": tmap,
              "missed_speech": [[round(a, 2), round(b, 2)] for a, b in missed],
              "cuts": [{"kind": c.kind, "reason": c.reason, "text": c.text, "first": c.first, "last": c.last, "t0": c.t0, "t1": c.t1, "applied": c.apply,
                        "start": round(tr.words[c.first].s, 2) if c.first is not None else c.t0,
                        "end": round(tr.words[c.last].e, 2) if c.first is not None else c.t1} for c in cuts]}
    (out / "cleanup.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    parts = [f"{n} {k.replace('_', ' ')}" for k, n in sorted(by.items(), key=lambda kv: -kv[1])]
    summary = f"cleaned: {_clock(info['duration'])} -> {_clock(kept_s)}" + (f" ({', '.join(parts)})" if parts else " (long pauses shortened)")
    say(summary)
    return CleanResult(dst, new, summary, report)
