"""The planner on a whole talk, replayed from recorded model answers (no API, deterministic).

Transcript: examples/synthetic_talk_mr.whisper.json (authored, not transcribed: see gen_whisper_json.py).
Answers: tests/data/replay_synthetic_talk/, written by a model answering the planner's real prompts.

An answer is replayed only for the exact prompt it was written for. If you change a prompt, this test
fails with "need an answer": re-run `clipcrow plan ... --answers <dir>`, answer the queued prompts in
dir/pending/, and copy the answer files here. That review step is the point.
"""
import shutil
from pathlib import Path

import pytest

from clipcrow.fonts import DEVANAGARI
from clipcrow.planner import PlanError, PlannerOptions, ReplayLLM, SourceInfo, load_transcript, plan_edl

ROOT = Path(__file__).resolve().parent.parent
TRANSCRIPT = ROOT / "examples" / "synthetic_talk_mr.whisper.json"
ANSWERS = Path(__file__).parent / "data" / "replay_synthetic_talk"
# Not clip material in that talk: intro + subscribe request, sponsor read, outro.
INTRO, SPONSOR, OUTRO = (0.0, 19.5), (123.7, 142.3), (190.4, 201.0)


@pytest.fixture(scope="module")
def plan(tmp_path_factory):
    work = tmp_path_factory.mktemp("answers")
    for f in ANSWERS.glob("*.json"):
        shutil.copy(f, work / f.name)
    llm = ReplayLLM(work)
    tr = load_transcript(TRANSCRIPT)
    opts = PlannerOptions(target_clips=2, min_clip=20, max_clip=60, speaker_name="शुभम", speaker_title="संस्थापक", workers=1)
    try:
        return plan_edl(tr, SourceInfo("talk.mp4", 202), llm, opts).edl, tr
    except PlanError as e:
        pytest.fail(f"{e}; {len(llm.pending)} prompt(s) need an answer: a prompt changed, re-record the answers "
                    f"(see this file's docstring)")


def overlap(a0, a1, b0, b1):
    return min(a1, b1) - max(a0, b0)


def test_clips_are_spread_across_the_talk_not_two_slices_of_one_story(plan):
    """Regression from the first run: both clips came from the opening story, back to back."""
    edl, tr = plan
    first, second = edl.clips
    assert first.source_in < tr.duration / 3 < tr.duration * 2 / 3 < second.source_in       # one early, one late
    assert second.source_in - first.source_out >= 0.5 * tr.duration / 2 - 1


def test_without_spread_the_two_best_clips_are_adjacent_slices_of_the_story(tmp_path):
    work = tmp_path / "a"
    shutil.copytree(ANSWERS, work)
    opts = PlannerOptions(target_clips=2, min_clip=20, max_clip=60, spread=0, workers=1)
    edl = plan_edl(load_transcript(TRANSCRIPT), SourceInfo("talk.mp4", 202), ReplayLLM(work), opts).edl
    assert edl.clips[1].source_in - edl.clips[0].source_out < 2


def test_two_clips_of_a_sane_length_in_order(plan):
    edl, _ = plan
    assert len(edl.clips) == 2
    assert all(20 <= c.duration <= 62 for c in edl.clips)
    assert [c.source_in for c in edl.clips] == sorted(c.source_in for c in edl.clips)


def test_housekeeping_is_never_clip_material(plan):
    edl, _ = plan
    for c in edl.clips:
        for lo, hi in (INTRO, SPONSOR, OUTRO):
            assert overlap(c.source_in, c.source_out, lo, hi) <= 0.5, (c.id, lo, hi)


def test_copy_is_devanagari_and_hooks_are_short(plan):
    edl, _ = plan
    for c in edl.clips:
        assert DEVANAGARI.search(c.title)
        hook = next(o for o in c.overlays if o.kind == "hook")
        assert DEVANAGARI.search(hook.text) and len(hook.text.split()) <= 10


def test_every_stat_pill_is_on_screen_while_its_figure_is_spoken(plan):
    """Regression: the pill used to be anchored to the sentence start and could vanish before the figure."""
    edl, tr = plan
    spoken_figures = {"₹50,000": "पन्नास", "10x": "दहा", "₹12 लाख": "बारा", "रोज ३ तास": "तीन"}
    checked = 0
    for c in edl.clips:
        for o in (x for x in c.overlays if x.kind == "stat_pill"):
            word = spoken_figures[o.text]
            times = [w.s - c.source_in for w in tr.words
                     if w.w.strip(".,") == word and c.source_in <= w.s <= c.source_out]
            assert any(o.t_in <= t <= o.t_in + o.duration for t in times), (c.id, o.text, times, o.t_in)
            checked += 1
    assert checked >= 2


def test_emphasis_words_are_real_words_of_the_clip(plan):
    edl, _ = plan
    for c in edl.clips:
        for i in c.captions.emphasis_words:
            w = edl.transcript[i]
            assert c.source_in <= w.s <= c.source_out


def test_the_usage_record_names_the_replay_and_every_call(plan):
    edl, _ = plan
    assert edl.ai.models == ["replay"] and len(edl.ai.calls) == 4          # 1 scoring call + 3 windows
