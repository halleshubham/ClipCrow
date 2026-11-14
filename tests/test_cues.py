"""Sound cues: placed by code from the clip's events, quiet, sparse, and mixed under the speech without touching the picture."""
import subprocess
from pathlib import Path

import pytest

from clipcrow import cues
from clipcrow.edl import EDL, Sfx
from tests.conftest import base_edl
from tests.test_service import video_bytes  # noqa: F401  (fixture)


def clip_with(overlays=(), punch=(), duration=30.0, **kw):
    c = {"id": "c1", "source_in": 0, "source_out": duration, "overlays": list(overlays),
         "layout": {"punch_ins": [{"t": t, "duration": 2.0} for t in punch]}, **kw}
    return EDL.model_validate(base_edl(source={"uri": "s.mp4", "duration": 60, "width": 1080, "height": 1920, "fps": 30}, clips=[c])).clips[0]


def ov(kind, t, **extra):
    base = {"hook": {"kind": "hook", "id": "hook", "text": "हुक", "duration": 2.5}, "lower_third": {"kind": "lower_third", "id": "lt", "title": "नाव", "duration": 3.0},
            "stat_pill": {"kind": "stat_pill", "id": "stat", "text": "10x", "duration": 2.5}}[kind]
    return {**base, "t_in": t, **extra}


def test_each_event_gets_its_own_sound_a_little_before_it():
    c = clip_with([ov("hook", 0.3), ov("lower_third", 5.0), ov("stat_pill", 12.0)], punch=[20.0])
    got = cues.plan_cues(c)
    assert [(x.sound, x.kind) for x in got] == [("pop", "hook"), ("whoosh-soft", "lower_third"), ("ding", "stat_pill"), ("click", "punch")]
    assert [x.t for x in got] == [pytest.approx(0.22), pytest.approx(4.92), pytest.approx(11.92), pytest.approx(19.92)]


def test_events_closer_than_the_gap_keep_only_the_more_important_one():
    c = clip_with([ov("hook", 5.0), ov("stat_pill", 5.8)], punch=[6.5])
    got = cues.plan_cues(c)
    assert [(x.sound, x.kind) for x in got] == [("ding", "stat_pill")]
    far = cues.plan_cues(clip_with([ov("hook", 1.0), ov("stat_pill", 3.5)]))
    assert [x.sound for x in far] == ["pop", "ding"]


def test_nothing_goes_before_the_start_or_after_the_end_and_a_plain_clip_has_no_cues():
    c = clip_with([ov("hook", 0.0)], punch=[27.9])
    got = cues.plan_cues(c)
    assert got and got[0].t == 0.0 and all(x.t < c.duration - 0.1 for x in got) and len(got) == 2
    assert cues.plan_cues(clip_with()) == []


def test_every_planned_sound_exists_in_the_library():
    m = cues.manifest()
    assert set(cues.CUE_FOR.values()) <= set(m) and set(cues.PRIORITY) == set(cues.CUE_FOR)


def test_the_setting_is_bounded_and_off_by_default():
    assert clip_with().sfx is None and Sfx().enabled and Sfx().level_db == -10.0
    for bad in ({"level_db": 0}, {"level_db": -40}, {"enabled": True, "extra": 1}):
        with pytest.raises(ValueError):
            Sfx.model_validate(bad)


# --- the mix ---------------------------------------------------------------------------------------------------

def speech(path: Path, seconds=8.0, channels=2, db=-14.0):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c=blue:s=160x284:r=10:d={seconds}", "-f", "lavfi",
                    "-i", f"anoisesrc=color=brown:a=0.02:d={seconds}", "-af", f"volume={db}dB,aformat=channel_layouts={'stereo' if channels == 2 else 'mono'}",
                    "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], check=True)
    return path


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,channels,nb_frames:format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True).stdout
    import json
    return json.loads(out)


def window_peak(path, start, length):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-ss", str(start), "-t", str(length), "-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True)
    import re
    return float(re.search(r"max_volume: (-?[\d.]+) dB", r.stderr).group(1))


@pytest.mark.parametrize("channels", [1, 2])
def test_a_cue_is_heard_where_it_was_placed_and_nowhere_else(tmp_path, channels):
    p = speech(tmp_path / "s.mp4", channels=channels)
    quiet_before = window_peak(p, 2.0, 1.0)
    n = cues.mix(p, [cues.Cue(2.0, "ding", "stat_pill")], -10.0)
    assert n == 1
    assert window_peak(p, 2.0, 1.0) > quiet_before + 10                    # the ding is clearly there...
    assert window_peak(p, 5.0, 1.5) < -25                                   # ...and the rest is still just the speech bed
    assert window_peak(p, 2.0, 1.0) <= -8.5                                 # ...at the level asked for (peak about -10 dBFS)
    info = probe(p)
    assert abs(float(info["format"]["duration"]) - 8.0) < 0.3
    assert {s["codec_name"] for s in info["streams"]} == {"h264", "aac"} and next(s for s in info["streams"] if s["codec_name"] == "aac")["channels"] == channels
    assert not list(tmp_path.glob("*.cues.mp4"))


def test_the_picture_is_copied_not_re_encoded(tmp_path):
    p = speech(tmp_path / "s.mp4")
    before = probe(p)["streams"][0]
    cues.mix(p, [cues.Cue(1.0, "pop", "hook"), cues.Cue(4.0, "shutter", "media")], -10.0)
    after = next(s for s in probe(p)["streams"] if s["codec_name"] == "h264")
    assert after["nb_frames"] == before["nb_frames"]


def test_no_audio_no_cues_or_a_broken_file_leave_the_clip_alone(tmp_path):
    silent = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=160x284:r=10:d=3", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(silent)], check=True)
    ok = speech(tmp_path / "s.mp4")
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"nope")
    for p, c in ((silent, [cues.Cue(1.0, "pop", "hook")]), (ok, []), (ok, [cues.Cue(1.0, "no-such-sound", "hook")]), (bad, [cues.Cue(1.0, "pop", "hook")])):
        snap = p.read_bytes()
        assert cues.mix(p, c, -10.0) is None and p.read_bytes() == snap
    assert not list(tmp_path.glob("*.cues.mp4"))


def test_the_render_mixes_cues_before_the_outro_and_the_loudness_pass():
    src = (Path(__file__).resolve().parent.parent / "clipcrow" / "pipeline.py").read_text(encoding="utf-8")
    assert src.index("cues.mix") < src.index("append_outro") < src.index("loudness.normalise")


# --- the option, the plan and the editor ---------------------------------------------------------------------------

def test_the_option_puts_the_setting_on_every_clip_and_nothing_otherwise():
    from clipcrow.planner import PlannerOptions, plan_edl
    from tests.test_planner import SRC, make_transcript, scripted
    on = plan_edl(make_transcript(), SRC, scripted(), PlannerOptions(target_clips=1, workers=1, sounds=True)).edl
    off = plan_edl(make_transcript(), SRC, scripted(), PlannerOptions(target_clips=1, workers=1)).edl
    assert all(c.sfx and c.sfx.enabled for c in on.clips) and all(c.sfx is None for c in off.clips)


def test_the_editor_switches_the_cues_on_and_off_and_keeps_their_level():
    from clipcrow.service.editor import ClipEdit, apply_edit, clip_view
    edl = EDL.model_validate(base_edl(source={"uri": "s.mp4", "duration": 60, "width": 1080, "height": 1920, "fps": 30},
                                      clips=[{"id": "c1", "source_in": 0, "source_out": 20, "sfx": {"level_db": -14}}]))
    assert clip_view(edl, "c1", source_duration=60)["sfx"] == {"on": True}
    off, _ = apply_edit(edl, "c1", ClipEdit(sfx=False), source_duration=60)
    assert off.clips[0].sfx.enabled is False and off.clips[0].sfx.level_db == -14 and clip_view(off, "c1", source_duration=60)["sfx"] == {"on": False}
    again, _ = apply_edit(off, "c1", ClipEdit(sfx=True), source_duration=60)
    assert again.clips[0].sfx.enabled and again.clips[0].sfx.level_db == -14
    same, _ = apply_edit(edl, "c1", ClipEdit(sfx=True), source_duration=60)
    assert same is edl
    plain = EDL.model_validate(base_edl(source={"uri": "s.mp4", "duration": 60, "width": 1080, "height": 1920, "fps": 30}, clips=[{"id": "c1", "source_in": 0, "source_out": 20}]))
    made, _ = apply_edit(plain, "c1", ClipEdit(sfx=True), source_duration=60)
    assert made.clips[0].sfx and made.clips[0].sfx.enabled


def test_the_job_option_is_kept(tmp_path, video_bytes):
    pytest.importorskip("fastapi")
    from tests.test_service import client, upload, wait
    c, _ = client(tmp_path)
    r = upload(c, video_bytes, sounds="true")
    assert r.status_code == 201 and r.json()["params"]["sounds"] is True
    wait(c, r.json()["id"])
    assert upload(c, video_bytes).json()["params"]["sounds"] is False
