"""Is a cheaper model, or a shortcut, as good as the current setup on this recording? Runs both on the same transcript and compares what they DECIDE (which
windows rank high, which sentences get corrected, which lines get cut), not just what they cost. Used by ``clipcrow compare-models``.

Nothing is switched on by this: it only measures. The thresholds below are what must hold before an operator sets ``CLIPCROW_MODEL_SCORE`` and the like."""
from __future__ import annotations

import math

from .costs import estimate_total
from .planner.plan import PlannerOptions, score_windows, target_count
from .planner.transcript import Transcript
from .planner.windows import build_windows
from . import textfix

# What must hold for a switch to be called safe.
MIN_POOL_OVERLAP = 0.8        # of the windows that go on to the (dear) detail pass, this share must be the same
MIN_RANK_CORR = 0.7           # and the order of all windows must broadly agree
MIN_FIX_AGREEMENT = 0.95      # sentences the two models treat the same way in text correction
MAX_MISSED_FIXES = 0.05       # corrections the baseline made that "only doubtful sentences" would never have asked about
MIN_CUT_AGREEMENT = 0.8       # lines both models would cut, out of the lines either would cut


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1                     # ties share their average rank
        i = j + 1
    return ranks


def rank_correlation(a: list[float], b: list[float]) -> float:
    """Spearman: 1 = the same order, 0 = unrelated. 1.0 for fewer than two items or no spread (nothing to disagree about)."""
    if len(a) < 2:
        return 1.0
    ra, rb = _ranks(a), _ranks(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb))
    return num / den if den else 1.0


def compare_scoring(tr: Transcript, baseline, cheap, opts: PlannerOptions | None = None) -> dict:
    opts = opts or PlannerOptions()
    windows = build_windows(tr.sentences, window_seconds=max(90, int(opts.max_clip * 1.5)))
    sa = score_windows(tr, windows, baseline, opts, {"warnings": []})
    sb = score_windows(tr, windows, cheap, opts, {"warnings": []})
    ids = [w.id for w in windows if w.id in sa and w.id in sb]
    n = target_count(tr.duration, opts)
    pool = min(len(ids), math.ceil(n * 1.5) + 1)
    top = lambda s: set(sorted(ids, key=lambda i: s[i], reverse=True)[:pool])          # noqa: E731
    overlap = len(top(sa) & top(sb)) / pool if pool else 1.0
    corr = rank_correlation([sa[i] for i in ids], [sb[i] for i in ids])
    return {"windows": len(ids), "pool": pool, "pool_overlap": round(overlap, 3), "rank_correlation": round(corr, 3),
            "ok": overlap >= MIN_POOL_OVERLAP and corr >= MIN_RANK_CORR}


def compare_refine(tr: Transcript, baseline, cheap=None) -> dict:
    """Text correction: how often the cheap model treats a sentence like the baseline did, and how many of the baseline's corrections the "doubtful only" shortcut
    would never have asked about. ``cheap`` None measures only the shortcut."""
    allsent = list(tr.sentences)
    base = textfix.corrections(tr, baseline, allsent)
    asked = {s.id for s in textfix.flagged(tr)}
    missed = [i for i in base if i not in asked]
    out = {"sentences": len(allsent), "baseline_changed": len(base), "asked_if_doubtful_only": len(asked),
           "missed_by_doubtful_only": len(missed), "missed_rate": round(len(missed) / len(base), 3) if base else 0.0,
           "shortcut_ok": (len(missed) / len(base) if base else 0.0) <= MAX_MISSED_FIXES}
    if cheap is not None:
        other = textfix.corrections(tr, cheap, allsent)
        same = sum(1 for s in allsent if base.get(s.id) == other.get(s.id))
        out.update(cheap_changed=len(other), agreement=round(same / len(allsent), 3) if allsent else 1.0,
                   ok=(same / len(allsent) if allsent else 1.0) >= MIN_FIX_AGREEMENT)
    return out


def compare_judge(tr: Transcript, baseline, cheap) -> dict:
    from .cleancut import judge_retakes
    cuts = lambda llm: {(c.first, c.last) for c in judge_retakes(tr.words, tr.language, llm) if c.apply}      # noqa: E731
    a, b = cuts(baseline), cuts(cheap)
    union = a | b
    agree = len(a & b) / len(union) if union else 1.0
    return {"baseline_cuts": len(a), "cheap_cuts": len(b), "agreement": round(agree, 3), "ok": agree >= MIN_CUT_AGREEMENT}


def cost_of(llm) -> dict:
    calls = llm.usage()
    return {"calls": len(calls), "input_tokens": sum(c.input_tokens for c in calls), "output_tokens": sum(c.output_tokens for c in calls),
            "est_usd": estimate_total(calls)}


def compare(tr: Transcript, baseline, cheap, what=("score", "refine", "judge")) -> dict:
    out: dict = {}
    if "score" in what:
        out["score"] = compare_scoring(tr, baseline, cheap)
    if "refine" in what and tr.language in textfix.REFINE_LANGS:
        out["refine"] = compare_refine(tr, baseline, cheap)
    if "judge" in what:
        out["judge"] = compare_judge(tr, baseline, cheap)
    out["cost"] = {"baseline": cost_of(baseline), "cheap": cost_of(cheap)}
    return out
