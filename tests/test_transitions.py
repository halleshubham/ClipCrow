"""Transitions on hard cuts: which cuts is code, the looks are a closed set, and the cleanup's jump cuts reach the clips."""
import json

import pytest

from clipcrow import transitions
from clipcrow.compile import _transitions_scene
from clipcrow.edl import EDL, Transition
from clipcrow.service.editor import ClipEdit, apply_edit, clip_view
from tests.conftest import base_edl
from tests.test_service import video_bytes  # noqa: F401  (fixture)

SRC = {"uri": "s.mp4", "duration": 120, "width": 1920, "height": 1080, "fps": 30}


def clip_of(**kw):
    c = {"id": "c1", "source_in": 10, "source_out": 40, **kw}
    return EDL.model_validate(base_edl(source=SRC, clips=[c])).clips[0]


def test_seams_speaker_jumps_and_slide_edges_are_the_hard_cuts():
    c = clip_of(seams=[5.0, 12.0],
                layout={"framing": "track", "reframe": [{"t": 0, "cx": 0.3}, {"t": 8.0, "cx": 0.3}, {"t": 8.04, "cx": 0.7}, {"t": 20, "cx": 0.7}],
                        "fit_spans": [{"t0": 22.0, "t1": 25.0}]})
    assert transitions.hard_cuts(c) == [5.0, 8.04, 12.0, 22.0, 25.0]


def test_a_pan_is_not_a_cut_and_close_cuts_share_one_effect_and_the_ends_are_left_alone():
    pan = clip_of(layout={"framing": "track", "reframe": [{"t": 0, "cx": 0.3}, {"t": 3.0, "cx": 0.7}]})        # a slow move over 3 s
    assert transitions.hard_cuts(pan) == []
    c = clip_of(seams=[0.1, 5.0, 5.3, 5.5, 9.0, 29.9])
    assert transitions.hard_cuts(c) == [5.0, 9.0]


def test_the_seam_list_must_be_inside_the_clip_and_in_order():
    for bad in ([0.0], [31.0], [5.0, 4.0]):
        with pytest.raises(ValueError):
            clip_of(seams=bad)


def test_the_cleanup_report_gives_the_joins_and_a_refused_cleanup_gives_none(tmp_path):
    p = tmp_path / "cleanup.json"
    p.write_text(json.dumps({"timemap": [{"src": [0, 10], "out": [0, 10]}, {"src": [14, 20], "out": [10, 16]}, {"src": [25, 30], "out": [16, 21]}]}))
    assert transitions.seams_from_cleanup(p) == [10.0, 16.0]
    p.write_text(json.dumps({"applied": False, "timemap": [{"out": [0, 1]}, {"out": [1, 2]}]}))
    assert transitions.seams_from_cleanup(p) == [] and transitions.seams_from_cleanup(tmp_path / "none.json") == []
    p.write_text("not json")
    assert transitions.seams_from_cleanup(p) == []


def test_each_clip_gets_the_joins_inside_its_own_window_in_clip_time():
    edl = EDL.model_validate(base_edl(source=SRC, clips=[{"id": "c1", "source_in": 10, "source_out": 40}, {"id": "c2", "source_in": 60, "source_out": 90}]))
    new = transitions.with_seams(edl, [12.0, 39.98, 61.5, 100.0])
    assert new.clips[0].seams == [2.0] and new.clips[1].seams == [1.5]
    assert transitions.with_seams(edl, []) is edl


def test_every_look_compiles_to_a_scene_that_is_clear_outside_the_cuts():
    for style in transitions.STYLES:
        html = _transitions_scene([5.0, 9.0], style, 30.0, 1080, 1920)
        assert 'id="tx"' in html and "const C = [5.0, 9.0]" in html and 'window.__timelines["transitions"]' in html
    assert "backdropFilter" in _transitions_scene([5.0], "blur", 30.0, 1080, 1920) and "#ffffff" in _transitions_scene([5.0], "flash", 30.0, 1080, 1920)
    with pytest.raises(KeyError):
        _transitions_scene([5.0], "explode", 30.0, 1080, 1920)


def test_the_setting_is_a_closed_set():
    assert Transition().style == "flash" and Transition().enabled
    with pytest.raises(ValueError):
        Transition(style="explode")
    with pytest.raises(ValueError):
        ClipEdit(transition="explode")


def test_the_editor_sets_changes_and_switches_off_the_look_and_reports_the_cuts():
    edl = EDL.model_validate(base_edl(source=SRC, clips=[{"id": "c1", "source_in": 10, "source_out": 40, "seams": [5.0, 9.0]}]))
    assert clip_view(edl, "c1", source_duration=120)["transition"] == {"style": "none", "cuts": 2}
    a, _ = apply_edit(edl, "c1", ClipEdit(transition="blur"), source_duration=120)
    assert a.clips[0].transition.style == "blur" and clip_view(a, "c1", source_duration=120)["transition"]["style"] == "blur"
    b, _ = apply_edit(a, "c1", ClipEdit(transition="dip"), source_duration=120)
    assert b.clips[0].transition.style == "dip"
    off, _ = apply_edit(b, "c1", ClipEdit(transition="none"), source_duration=120)
    assert off.clips[0].transition.enabled is False and clip_view(off, "c1", source_duration=120)["transition"]["style"] == "none"
    same, _ = apply_edit(edl, "c1", ClipEdit(transition="none"), source_duration=120)
    assert same is edl


def test_a_click_cue_goes_on_each_hard_cut_when_the_sound_cues_are_on():
    from clipcrow import cues
    c = clip_of(seams=[5.0, 12.0])
    got = cues.plan_cues(c)
    assert [(x.sound, x.kind) for x in got] == [("click", "cut"), ("click", "cut")]


def test_the_option_reaches_the_plan_and_the_job(tmp_path, video_bytes):
    pytest.importorskip("fastapi")
    from clipcrow.planner import PlannerOptions, plan_edl
    from tests.test_planner import SRC as PSRC, make_transcript, scripted
    from tests.test_service import client, upload, wait
    edl = plan_edl(make_transcript(), PSRC, scripted(), PlannerOptions(target_clips=1, workers=1, transition="whip")).edl
    assert all(c.transition and c.transition.style == "whip" for c in edl.clips)
    c, _ = client(tmp_path)
    r = upload(c, video_bytes, transition="blur")
    assert r.status_code == 201 and r.json()["params"]["transition"] == "blur"
    assert upload(c, video_bytes, transition="explode").status_code == 422
    wait(c, r.json()["id"])
