"""The comparison that has to pass before a cheaper model is switched on: same decisions, not only a smaller bill."""
import pytest

from clipcrow.aicompare import compare, compare_refine, compare_scoring, rank_correlation
from clipcrow.planner import FakeLLM
from tests.test_planner import make_transcript


def scorer(rule):
    """A model that scores window k with rule(k) (all four dimensions equal, so the score is rule(k)/10)."""
    def respond(purpose, prompt, schema):
        import re
        ids = [int(x) for x in re.findall(r'<window id="(\d+)">', prompt)]
        return {"windows": [{"id": i, "hook": rule(i), "completeness": rule(i), "standalone": rule(i), "emotion": rule(i), "why": ""} for i in ids]}
    return FakeLLM(respond)


def test_rank_correlation_basics():
    assert rank_correlation([1, 2, 3], [10, 20, 30]) == pytest.approx(1.0)
    assert rank_correlation([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)
    assert rank_correlation([1], [1]) == 1.0 and rank_correlation([2, 2, 2], [1, 2, 3]) == 1.0


def test_a_model_that_ranks_the_windows_the_same_way_passes():
    tr = make_transcript(120)
    r = compare_scoring(tr, scorer(lambda k: k % 10), scorer(lambda k: (k % 10) if k % 10 < 9 else 8))
    assert r["pool_overlap"] >= 0.8 and r["ok"] is True


def test_a_model_that_ranks_them_differently_fails():
    tr = make_transcript(120)
    r = compare_scoring(tr, scorer(lambda k: k % 10), scorer(lambda k: 9 - k % 10))
    assert r["ok"] is False and r["rank_correlation"] < 0.7


def _refine_llm(fix):
    return FakeLLM(lambda p, u, s: {"items": [{"id": int(line.split(":")[0]), "text": fix(line.split(":", 1)[1].strip())} for line in u.splitlines() if ":" in line]})


def test_refine_comparison_counts_agreement_and_what_the_shortcut_would_miss():
    tr = make_transcript(8, language="mr")                       # no confidences: the shortcut asks about everything, so it misses nothing
    same = _refine_llm(lambda t: t + " पण")
    r = compare_refine(tr, same, same)
    assert r["missed_by_doubtful_only"] == 0 and r["shortcut_ok"] is True and r["agreement"] == 1.0 and r["ok"] is True
    other = _refine_llm(lambda t: t)                             # never corrects: disagrees wherever the baseline corrects
    assert compare_refine(tr, same, other)["ok"] is False


def test_the_whole_report_has_a_verdict_per_part_and_the_cost_of_both():
    tr = make_transcript(60)
    rep = compare(tr, scorer(lambda k: k % 10), scorer(lambda k: k % 10), what=("score", "refine"))
    assert rep["score"]["ok"] is True and "refine" in rep and set(rep["cost"]) == {"baseline", "cheap"}
