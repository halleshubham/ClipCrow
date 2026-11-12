"""Editing a finished clip: the allowed edits, and the API that applies them and re-renders."""
import threading
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("multipart")

from clipcrow.edl import EDL, load_edl
from clipcrow.render import RenderError
from clipcrow.service.editor import ClipEdit, apply_edit, clip_view
from tests.conftest import base_edl
from tests.test_service import FakePipeline, client, upload, video_bytes, wait  # noqa: F401


def edl_with(layout=None, overlays=None, **clip):
    c = {"id": "c1", "source_in": 10, "source_out": 40, "title": "शीर्षक", "overlays": overlays or [
        {"kind": "hook", "id": "hook", "t_in": 0.3, "duration": 2.5, "text": "जुना हुक"},
        {"kind": "lower_third", "id": "speaker", "t_in": 1.0, "duration": 3.0, "title": "शुभम", "subtitle": "संस्थापक"},
        {"kind": "stat_pill", "id": "stat", "t_in": 20.0, "duration": 2.5, "text": "10x"}],
         "ai": {"decided": ["cut points", "title", "hook text"], "why": "strong opening", "score": 0.8},
         **clip}
    if layout:
        c["layout"] = layout
    return EDL.model_validate(base_edl(
        clips=[c], source={"uri": "src.mp4", "duration": 600, "width": 1920, "height": 1080, "fps": 30}))


def edit(edl, **kw):
    return apply_edit(edl, "c1", ClipEdit(**kw), source_duration=600)


# --- the pure rules -----------------------------------------------------------------------------

def test_a_text_edit_changes_only_that_text_and_notes_who_did_it():
    new, replan = edit(edl_with(), title="नवीन शीर्षक", hook="नवीन हुक")
    c = new.clips[0]
    assert c.title == "नवीन शीर्षक" and next(o for o in c.overlays if o.kind == "hook").text == "नवीन हुक"
    assert replan is None
    assert c.ai.decided == ["cut points", "title", "hook text"]                 # what the AI decided is not rewritten
    assert c.ai.notes == ["edited by a person: hook, title"]                    # but a person's change is on record


def test_an_edit_that_changes_nothing_returns_the_same_edl():
    edl = edl_with()
    same, replan = edit(edl, title="शीर्षक", hook="जुना हुक")
    assert same is edl and replan is None


def test_overlays_can_be_removed_added_and_the_speaker_changed():
    new, _ = edit(edl_with(), hook="", stat="", speaker_name="नवा")
    kinds = {o.kind: o for o in new.clips[0].overlays}
    assert set(kinds) == {"lower_third"} and kinds["lower_third"].title == "नवा" and kinds["lower_third"].subtitle == "संस्थापक"
    new2, _ = apply_edit(new, "c1", ClipEdit(hook="नवा हुक", stat="3x"), source_duration=600)
    assert {o.kind for o in new2.clips[0].overlays} == {"lower_third", "hook", "stat_pill"}
    new3, _ = apply_edit(new2, "c1", ClipEdit(speaker_name=""), source_duration=600)
    assert "lower_third" not in {o.kind for o in new3.clips[0].overlays}


def test_captions_can_be_restyled_but_only_with_known_fonts_and_hex_colours():
    new, _ = edit(edl_with(), captions={"max_words": 2, "color": "#00FF00", "top_px": 1300, "font_family": "Noto Sans Devanagari"})
    cap = new.clips[0].captions
    assert (cap.max_words, cap.color, cap.top_px, cap.font_family) == (2, "#00FF00", 1300, "Noto Sans Devanagari")
    with pytest.raises(ValueError, match="font"):
        edit(edl_with(), captions={"font_family": "Comic Sans"})
    with pytest.raises(ValueError):
        edit(edl_with(), captions={"color": "red; background:url(x)"})


@pytest.mark.parametrize("kw,text", [
    ({"source_in": 10, "source_out": 11}, "at least"),
    ({"source_in": 10, "source_out": 200}, "at most"),
    ({"source_in": 590, "source_out": 640}, "past the end"),
    ({"hook": "x" * 200}, ""),
])
def test_bad_edits_are_refused_with_a_message(kw, text):
    with pytest.raises((ValueError, Exception)) as e:
        edit(edl_with(), **kw)
    assert text in str(e.value)


def test_unknown_fields_are_not_accepted():
    with pytest.raises(Exception):
        ClipEdit(source_uri="../../etc/passwd")
    with pytest.raises(Exception):
        ClipEdit(overlays=[{"kind": "hook", "id": "x", "text": "y"}])


def test_trimming_drops_overlays_that_would_appear_after_the_new_end():
    new, _ = edit(edl_with(), source_in=10, source_out=25)          # the stat pill at 20 s is still inside 15 s? no: 20 > 15
    assert "stat_pill" not in {o.kind for o in new.clips[0].overlays}
    assert new.clips[0].source_out == 25


def test_trimming_a_face_tracked_clip_asks_for_the_framing_to_be_planned_again():
    layout = {"framing": "track", "reframe": [{"t": 0, "cx": 0.3}, {"t": 5, "cx": 0.6}]}
    new, replan = edit(edl_with(layout), source_in=12, source_out=40)
    assert replan == "switch" and new.clips[0].layout.framing == "cover" and not new.clips[0].layout.reframe
    kept, none = edit(edl_with(layout), title="x")                    # a text edit keeps the face tracking
    assert none is None and kept.clips[0].layout.framing == "track"


def test_framing_choices():
    layout = {"framing": "track", "reframe": [{"t": 0, "cx": 0.3}]}
    new, replan = edit(edl_with(layout), framing="fit_blur")
    assert new.clips[0].layout.framing == "fit_blur" and not new.clips[0].layout.reframe and replan is None
    _, replan = edit(edl_with(layout), framing="auto")
    assert replan == "switch"
    _, replan = edit(edl_with(layout), multi="split")
    assert replan == "split"


def test_the_view_lists_plain_values_and_the_words_around_the_clip():
    words = [{"w": f"w{i}", "s": i, "e": i + 0.5} for i in range(0, 100)]
    edl = EDL.model_validate({**edl_with().model_dump(exclude_none=True), "transcript": words})
    v = clip_view(edl, "c1", source_duration=600)
    assert v["hook"] == "जुना हुक" and v["speaker_name"] == "शुभम" and v["stat"] == "10x"
    assert v["words"][0]["s"] >= 3.5 and v["words"][-1]["s"] <= 47                # only the neighbourhood, not 100 words
    assert v["captions"]["max_words"] == 4 and "Noto Serif Devanagari" in v["fonts"]


# --- the API ----------------------------------------------------------------------------------

def done_job(tmp_path, video_bytes, pipeline=None, **kw):
    pipe = pipeline or FakePipeline()
    c, app = client(tmp_path, pipe, **kw)
    job = wait(c, upload(c, video_bytes).json()["id"])
    assert job["status"] == "done"
    return c, app, pipe, job


def wait_clip(c, job_id, clip_id, state="ready", timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        info = next(x for x in c.get(f"/api/jobs/{job_id}").json()["clips"] if x["id"] == clip_id)
        if info["render"] == state:
            return info
        time.sleep(0.05)
    raise AssertionError(f"clip still {info['render']}")


def test_editing_a_clip_saves_the_plan_and_re_renders_it(tmp_path, video_bytes):
    c, app, pipe, job = done_job(tmp_path, video_bytes)
    jid, cid = job["id"], job["clips"][0]["id"]
    before = c.get(f"/api/jobs/{jid}/clips/{cid}").json()
    assert before["words"] and before["render"]["render"] == "ready" and not before["edited"]
    r = c.put(f"/api/jobs/{jid}/clips/{cid}", json={"title": "संपादित शीर्षक", "hook": "नवीन हुक"})
    assert r.status_code == 200 and r.json()["changed"] is True
    info = wait_clip(c, jid, cid)
    assert info["rev"] == 1 and info["title"] == "संपादित शीर्षक" and info["hook"] == "नवीन हुक" and info["edited"]
    assert pipe.rendered[-1][:2] == (cid, "संपादित शीर्षक")
    edl = load_edl(tmp_path / "data" / "jobs" / jid / "out" / "edl.json")
    assert next(x for x in edl.clips if x.id == cid).title == "संपादित शीर्षक"
    assert (tmp_path / "data" / "jobs" / jid / "out" / "edl.original.json").is_file()
    assert c.get(f"/api/jobs/{jid}/clips/{cid}/video").content.startswith(b"re-rendered")


def test_an_edit_that_changes_nothing_does_not_render(tmp_path, video_bytes):
    c, _, pipe, job = done_job(tmp_path, video_bytes)
    jid, cid, title = job["id"], job["clips"][0]["id"], job["clips"][0]["title"]
    r = c.put(f"/api/jobs/{jid}/clips/{cid}", json={"title": title})
    assert r.json()["changed"] is False and pipe.rendered == []


def test_reset_puts_the_clip_back_to_the_ai_plan(tmp_path, video_bytes):
    c, _, pipe, job = done_job(tmp_path, video_bytes)
    jid, cid, orig = job["id"], job["clips"][0]["id"], job["clips"][0]
    c.put(f"/api/jobs/{jid}/clips/{cid}", json={"title": "आपले", "source_in": orig["start"] + 2, "source_out": orig["end"]})
    wait_clip(c, jid, cid)
    assert c.post(f"/api/jobs/{jid}/clips/{cid}/reset").json()["changed"] is True
    info = wait_clip(c, jid, cid)
    assert info["title"] == orig["title"] and info["start"] == orig["start"] and not info["edited"] and info["rev"] == 2
    assert c.post(f"/api/jobs/{jid}/clips/{cid}/reset").status_code == 200       # idempotent


def test_bad_edits_are_422_and_nothing_is_saved(tmp_path, video_bytes):
    c, _, pipe, job = done_job(tmp_path, video_bytes)
    jid, cid = job["id"], job["clips"][0]["id"]
    for body in ({"source_in": 5, "source_out": 6}, {"captions": {"font_family": "Nope"}}, {"surprise": 1},
                 {"source_out": 99999}):
        r = c.put(f"/api/jobs/{jid}/clips/{cid}", json=body)
        assert r.status_code == 422, body
    assert pipe.rendered == [] and not (tmp_path / "data" / "jobs" / jid / "out" / "edl.original.json").exists()


def test_a_failed_render_is_reported_and_can_be_retried(tmp_path, video_bytes):
    pipe = FakePipeline()
    c, _, pipe, job = done_job(tmp_path, video_bytes, pipe)
    jid, cid = job["id"], job["clips"][0]["id"]
    pipe.render_fail = RenderError("the browser crashed")
    c.put(f"/api/jobs/{jid}/clips/{cid}", json={"title": "नवे"})
    info = wait_clip(c, jid, cid, "failed")
    assert "browser crashed" in info["render_error"] and info["ready"] is True       # the old video stays playable
    assert c.get(f"/api/jobs/{jid}/clips/{cid}/video").status_code == 200
    pipe.render_fail = None
    assert c.post(f"/api/jobs/{jid}/clips/{cid}/render").status_code == 200
    assert wait_clip(c, jid, cid)["render_error"] == ""


def test_an_unexpected_render_error_hides_internals(tmp_path, video_bytes):
    c, _, pipe, job = done_job(tmp_path, video_bytes)
    jid, cid = job["id"], job["clips"][0]["id"]
    pipe.render_fail = KeyError("/srv/secret/path")
    c.put(f"/api/jobs/{jid}/clips/{cid}", json={"title": "नवे"})
    info = wait_clip(c, jid, cid, "failed")
    assert "secret" not in info["render_error"] and "KeyError" in info["render_error"]


def test_two_quick_edits_render_the_latest_plan(tmp_path, video_bytes):
    gate = threading.Event()
    pipe = FakePipeline()
    c, _, pipe, job = done_job(tmp_path, video_bytes, pipe)
    jid, cid = job["id"], job["clips"][0]["id"]
    pipe.render_gate = gate
    c.put(f"/api/jobs/{jid}/clips/{cid}", json={"title": "पहिले"})
    time.sleep(0.2)                                          # the first render is now waiting on the gate
    c.put(f"/api/jobs/{jid}/clips/{cid}", json={"title": "दुसरे"})
    gate.set()
    wait_clip(c, jid, cid)
    time.sleep(0.5)
    info = wait_clip(c, jid, cid)
    assert info["title"] == "दुसरे" and pipe.rendered[-1][1] == "दुसरे"


def test_a_trim_plans_the_framing_again_in_the_background(tmp_path, video_bytes):
    c, _, pipe, job = done_job(tmp_path, video_bytes)
    jid, cid, orig = job["id"], job["clips"][0]["id"], job["clips"][0]
    r = c.put(f"/api/jobs/{jid}/clips/{cid}", json={"framing": "auto", "multi": "split"})
    assert r.status_code == 200
    info = wait_clip(c, jid, cid)
    assert info["framing"] == "cover" and pipe.rendered[-1][4] == "cover"              # the test video is already vertical


def test_editing_needs_a_finished_job_a_real_clip_and_a_login(tmp_path, video_bytes):
    gate = threading.Event()
    c, _ = client(tmp_path, FakePipeline(gate=gate))
    jid = upload(c, video_bytes).json()["id"]
    assert c.get(f"/api/jobs/{jid}/clips/c1").status_code in (404, 409)
    assert c.put(f"/api/jobs/{jid}/clips/c1", json={"title": "x"}).status_code in (404, 409)
    gate.set()
    wait(c, jid)
    assert c.put(f"/api/jobs/{jid}/clips/c99", json={"title": "x"}).status_code == 404
    assert c.put(f"/api/jobs/{jid}/clips/..%2f", json={"title": "x"}).status_code in (404, 405, 422)
    c2, _ = client(tmp_path / "t", FakePipeline(), token="s3cret")
    assert c2.put("/api/jobs/aaaaaaaaaaaa/clips/c1", json={"title": "x"}).status_code == 401


def test_a_restart_marks_interrupted_renders_failed(tmp_path, video_bytes):
    c, app, pipe, job = done_job(tmp_path, video_bytes)
    jid, cid = job["id"], job["clips"][0]["id"]
    app.state.store.update(jid, lambda j: setattr(j.clip(cid), "render", "rendering"))
    c2, _ = client(tmp_path, FakePipeline())                  # a new app over the same data directory
    info = next(x for x in c2.get(f"/api/jobs/{jid}").json()["clips"] if x["id"] == cid)
    assert info["render"] == "failed" and "restarted" in info["render_error"]
