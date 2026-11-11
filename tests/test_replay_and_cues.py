import json

import pytest

from clipcrow.cli import main
from clipcrow.planner import PlanError, PlannerOptions, ReplayLLM, SourceInfo, load_transcript, plan_edl
from clipcrow.planner.models import DetailResult, ScoreResult
from clipcrow.planner.replay import prompt_key
from clipcrow.planner.transcript import TranscriptError, parse_cues
from tests.test_planner import make_transcript, scripted

SRT = """1
00:00:01,000 --> 00:00:03,000
नमस्कार मंडळी, आज आपण

2
00:00:03,500 --> 00:00:06,000
<i>आयुष्य</i> बदलणाऱ्या गोष्टीबद्दल बोलू।

3
00:01:10,000 --> 00:01:12,500
ज्ञानाची शक्ती
"""

VTT = """WEBVTT

00:00:01.000 --> 00:00:03.000 align:start
नमस्कार {\\an8}मंडळी

00:00:03.500 --> 00:00:06.000
आयुष्य बदलणाऱ्या।
"""


# --- subtitle files ---------------------------------------------------------------------

def test_srt_cues_become_words_with_times_inside_their_cue():
    tr = parse_cues(SRT, "mr")
    assert tr.timing == "cue" and tr.language == "mr"
    first = [w for w in tr.words if w.s < 3.2]
    assert [w.w for w in first] == ["नमस्कार", "मंडळी,", "आज", "आपण"]
    assert 1.0 <= first[0].s < first[-1].s and first[-1].e == pytest.approx(3.0)
    assert [w.w for w in tr.words if 3.5 <= w.s < 6.0][0] == "आयुष्य"        # <i> tags removed
    assert tr.words[-1].e == pytest.approx(72.5)                              # a cue over a minute in (HH:MM:SS parsing)
    assert all(a.e <= b.s + 1e-9 for a, b in zip(tr.words, tr.words[1:]))     # no overlap between words


def test_vtt_with_cue_settings_and_inline_markup():
    tr = parse_cues(VTT)
    assert [w.w for w in tr.words][:2] == ["नमस्कार", "मंडळी"] and tr.words[-1].w == "बदलणाऱ्या।"


def test_load_transcript_detects_srt_and_vtt(tmp_path):
    (tmp_path / "a.srt").write_text(SRT, encoding="utf-8")
    (tmp_path / "b.txt").write_text(VTT, encoding="utf-8")                    # recognised by its WEBVTT header
    assert load_transcript(tmp_path / "a.srt").timing == "cue" and load_transcript(tmp_path / "b.txt").timing == "cue"


def test_empty_subtitle_file_is_an_error():
    with pytest.raises(TranscriptError):
        parse_cues("1\n\n2\n")


def test_planning_from_cue_times_warns_about_caption_accuracy():
    tr = make_transcript()
    tr.timing = "cue"
    plan = plan_edl(tr, SourceInfo("a.mp4", 240), scripted(), PlannerOptions(target_clips=1, workers=1))
    assert any("interpolated" in w for w in plan.report["warnings"])


# --- replay -----------------------------------------------------------------------------

def test_missing_answers_are_queued_and_the_plan_reports_it(tmp_path):
    llm = ReplayLLM(tmp_path)
    with pytest.raises(PlanError):
        plan_edl(make_transcript(), SourceInfo("a.mp4", 240), llm, PlannerOptions(target_clips=1, workers=1))
    queued = sorted((tmp_path / "pending").glob("*.json"))
    assert queued and len(queued) == len(llm.pending)
    item = json.loads(queued[0].read_text(encoding="utf-8"))
    assert item["purpose"] == "score" and "<transcript" not in item["prompt"] and "answer_schema" in item


def test_answers_are_replayed_pass_by_pass_until_the_plan_completes(tmp_path):
    """Re-running after answering lets the planner advance: score pass first, then the detail pass."""
    tr, src, opts = make_transcript(), SourceInfo("a.mp4", 240), PlannerOptions(target_clips=1, workers=1)
    oracle = scripted()                                                      # plays the person/model answering
    for _ in range(4):
        llm = ReplayLLM(tmp_path)
        try:
            plan = plan_edl(tr, src, llm, opts)
            break
        except PlanError:
            for f in (tmp_path / "pending").glob("*.json"):
                item = json.loads(f.read_text(encoding="utf-8"))
                schema = ScoreResult if item["purpose"] == "score" else DetailResult
                ans = oracle.respond(item["purpose"], item["prompt"], schema)
                (tmp_path / f"{item['key']}.json").write_text(ans.model_dump_json(), encoding="utf-8")
                f.unlink()
    else:
        pytest.fail("the plan never completed")
    assert plan.edl.clips and plan.edl.ai.models == ["replay"]


def test_an_answer_only_applies_to_its_exact_prompt(tmp_path):
    assert prompt_key("score", "a") != prompt_key("score", "b") and prompt_key("score", "a") != prompt_key("detail", "a")
    (tmp_path / f"{prompt_key('score', 'a')}.json").write_text('{"windows": []}', encoding="utf-8")
    llm = ReplayLLM(tmp_path)
    assert llm.generate("s", "a", ScoreResult, purpose="score").windows == []
    from clipcrow.planner import LLMError
    with pytest.raises(LLMError):
        llm.generate("s", "b", ScoreResult, purpose="score")


def test_a_malformed_answer_is_an_llm_error_not_a_crash(tmp_path):
    from clipcrow.planner import LLMError
    (tmp_path / f"{prompt_key('score', 'a')}.json").write_text('{"windows": "nope"}', encoding="utf-8")
    with pytest.raises(LLMError, match="schema"):
        ReplayLLM(tmp_path).generate("s", "a", ScoreResult, purpose="score")


def test_plan_command_with_answers_dir_and_no_video_file(tmp_path, capsys):
    srt = tmp_path / "t.srt"
    srt.write_text(SRT, encoding="utf-8")
    rc = main(["plan", str(srt), "--source", "missing.mp4", "--duration", "120", "--out", str(tmp_path / "e.json"),
               "--answers", str(tmp_path / "ans"), "--min-clip", "5", "--max-clip", "30"])
    assert rc == 1 and list((tmp_path / "ans" / "pending").glob("*.json"))      # queued, nothing answered yet
    assert "need an answer" in capsys.readouterr().err
