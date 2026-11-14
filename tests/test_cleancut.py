import json
import shutil
import subprocess

import numpy as np
import pytest

from clipcrow import cleancut as cc
from clipcrow.edl import Word
from clipcrow.planner import FakeLLM, parse_transcript
from tests.test_service import video_bytes  # noqa: F401  (the pytest fixture)


def W(text, s, e):
    return Word(w=text, s=s, e=e)


def seq(*items, gap=0.12, start=0.0, step=0.4):
    """words from strings, one every ``step`` s; ("pause", seconds) adds silence."""
    out, t = [], start
    for it in items:
        if isinstance(it, tuple):
            t += it[1]
            continue
        out.append(W(it, t, t + step - gap))
        t += step
    return out


# --- what counts as a hesitation ---------------------------------------------------------------------------------

@pytest.mark.parametrize("w", ["अं", "उम्", "ऊं", "हम्म", "ह्म्म", "अऽ", "um", "Uh,", "hmm", "uhhh", "mm", "आँ"])
def test_hesitation_sounds_are_fillers(w):
    assert cc.is_filler(w)


@pytest.mark.parametrize("w", ["हम", "आ", "हां", "the", "umbrella", "उमेश", "अब", "मम्मी", "ok", ""])
def test_real_words_are_not_fillers(w):
    assert not cc.is_filler(w)


def kinds(cuts, applied=True):
    return [(c.kind, c.first, c.last) for c in cuts if c.apply == applied and c.first is not None]


# --- detection ---------------------------------------------------------------------------------------------------

def test_fillers_and_function_word_stutters_are_cut_in_english_and_marathi():
    en = seq("so", "um", "the", "the", "thing", "is", "good")
    assert ("filler", 1, 1) in kinds(cc.detect(en, "en")) and ("stutter", 2, 2) in kinds(cc.detect(en, "en"))
    mr = seq("मी", "अं", "तो", "तो", "आलो")
    got = kinds(cc.detect(mr, "mr"))
    assert ("filler", 1, 1) in got and ("stutter", 2, 2) not in got          # a fluent single-word repeat in Marathi is only suggested
    assert ("stutter", 2, 2) in kinds(cc.detect(mr, "mr"), applied=False)


def test_marathi_reduplication_is_kept_but_a_comma_or_a_pause_makes_it_a_stutter():
    assert kinds(cc.detect(seq("धीरे", "धीरे", "चल"), "hi")) == []
    assert ("stutter", 0, 0) in kinds(cc.detect([W("तो,", 0, 0.3), W("तो", 0.35, 0.6), W("आला", 0.7, 1.0)], "mr"))
    assert ("stutter", 0, 0) in kinds(cc.detect([W("कधी", 0, 0.3), W("कधी", 0.6, 0.9), W("आला", 1.0, 1.3)], "mr"))     # a 0.3 s gap


def test_emphasis_is_never_a_stutter():
    assert kinds(cc.detect(seq("no", "no", "no", "stop"), "en")) == [] or all(k[0] != "stutter" for k in kinds(cc.detect(seq("no", "no", "stop"), "en")))


def test_a_phrase_said_twice_drops_the_first_attempt_and_a_dashed_partial_word_goes():
    w = seq("this", "saves", "your", "time", "this", "saves", "your", "time", "every", "week")
    assert ("false_start", 0, 3) in kinds(cc.detect(w, "en"))
    p = [W("pro-", 0, 0.2), W("product", 0.3, 0.8), W("is", 0.9, 1.1)]
    assert ("stutter", 0, 0) in kinds(cc.detect(p, "en"))
    q = [W("pro", 0, 0.2), W("product", 0.3, 0.8), W("is", 0.9, 1.1)]
    assert kinds(cc.detect(q, "en")) == [] and ("stutter", 0, 0) in kinds(cc.detect(q, "en"), applied=False)


def test_an_earlier_take_of_a_line_is_dropped_and_the_later_one_kept():
    a = seq("तो", "ये", "आपके", "घंटे", "बचाता", "है।", ("pause", 1.0), "तो", "ये", "आपके", "घंटे", "बचाता", "है", "हर", "हफ्ते।")
    got = kinds(cc.detect(a, "hi"))
    assert any(f == 0 and l == 5 for _, f, l in got)                  # words 0-5 (the earlier take) go, whichever rule saw it first; the later take stays


def test_voice_in_a_gap_with_no_word_is_a_hidden_filler():
    words = [W("so", 0.0, 0.3), W("yes", 1.5, 1.8), W("go", 2.0, 2.2)]
    db = np.full(300, -80.0)
    db[0:30] = -20.0
    db[150:180] = -20.0
    db[60:85] = -20.0                                    # 0.25 s of voice between 0.3 s and 1.5 s
    thr = cc.speech_threshold(db)
    got = [c for c in cc.detect(words, "en", db=db, thr=thr) if c.kind == "hidden_filler"]
    assert len(got) == 1 and 0.55 < got[0].t0 < 0.65 and 0.85 < got[0].t1 < 0.95


# --- the editorial pass --------------------------------------------------------------------------------------

def sentence_words(n):
    """n one-line sentences, a pause apart."""
    return [W(f"वाक्य{i}।", i * 2.0, i * 2.0 + 0.8) for i in range(n)]


def test_the_model_can_only_drop_lines_it_was_shown_within_the_caps_and_never_the_last_line():
    words = sentence_words(10)
    seen = {}

    def respond(purpose, prompt, schema):
        seen["prompt"], seen["purpose"] = prompt, purpose
        return cc.Drops(drop=[cc.Drop(id=2, why="restart"), cc.Drop(id=2, why="dup"), cc.Drop(id=99), cc.Drop(id=9), cc.Drop(id=4), cc.Drop(id=5), cc.Drop(id=6)])
    cuts = cc.judge_retakes(words, "mr", FakeLLM(respond))
    assert seen["purpose"] == "retake-judge" and seen["prompt"].startswith("0: वाक्य0")
    assert [c.first for c in cuts] == [2, 4, 5]                    # 99 and the last line refused, 2 once, the cap is 35% of 10 = 3
    assert all(c.kind == "judged" for c in cuts)


def test_a_failing_model_keeps_the_rule_based_cuts():
    said = []

    def boom(p, prompt, schema):
        raise RuntimeError("down")
    assert cc.judge_retakes(sentence_words(5), "en", FakeLLM(boom), say=said.append) == [] and said and "skipped" in said[0]


# --- what to keep ----------------------------------------------------------------------------------------------

def test_a_long_pause_shrinks_to_the_pace_and_a_short_one_stays():
    words = [W("a", 0.0, 0.4), W("b", 0.5, 0.9), W("c", 3.0, 3.4)]           # 0.1 s then 2.1 s
    keeps = cc.compute_keeps(words, [], None, 0.0, "natural", 4.0, 30.0)
    assert len(keeps) == 2                                                   # the long pause splits the kept ranges
    gap = (keeps[1][0] - keeps[0][1]) / 30.0
    assert 1.5 < gap < 2.2                                                   # ... and the cut removes most of it
    kept = sum(b - a for a, b in keeps) / 30.0
    assert kept < 3.0


def test_cut_words_leave_no_trace_and_the_frame_grid_is_respected():
    words = seq("so", "um", "yes", "good")
    um = cc.detect(words, "en")
    keeps = cc.compute_keeps(words, um, None, 0.0, "natural", 3.0, 25.0)
    assert all(isinstance(a, int) and isinstance(b, int) and b > a for a, b in keeps)
    kept_words = cc.remap_words(words, {1}, keeps, 25.0)
    assert [w.w for w in kept_words] == ["so", "yes", "good"]
    assert all(b.s >= a.e - 1e-6 for a, b in zip(kept_words, kept_words[1:]))        # still in order on the new timeline


def test_remapped_words_move_up_by_what_was_cut_before_them():
    words = [W("a", 0.0, 0.4), W("gap", 0.5, 0.9), W("b", 5.0, 5.4)]
    keeps = [(0, 30), (120, 180)]                                            # keep 0-1 s and 4-6 s at 30 fps
    out = cc.remap_words(words, {1}, keeps, 30.0)
    assert [w.w for w in out] == ["a", "b"] and out[0].s == 0.0 and abs(out[1].s - (1.0 + 1.0)) < 0.01


# --- the whole step on a real file ---------------------------------------------------------------------------------

ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


@pytest.fixture(scope="module")
def raw_video(tmp_path_factory):
    p = tmp_path_factory.mktemp("raw") / "raw.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=30:d=16", "-f", "lavfi", "-i", "sine=f=440:d=16",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(p)], check=True)
    return p


def uniq(k):
    """A four-letter Devanagari token unlike every other one (so synthetic sentences are not near-repeats of each other)."""
    return chr(0x0915 + k % 30) + chr(0x0915 + (k * 7 + 3) % 30) + "ा" + chr(0x0915 + (k // 30 * 11 + 5) % 30)


def long_talk():
    """~2 min of Marathi sentences, with an 'अं', a stutter and a 4 s pause, for the planner."""
    items = []
    for i in range(36):
        sent = [uniq(i * 6 + k) for k in range(5)] + [uniq(i * 6 + 5) + "।"]
        if i == 5:
            sent.insert(2, "अं")
        if i == 9:
            sent[3:3] = ["आपण"]
        items += sent
        if i == 15:
            items.append(("pause", 4.0))
    words = seq(*items, step=0.5, gap=0.1)
    for k, w in enumerate(words):                                      # a pause between sentences, so lines split cleanly
        pass
    return parse_transcript({"language": "mr", "words": [w.model_dump() for w in words]})


@pytest.fixture(scope="module")
def long_video(tmp_path_factory):
    p = tmp_path_factory.mktemp("long") / "long.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=160x120:r=15:d=130", "-f", "lavfi", "-i", "sine=f=300:d=130",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(p)], check=True)
    return p


def talk():
    """16 s: words, an 'um', a stutter and a 4 s pause in the middle."""
    items = ["hello", "everyone", "um", "today", "the", "the", "plan", "is", "simple", ("pause", 4.0), "first", "we", "start", "then", "we", "finish", "nicely", "now",
             "please", "subscribe", "and", "share", "this", "video", "with", "friends", "thanks", "bye", "friends", "again", "soon", "ok"]
    words = seq(*items, step=0.38)
    return parse_transcript({"language": "en", "words": [w.model_dump() for w in words]})


@ffmpeg
def test_the_cleaned_video_is_shorter_in_sync_and_its_transcript_matches(tmp_path, raw_video):
    tr = talk()
    res = cc.clean_video(raw_video, tr, tmp_path, pace="natural", audio_loader=lambda p: np.zeros(16 * 16000, np.float32))
    assert res is not None and res.video.name == "clean.mp4" and res.video.is_file()
    info = cc.probe(str(res.video))
    raw = cc.probe(str(raw_video))
    assert info["duration"] < raw["duration"] - 3.0                        # the 4 s pause and the fillers are gone
    streams = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration", "-of", "json", str(res.video)], capture_output=True, text=True).stdout
    d = {s["codec_type"]: float(s["duration"]) for s in json.loads(streams)["streams"]}
    assert abs(d["video"] - d["audio"]) < 2 / 30 + 0.03                    # sync: both streams the same length (within two frames)
    texts = [w.w for w in res.transcript.words]
    assert "um" not in texts and texts.count("the") == 1 and "hello" in texts
    assert res.transcript.duration <= info["duration"] + 0.1 and all(w.e <= info["duration"] + 0.1 for w in res.transcript.words)
    rep = json.loads((tmp_path / "cleanup.json").read_text(encoding="utf-8"))
    assert rep["by_kind"].get("filler") == 1 and rep["by_kind"].get("stutter") == 1 and rep["after_seconds"] < rep["before_seconds"]
    assert "cleaned" in res.summary


@ffmpeg
def test_too_much_to_cut_or_too_little_speech_leaves_the_video_alone(tmp_path, raw_video):
    few = parse_transcript({"language": "en", "words": [{"w": "hi", "s": 0, "e": 0.3}] * 3 + [{"w": "x", "s": 1, "e": 1.2}]})
    assert cc.clean_video(raw_video, few, tmp_path, audio_loader=lambda p: np.zeros(16000, np.float32)) is None
    clean = parse_transcript({"language": "en", "words": [w.model_dump() for w in seq(*[uniq(i) for i in range(40)], step=0.4)]})
    said = []
    assert cc.clean_video(raw_video, clean, tmp_path / "b", audio_loader=lambda p: np.zeros(16000, np.float32), say=said.append) is None and said
    assert not (tmp_path / "clean.mp4").exists()


@ffmpeg
def test_a_cue_timed_transcript_is_not_cut(tmp_path, raw_video):
    tr = talk()
    tr.timing = "cue"
    assert cc.clean_video(raw_video, tr, tmp_path) is None


# --- the service ---------------------------------------------------------------------------------------------------

def test_the_working_video_is_the_cleaned_cut_when_there_is_one(tmp_path):
    from clipcrow.service.jobs import JobStore
    store = JobStore(tmp_path)
    jid = "a" * 12
    (store.dir(jid) / "out").mkdir(parents=True)
    (store.dir(jid) / "source.mp4").write_bytes(b"orig")
    assert store.working_video(jid).name == "source.mp4"
    (store.dir(jid) / "out" / "clean.mp4").write_bytes(b"cut")
    assert store.working_video(jid).name == "clean.mp4" and store.source_path(jid).name == "source.mp4"


def test_the_options_reach_the_job_and_the_pace_is_checked(tmp_path, video_bytes):
    from tests.test_service import FakePipeline, client, upload, wait
    c, _ = client(tmp_path, FakePipeline())
    job = wait(c, upload(c, video_bytes, clean="true", clean_pace="tight").json()["id"])
    assert job["params"]["clean"] is True and job["params"]["clean_pace"] == "tight"
    off = wait(c, upload(c, video_bytes).json()["id"])
    assert off["params"]["clean"] is False and off["params"]["clean_pace"] == "natural"
    assert upload(c, video_bytes, clean="true", clean_pace="warp").status_code == 422


@ffmpeg
def test_make_cuts_the_recording_first_and_everything_after_uses_the_cleaned_video(tmp_path, long_video):
    from clipcrow.make import Hooks, make
    from clipcrow.planner import PlannerOptions
    from tests.test_planner import scripted
    tr = long_talk()
    stages = []
    res = make(long_video, tmp_path / "o", llm=scripted(), opts=PlannerOptions(target_clips=1, workers=1), render=False, clean=True, hooks=Hooks(stage=lambda n, s, d="": stages.append((n, s, d))),
               transcribe_fn=lambda media, language=None, progress=None: tr, refine_text=False)
    out = tmp_path / "o"
    assert (out / "clean.mp4").is_file() and (out / "cleanup.json").is_file() and (out / "transcript.original.json").is_file()
    assert res.edl.source.uri == "clean.mp4" and res.edl.source.duration < cc.probe(str(long_video))["duration"] - 3     # the plan is on the cleaned video
    done = [d for n, s, d in stages if n == "transcribe" and s == "done"]
    assert done and "cleaned" in done[0]


@ffmpeg
def test_make_without_the_option_leaves_the_video_alone(tmp_path, long_video):
    from clipcrow.make import make
    from clipcrow.planner import PlannerOptions
    from tests.test_planner import scripted
    res = make(long_video, tmp_path / "o", llm=scripted(), opts=PlannerOptions(target_clips=1, workers=1), render=False,
               transcribe_fn=lambda media, language=None, progress=None: long_talk(), refine_text=False)
    assert res.edl.source.uri == "long.mp4" and not (tmp_path / "o" / "clean.mp4").exists()


@ffmpeg
def test_an_ffmpeg_that_renamed_the_filter_file_option_is_retried_with_the_new_name(tmp_path, raw_video, monkeypatch):
    """ffmpeg 7 replaced -filter_complex_script with -/filter_complex; CI has the newer one, a laptop the older. Both spellings are tried, in that order."""
    calls = []

    def fake(cmd, check, label):
        cmd = [str(c) for c in cmd]
        calls.append(cmd)
        if "-filter_complex_script" in cmd:
            raise ValueError("ffmpeg could not cut the video: Unrecognized option 'filter_complex_script'.")
        open(cmd[-1], "wb").write(b"cut")                 # the second spelling "works"
    monkeypatch.setattr(cc, "_run", fake)
    cc.render_cleaned(raw_video, [(0, 60), (120, 200)], cc.probe(str(raw_video)), tmp_path / "c.mp4")
    assert "-filter_complex_script" in calls[0] and "-/filter_complex" in calls[1] and (tmp_path / "c.mp4").is_file()

    def other(cmd, check, label):
        raise ValueError("ffmpeg could not cut the video: Invalid data found")
    monkeypatch.setattr(cc, "_run", other)
    with pytest.raises(ValueError, match="Invalid data"):                      # any other failure is NOT retried under another name
        cc.render_cleaned(raw_video, [(0, 60)], cc.probe(str(raw_video)), tmp_path / "d.mp4")


# --- repeats that were changed, extended or respelled -----------------------------------------------------------------

def line(*tokens, start=0.0, step=0.4):
    return seq(*tokens, start=start, step=step)


def two_lines(first, second, pause=1.2):
    a = line(*first)
    b = line(*second, start=a[-1].e + pause)
    return a + b


def cut_range(words, lang="mr"):
    return [(c.first, c.last) for c in cc.detect(words, lang) if c.apply and c.first is not None and c.kind != "filler"]


def test_a_sentence_said_again_with_one_word_changed_drops_the_earlier_try():
    w = two_lines("आज आपण बँकेच्या खाजगीकरणाबद्दल बोलणार आहोत।".split(), "आज आपण बँकांच्या खाजगीकरणाबद्दल बोलणार आहोत।".split())
    assert (0, 5) in cut_range(w)                                                        # all six words of the first try, none of the second


def test_a_sentence_restated_with_words_added_drops_the_earlier_try():
    w = two_lines("सरकार बँक विकणार आहे।".split(), "सरकार नफ्यातली बँक विकणार आहे म्हणून लोक चिंतेत आहेत।".split())
    assert (0, 3) in cut_range(w)


def test_recogniser_spelling_drift_does_not_hide_a_repeat():
    w = two_lines("मी तुम्हाला नाही सांगणार होतो हे".split(), "मी तुम्हाला नही सांगणार होतो हे सगळं".split())
    assert (0, 5) in cut_range(w)


def test_a_phrase_restated_inside_one_sentence_with_a_change_drops_the_first_try():
    w = line("हे", "बँक", "सरकारी", "मालकीची", "आहे", "हे", "बँक", "सरकारच्या", "मालकीची", "आहे", "म्हणून", "महत्त्वाची")
    assert any(f == 0 for f, _ in cut_range(w))


def test_parallel_sentences_said_on_purpose_are_not_retakes():
    w = line("आम्ही", "लढू।", start=0.0) + line("आम्ही", "जिंकू।", start=2.0) + line("आम्ही", "टिकू।", start=4.0)
    assert cut_range(w) == []                                                          # too short: emphasis
    lst = (line(*"आज आपण बँकेबद्दल बोलूया आणि".split(), start=0) + line(*"आज आपण विम्याबद्दल बोलूया आणि".split(), start=3)
           + line(*"आज आपण कर्जाबद्दल बोलूया आणि".split(), start=6))
    assert cut_range(lst) == []                                                        # three alike lines in a row are a list


def test_a_short_recap_or_a_much_later_callback_is_not_a_retake():
    long = "बँकांचे खाजगीकरण केल्यावर सामान्य लोकांच्या ठेवींचे काय होईल हा प्रश्न आहे".split()
    recap = "म्हणजे ठेवींचे काय".split()
    assert cut_range(two_lines(long, recap)) == []
    late = two_lines("आज आपण बँकेच्या खाजगीकरणाबद्दल बोलणार आहोत।".split(), "आज आपण बँकेच्या खाजगीकरणाबद्दल बोलणार आहोत।".split(), pause=30.0)
    assert (0, 4) not in cut_range(late)


def test_the_editing_pass_is_told_that_a_reworded_second_try_replaces_the_first():
    assert "reworded" in cc.JUDGE_SYSTEM and "EARLIER line" in cc.JUDGE_SYSTEM


# --- long reels ------------------------------------------------------------------------------------------------------

def test_the_editor_may_trim_a_long_clip_but_not_stretch_it_and_shorts_keep_their_limit():
    from clipcrow.edl import EDL
    from clipcrow.service.editor import ClipEdit, apply_edit, clip_view
    from tests.conftest import base_edl
    d = base_edl(clips=[{"id": "c1", "source_in": 0, "source_out": 400}], source={"uri": "s.mp4", "duration": 900, "width": 1080, "height": 1920, "fps": 30})
    edl = EDL.model_validate(d)
    assert clip_view(edl, "c1", source_duration=900)["max_seconds"] == 400
    out, _ = apply_edit(edl, "c1", ClipEdit(source_out=380), source_duration=900)
    assert out.clips[0].source_out == 380
    with pytest.raises(ValueError, match="at most 400"):
        apply_edit(edl, "c1", ClipEdit(source_out=450), source_duration=900)
    short = EDL.model_validate(base_edl(clips=[{"id": "c1", "source_in": 0, "source_out": 20}], source={"uri": "s.mp4", "duration": 900, "width": 1080, "height": 1920, "fps": 30}))
    assert clip_view(short, "c1", source_duration=900)["max_seconds"] == 90


def test_the_job_accepts_long_lengths_and_the_whole_video_option(tmp_path, video_bytes):
    from tests.test_service import FakePipeline, client, upload, wait
    c, _ = client(tmp_path, FakePipeline())
    long = wait(c, upload(c, video_bytes, min_clip="180", max_clip="480").json()["id"])
    assert long["params"]["min_clip"] == 180 and long["params"]["max_clip"] == 480
    whole = wait(c, upload(c, video_bytes, whole="true").json()["id"])
    assert whole["params"]["whole"] is True
    assert upload(c, video_bytes, max_clip="1200").status_code == 422                  # more than 15 minutes is refused


def test_a_long_clip_is_rendered_on_the_server_not_sent_through_the_proxy(tmp_path):
    from types import SimpleNamespace
    from clipcrow.edl import EDL
    from clipcrow.service.workers import RemoteRenderer
    from tests.conftest import base_edl
    edl = EDL.model_validate(base_edl(clips=[{"id": "c1", "source_in": 0, "source_out": 300}], source={"uri": "s.mp4", "duration": 900, "width": 1080, "height": 1920, "fps": 30}))
    ran, said = [], []
    hub = SimpleNamespace(online=lambda kind: True)
    rr = RemoteRenderer(hub, say=said.append, fallback=lambda *a, **k: ran.append("local") or ("out.mp4", None))
    rr(edl, "c1", tmp_path / "c1.mp4", workdir=tmp_path, media_root=tmp_path)
    assert ran == ["local"] and said and "long" in said[0]


def test_the_worker_clip_limit_comes_from_the_environment(monkeypatch):
    from clipcrow.service.pipelines import _worker_clip_limit
    assert _worker_clip_limit() == 100.0
    monkeypatch.setenv("CLIPCROW_WORKER_MAX_CLIP_SECONDS", "0")
    assert _worker_clip_limit() == 0.0
    monkeypatch.setenv("CLIPCROW_WORKER_MAX_CLIP_SECONDS", "nonsense")
    assert _worker_clip_limit() == 100.0
