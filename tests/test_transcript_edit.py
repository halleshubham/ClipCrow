"""Checking and correcting the words captions are made from: pure rules, then the API with a re-render of only the clips that use them."""
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("multipart")

from clipcrow.edl import EDL, load_edl
from clipcrow.service.editor import SentenceEdit, TranscriptEdit, apply_transcript, transcript_view
from tests.conftest import base_edl
from tests.test_editor import done_job, wait_clip
from tests.test_service import video_bytes  # noqa: F401


def edl_two_clips():
    words = [{"w": w, "s": 1.0 + i * 0.5, "e": 1.5 + i * 0.5} for i, w in enumerate(["नमस्कार", "मित्रांनो.", "आज", "आपण", "बोलू", "या.", "दुसरे", "वाक्य", "येथे."])]
    words[6]["s"] = 30.0
    words[6]["e"] = 30.4
    for i, w in enumerate(words[6:]):
        w["s"], w["e"] = 30.0 + i * 0.5, 30.4 + i * 0.5
    return EDL.model_validate(base_edl(source={"uri": "src.mp4", "duration": 60, "width": 1080, "height": 1920, "fps": 30}, transcript=words, clips=[{"id": "c1", "source_in": 0, "source_out": 10}, {"id": "c2", "source_in": 28, "source_out": 40}]))


def edit_of(edl, text, sentence=0):
    s = transcript_view(edl)["sentences"][sentence]
    return TranscriptEdit(edits=[SentenceEdit(id=s["id"], start=s["start"], text=text)])


def test_the_view_lists_sentences_with_times_and_the_clips_that_show_them():
    v = transcript_view(edl_two_clips())
    assert v["language"] == "mr" and len(v["sentences"]) == 3
    assert [s["clips"] for s in v["sentences"]] == [["c1"], ["c1"], ["c2"]]
    assert v["sentences"][0]["text"] == "नमस्कार मित्रांनो." and v["sentences"][0]["start"] == 1.0


def test_the_ai_original_is_shown_only_where_the_ai_changed_the_words():
    edl = edl_two_clips()
    raw = [{"w": "नमस्कार", "s": 1.0}, {"w": "मित्रानो", "s": 1.5}, {"w": "आज", "s": 2.0}, {"w": "आपण", "s": 2.5}, {"w": "बोलू", "s": 3.0}, {"w": "या.", "s": 3.5}]
    v = transcript_view(edl, raw)
    assert v["sentences"][0]["original"] == "नमस्कार मित्रानो" and "original" not in v["sentences"][1]


def test_a_same_length_correction_keeps_every_word_time_and_only_the_touched_clip_is_named():
    edl = edl_two_clips()
    new, hit = apply_transcript(edl, edit_of(edl, "नमस्कार मित्रांनो!"))
    assert hit == ["c1"] and new.transcript[1].w == "मित्रांनो!"
    assert [(w.s, w.e) for w in new.transcript] == [(w.s, w.e) for w in edl.transcript]
    assert new.clips[0].ai.notes == ["edited by a person: transcript"] and new.clips[1].ai is None
    assert new.clips[0].source_in == 0 and new.clips[0].source_out == 10                    # cut points never move


def test_a_longer_or_shorter_sentence_is_spread_over_the_same_span_in_order():
    edl = edl_two_clips()
    new, hit = apply_transcript(edl, edit_of(edl, "नमस्कार मित्रांनो आणि मैत्रिणींनो."))
    ws = [w for w in new.transcript if w.s < 3.9]
    assert [w.w for w in ws][:4] == ["नमस्कार", "मित्रांनो", "आणि", "मैत्रिणींनो."] and len(new.transcript) == len(edl.transcript) + 2
    first = new.transcript[0]
    assert first.s == 1.0 and abs(new.transcript[3].e - 2.0) < 1e-6                         # the sentence still ends where it did
    assert all(a.e <= b.s + 1e-9 for a, b in zip(new.transcript, new.transcript[1:]))


def test_nothing_changes_for_the_same_text_and_a_stale_page_or_bad_text_is_refused():
    edl = edl_two_clips()
    same, hit = apply_transcript(edl, edit_of(edl, "नमस्कार   मित्रांनो."))
    assert same is edl and hit == []
    s = transcript_view(edl)["sentences"][0]
    with pytest.raises(ValueError, match="changed since"):
        apply_transcript(edl, TranscriptEdit(edits=[SentenceEdit(id=s["id"], start=s["start"] + 5, text="x y")]))
    with pytest.raises(ValueError, match="changed since"):
        apply_transcript(edl, TranscriptEdit(edits=[SentenceEdit(id=99, start=1.0, text="x y")]))
    for bad in ("", "   ", "एक\nदोन"):
        with pytest.raises(ValueError, match="some text on one line"):
            apply_transcript(edl, edit_of(edl, bad))


def test_the_edit_schema_is_bounded():
    with pytest.raises(ValueError):
        TranscriptEdit(edits=[])
    with pytest.raises(ValueError):
        SentenceEdit(id=0, start=0, text="x" * 601)
    with pytest.raises(ValueError):
        TranscriptEdit.model_validate({"edits": [{"id": 0, "start": 0, "text": "a", "extra": 1}]})


# --- the API -------------------------------------------------------------------------------------------------------

def test_a_person_reads_and_corrects_the_words_and_only_the_clips_that_use_them_render_again(tmp_path, video_bytes):
    c, app, pipe, job = done_job(tmp_path, video_bytes)
    jid = job["id"]
    v = c.get(f"/api/jobs/{jid}/transcript").json()
    assert v["sentences"] and all(isinstance(s["clips"], list) for s in v["sentences"])
    s = next(x for x in v["sentences"] if x["clips"])
    cid = s["clips"][0]
    before = len(pipe.rendered)
    r = c.put(f"/api/jobs/{jid}/transcript", json={"edits": [{"id": s["id"], "start": s["start"], "text": "सुधारित " + s["text"]}]})
    assert r.status_code == 200 and r.json()["changed"] is True and cid in r.json()["clips"]
    wait_clip(c, jid, cid)
    assert len(pipe.rendered) > before
    edl = load_edl(tmp_path / "data" / "jobs" / jid / "out" / "edl.json")
    assert any(w.w == "सुधारित" for w in edl.transcript) and (tmp_path / "data" / "jobs" / jid / "out" / "edl.original.json").is_file()
    again = c.get(f"/api/jobs/{jid}/transcript").json()
    assert any(x["text"].startswith("सुधारित") for x in again["sentences"])


def test_a_stale_edit_a_bad_edit_and_a_missing_job_are_refused(tmp_path, video_bytes):
    c, app, pipe, job = done_job(tmp_path, video_bytes)
    jid = job["id"]
    s = c.get(f"/api/jobs/{jid}/transcript").json()["sentences"][0]
    assert c.put(f"/api/jobs/{jid}/transcript", json={"edits": [{"id": s["id"], "start": s["start"] + 9, "text": "a b"}]}).status_code == 422
    assert c.put(f"/api/jobs/{jid}/transcript", json={"edits": [{"id": s["id"], "start": s["start"], "text": ""}]}).status_code == 422
    assert c.put(f"/api/jobs/{jid}/transcript", json={"edits": []}).status_code == 422
    assert c.put(f"/api/jobs/{jid}/transcript", json={"edits": [{"id": s["id"], "start": s["start"], "text": s["text"]}]}).json() == {"changed": False, "clips": []}
    assert c.get("/api/jobs/ffffffffffff/transcript").status_code == 404 and c.put("/api/jobs/ffffffffffff/transcript", json={"edits": [{"id": 0, "start": 0, "text": "a"}]}).status_code == 404
