"""transcript -> EDL.

    sentences -> windows -> [LLM] score windows -> pick top windows
              -> [LLM] pick clips + write copy per window -> deterministic finalize
              -> drop overlaps, keep the best N -> EDL (validated by pydantic)
"""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..costs import estimate_total
from ..edl import EDL, AIUsage, Asset, ClipAI, EDLError
from ..fonts import DEFAULT_DEVANAGARI
from . import prompts
from .llm import LLM, LLMError
from .models import DetailResult, ScoreResult
from .select import (DEVANAGARI_LANGS, PlannedClip, drop_overlaps, finalize, wrong_script)
from .transcript import Transcript
from .windows import Window, build_windows

STAT_LEAD = 0.4        # the pill appears this long before the figure is said
STAT_SECONDS = 2.4
STAT_MAX_LATE = 1.5    # a pill that would appear this long after the figure was said is dropped
WEIGHTS = {"hook": 0.35, "completeness": 0.25, "standalone": 0.25, "emotion": 0.15}


class PlanError(RuntimeError):
    def __init__(self, msg, report=None):
        super().__init__(msg)
        self.report = report or {}


@dataclass
class SourceInfo:
    uri: str
    duration: float
    width: int = 1080
    height: int = 1920
    fps: float = 30


@dataclass
class PlannerOptions:
    job_id: str = "job"
    min_clip: int = 15
    max_clip: int = 60
    target_clips: Optional[int] = None      # None: about one per two minutes of source
    max_clips: int = 10
    cutout: bool = False                    # a person cutout will exist: allow text behind the speaker
    speaker_name: Optional[str] = None      # adds a lower third
    speaker_title: str = ""
    score_batch: int = 6
    workers: int = 4
    caption_max_words: int = 4
    # Spread clips across the talk: clips must be at least spread * (source length / clips) seconds apart,
    # relaxed only when there are not enough distant candidates. 0 = plain best-first.
    spread: float = 0.5
    on_progress: Optional[Callable[[str], None]] = None


@dataclass
class Plan:
    edl: EDL
    report: dict = field(default_factory=dict)


def _map(fn, items, workers):
    if workers <= 1 or len(items) <= 1:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items))


def window_score(s) -> float:
    return sum(WEIGHTS[k] * max(0, min(10, getattr(s, k))) / 10 for k in WEIGHTS)


def target_count(duration: float, opts: PlannerOptions) -> int:
    n = opts.target_clips or math.ceil(duration / 120)
    return max(1, min(opts.max_clips, n))


def plan_edl(tr: Transcript, source: SourceInfo, llm: LLM, opts: PlannerOptions | None = None,
             *, score_llm: LLM | None = None) -> Plan:
    opts = opts or PlannerOptions()
    say = opts.on_progress or (lambda _m: None)
    score_llm = score_llm or llm
    report: dict = {"warnings": [], "dropped": [], "windows": [], "clips": []}
    lang = tr.language
    if tr.timing == "cue":
        report["warnings"].append("word times are interpolated from subtitle cues: cuts are sentence-accurate, "
                                  "caption highlighting is approximate")

    windows = build_windows(tr.sentences, window_seconds=max(90, int(opts.max_clip * 1.5)))
    say(f"{len(tr.sentences)} sentences, {len(windows)} windows")

    # --- pass 1: score windows --------------------------------------------------
    batches = [windows[i:i + opts.score_batch] for i in range(0, len(windows), opts.score_batch)]

    def score_batch(batch: list[Window]):
        items = [(w.id, "\n".join(prompts.fmt_sentence(s) for s in w.sentences(tr.sentences))) for w in batch]
        try:
            res = score_llm.generate(prompts.SYSTEM, prompts.score_prompt(items, opts.min_clip, opts.max_clip),
                                     ScoreResult, purpose="score")
        except LLMError as e:
            report["warnings"].append(f"scoring batch skipped: {e}")
            return []
        return res.windows

    scores: dict[int, float] = {}
    for ws in _map(score_batch, batches, opts.workers):
        for s in ws:
            scores[s.id] = window_score(s)
    if not scores:
        raise PlanError("every scoring call failed", report)
    for w in windows:
        report["windows"].append({"id": w.id, "start": round(w.start, 1), "end": round(w.end, 1),
                                  "score": round(scores.get(w.id, 0.0), 3)})

    # --- pick windows ---------------------------------------------------------------
    n = target_count(tr.duration, opts)
    pool = math.ceil(n * 1.5) + 1
    ranked = sorted(windows, key=lambda w: scores.get(w.id, 0.0), reverse=True)
    mid = lambda w: (w.start + w.end) / 2  # noqa: E731
    heavy = lambda w, c: min(w.end, c.end) - max(w.start, c.start) > 0.6 * (w.end - w.start)  # noqa: E731
    chosen: list[Window] = []
    for win_gap in ([opts.spread * tr.duration / pool, 0.0] if opts.spread > 0 else [0.0]):
        for w in ranked:
            if len(chosen) >= pool:
                break
            if w in chosen or any(heavy(w, c) for c in chosen):
                continue
            if all(abs(mid(w) - mid(c)) >= win_gap for c in chosen):
                chosen.append(w)
    say(f"detailing {len(chosen)} window(s) for {n} clip(s)")

    # --- pass 2: clips + copy per window ---------------------------------------------
    use_behind = opts.cutout

    def detail(w: Window) -> list[PlannedClip]:
        sents = w.sentences(tr.sentences)
        prompt = prompts.detail_prompt(sents, window_id=w.id, min_s=opts.min_clip, max_s=opts.max_clip,
                                       max_clips=2, language=lang, behind=use_behind)
        try:
            res = llm.generate(prompts.SYSTEM, prompt, DetailResult, purpose=f"detail:{w.id}")
            if any(wrong_script(c.title, lang) or wrong_script(c.hook_text, lang) for c in res.clips):
                res = llm.generate(prompts.SYSTEM, prompt + prompts.repair_suffix(lang), DetailResult,
                                   purpose=f"repair:{w.id}")
        except LLMError as e:
            report["warnings"].append(f"window {w.id} skipped: {e}")
            return []
        out = []
        for raw in res.clips:
            clip, why = finalize(raw, w, tr, min_s=opts.min_clip, max_s=opts.max_clip,
                                 window_score=scores.get(w.id, 0.0), language=lang, use_behind=use_behind)
            if clip is None:
                report["dropped"].append({"window": w.id, "reason": why})
                continue
            if wrong_script(clip.hook, lang):
                clip.notes.append("hook dropped: wrong script")
                clip.hook = ""
            if wrong_script(clip.title, lang):
                clip.notes.append("title is not in the expected script")
            out.append(clip)
        return out

    candidates = [c for cs in _map(detail, chosen, opts.workers) for c in cs]
    if not candidates:
        raise PlanError("no usable clips came out of the plan", report)
    min_gap = opts.spread * tr.duration / n if opts.spread > 0 else 0.0
    picked = drop_overlaps(candidates, n, min_gap)
    spread_skipped = [c for c in drop_overlaps(candidates, n) if c not in picked]
    for c in spread_skipped:
        report['dropped'].append({'window': None, 'reason': f'clip at {c.start:.0f}-{c.end:.0f}s left out to spread clips across the talk'})

    # --- build the EDL -----------------------------------------------------------------
    clips = [_clip_dict(i + 1, c, tr, opts, lang) for i, c in enumerate(picked)]
    for d, c in zip(clips, picked):
        d["ai"] = _clip_ai(c, d).model_dump()
    for i, c in enumerate(picked):
        report["clips"].append({"id": f"c{i + 1}", "start": round(c.start, 2), "end": round(c.end, 2),
                                "score": round(c.score, 3), "title": c.title, "why": c.why, "notes": c.notes})
    deva = lang in DEVANAGARI_LANGS
    try:
        edl = EDL.model_validate({
            "job_id": opts.job_id,
            "source": {"uri": source.uri, "duration": source.duration, "width": source.width,
                       "height": source.height, "fps": source.fps},
            "language": {"primary": lang, "script": "Deva" if deva else "Latn"},
            "transcript": [w.model_dump(exclude_none=True) for w in tr.words],
            "clips": clips,
            "assets": _assets(source, opts),
            "ai": _usage(llm, score_llm),
        })
    except ValueError as e:
        raise EDLError(f"the planner produced an invalid EDL: {e}") from e
    return Plan(edl, report)


def _usage(llm: LLM, score_llm: LLM) -> dict:
    calls = list(llm.usage()) + ([] if score_llm is llm else list(score_llm.usage()))
    u = AIUsage(calls=calls, est_cost_usd=estimate_total(calls))
    return u.model_dump()


def _assets(source: SourceInfo, opts: PlannerOptions) -> list[dict]:
    out = [Asset(id="source", kind="video", origin="upload", ai="none").model_dump()]
    if opts.cutout:
        out.append(Asset(id="cutout", kind="video", origin="matte", ai="processed",
                         tool="u2net_human_seg").model_dump())
    return out


def _clip_ai(c: PlannedClip, d: dict) -> ClipAI:
    kinds = {o["kind"] for o in d["overlays"]}
    decided = ["cut points", "title"]
    if "hook" in kinds:
        decided.append("hook text")
    if c.emphasis:
        decided.append("emphasis words")
    if "stat_pill" in kinds:
        decided.append("stat")
    if "text_behind" in kinds:
        decided.append("text behind speaker")
    return ClipAI(decided=decided, why=c.why, score=round(c.score, 3), notes=list(c.notes))


def _clip_dict(n: int, c: PlannedClip, tr: Transcript, opts: PlannerOptions, lang: str) -> dict:
    deva = lang in DEVANAGARI_LANGS
    d: dict = {
        "id": f"c{n}", "source_in": round(c.start, 3), "source_out": round(c.end, 3), "title": c.title,
        "layout": {"mode": "cutout_behind" if opts.cutout else "passthrough"},
        "captions": {"max_words": opts.caption_max_words, "emphasis_words": c.emphasis,
                     "font_family": DEFAULT_DEVANAGARI if deva else "Noto Sans"},
        "overlays": [],
    }
    ov = d["overlays"]
    hook_end = 0.0
    if c.hook:
        ov.append({"kind": "hook", "id": "hook", "t_in": 0.3, "duration": 2.5, "text": c.hook})
        hook_end = 2.8
    if c.behind is not None:
        ov.append({"kind": "text_behind", "id": "behind", "trigger_word_index": c.behind,
                   "duration": 3.0, "text": tr.words[c.behind].w.strip(".,!?।॥")})
    if c.stat is not None:
        idx, text, exact = c.stat
        spoken = tr.words[idx].s - c.start
        t0 = max(spoken - STAT_LEAD if exact else spoken, hook_end + 0.3, 0.5)
        late = t0 - spoken                       # how long after the figure was said the pill would appear
        if exact and late > STAT_MAX_LATE:
            c.notes.append("stat dropped: the figure is spoken while the hook is on screen")
        elif t0 + 1.5 <= c.duration:
            ov.append({"kind": "stat_pill", "id": "stat", "t_in": round(t0, 2), "duration": STAT_SECONDS, "text": text})
    if opts.speaker_name:
        ov.append({"kind": "lower_third", "id": "speaker", "t_in": 1.0, "duration": 3.0,
                   "title": opts.speaker_name, "subtitle": opts.speaker_title})
    return d
