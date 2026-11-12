"""Face-to-camera mode: push-ins, the matte cache, and what happens when there is no matte."""
import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from clipcrow import pipeline, render
from clipcrow.compile import compile_clip
from clipcrow.edl import EDL, Asset, Layout, PunchIn
from clipcrow.punch import plan_punch_ins, sentence_spans
from clipcrow.render import RenderError
from clipcrow.service.editor import ClipEdit, apply_edit, clip_view
from tests.conftest import base_edl
from tests.test_planner import SRC, make_transcript, scripted
from clipcrow.planner import PlannerOptions, plan_edl

SRC_META = {"uri": "src.mp4", "duration": 600, "width": 1920, "height": 1080, "fps": 30}


def edl_of(layout=None, source=None, **clip):
    c = {"id": "c1", "source_in": 10, "source_out": 40, **clip}
    if layout:
        c["layout"] = layout
    return EDL.model_validate(base_edl(clips=[c], source=source or SRC_META))


# --- the model -----------------------------------------------------------------------------------

def test_punch_ins_must_be_in_order_inside_the_clip_and_in_range():
    ok = [{"t": 3, "duration": 2}, {"t": 6, "duration": 2, "scale": 1.2}]
    assert len(edl_of({"punch_ins": ok}).clips[0].layout.punch_ins) == 2
    with pytest.raises(ValidationError, match="overlap"):
        edl_of({"punch_ins": [{"t": 3, "duration": 4}, {"t": 5, "duration": 2}]})
    with pytest.raises(ValidationError, match="past the end"):
        edl_of({"punch_ins": [{"t": 29, "duration": 3}]})
    for bad in ({"t": 1, "duration": 2, "scale": 2.5}, {"t": 1, "duration": 0.1}, {"t": -1, "duration": 2}):
        with pytest.raises(ValidationError):
            PunchIn(**bad)


# --- timing is code ------------------------------------------------------------------------------

def test_punch_ins_start_at_sentences_away_from_the_hook_and_the_end():
    spans = [(0.0, 2.0), (2.2, 5.0), (5.5, 8.0), (8.4, 9.0), (9.5, 13.0), (13.5, 29.0)]
    plan = plan_punch_ins(spans, 30.0)
    assert plan and plan[0]["t"] >= 2.6                                       # the hook is on screen first
    for a, b in zip(plan, plan[1:]):
        assert b["t"] >= a["t"] + a["duration"] + 1.0                         # the picture rests between pushes
    assert all(p["t"] + p["duration"] <= 30.0 - 0.3 for p in plan)
    assert all(1.5 <= p["duration"] <= 4.0 for p in plan)
    assert [p["scale"] for p in plan[:2]] == [1.12, 1.18]
    assert all(s[0] != 8.4 for s in [(p["t"], 0) for p in plan])              # a 0.6 s sentence is too short to push on


def test_punch_ins_are_capped_and_a_short_clip_gets_none():
    many = [(i * 4.0, i * 4.0 + 3.0) for i in range(40)]
    assert len(plan_punch_ins(many, 170.0)) == 6
    assert plan_punch_ins([(0.0, 3.0)], 3.5) == []


def test_sentence_spans_split_on_punctuation_and_pauses():
    from clipcrow.edl import Word
    words = [Word(w="एक", s=10.0, e=10.5), Word(w="दोन.", s=10.5, e=11.0), Word(w="तीन", s=11.2, e=11.7),
             Word(w="चार", s=13.0, e=13.5), Word(w="पाच।", s=13.5, e=14.0)]
    spans = sentence_spans(words, 10.0, 14.0)
    assert [(round(a, 2), round(b, 2)) for a, b in spans] == [(0.0, 1.0), (1.2, 1.7), (3.0, 4.0)]


def test_the_planner_adds_push_ins_only_when_asked():
    tr = make_transcript()
    off = plan_edl(tr, SRC, scripted(), PlannerOptions(target_clips=1, workers=1)).edl
    on = plan_edl(tr, SRC, scripted(), PlannerOptions(target_clips=1, workers=1, immersive=True)).edl
    assert off.clips[0].layout.punch_ins == []
    pi = on.clips[0].layout.punch_ins
    assert pi and all(p.t >= 2.6 for p in pi)
    assert "punch" not in " ".join(on.clips[0].ai.decided)                    # the AI did not choose these: code did


# --- the compiled page ---------------------------------------------------------------------------

def test_the_push_scales_the_footage_the_text_behind_and_the_cutout_together(tmp_path, gsap, fonts_available):
    edl = edl_of({"mode": "cutout_behind", "cutout_uri": "m.webm",
                  "punch_ins": [{"t": 3, "duration": 2, "scale": 1.15}, {"t": 6, "duration": 2.5, "scale": 1.2, "anchor_y": 0.35}]},
                 overlays=[{"kind": "text_behind", "id": "b", "t_in": 3, "text": "ज्ञान"}])
    compile_clip(edl, edl.clips[0], tmp_path / "p", media_root=tmp_path, gsap_js=gsap, prepare=False)
    html = (tmp_path / "p" / "index.html").read_text()
    assert 'const stage = ["#footage", "#behind", "#cutout"]' in html
    assert 'scale: 1.15, duration: 2' in html and 'transformOrigin: "50% 35%"' in html
    assert html.count("tl.set(stage") == 2                                    # each push snaps back


def test_no_push_ins_no_script(tmp_path, gsap, fonts_available):
    edl = edl_of()
    compile_clip(edl, edl.clips[0], tmp_path / "p", media_root=tmp_path, gsap_js=gsap, prepare=False)
    assert "stage" not in (tmp_path / "p" / "index.html").read_text()


def test_stacked_and_blurred_framings_drop_the_push_ins():
    from clipcrow.framing import apply_framing
    from tests.test_multispeaker import LEFT, RIGHT, TwoFaces, talkers_video
    import tempfile
    v = talkers_video(Path(tempfile.mkdtemp()) / "t.avi")
    edl = EDL.model_validate(base_edl(clips=[{"id": "c1", "source_in": 0, "source_out": 12,
                                              "layout": {"punch_ins": [{"t": 3, "duration": 2}]}}]))
    out = apply_framing(edl, str(v), detector=TwoFaces([LEFT, RIGHT]), multi="split")
    assert out.clips[0].layout.framing == "split" and out.clips[0].layout.punch_ins == []
    out = apply_framing(edl, str(v), detector=TwoFaces([]), multi="switch")      # nobody to follow: blurred fit
    assert out.clips[0].layout.framing == "fit_blur" and out.clips[0].layout.punch_ins == []


# --- the matte -----------------------------------------------------------------------------------

def lavfi_video(path: Path, seconds=3):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc=s=1280x720:r=30:d={seconds}",
                    "-pix_fmt", "yuv420p", str(path)], check=True)


def test_the_matte_is_made_once_per_footage_and_again_when_the_cut_changes(tmp_path, gsap, fonts_available):
    lavfi_video(tmp_path / "src.mp4")
    meta = {"uri": "src.mp4", "duration": 3, "width": 1280, "height": 720, "fps": 30}
    calls = []

    def matte(src, dest):
        calls.append(src)
        Path(dest).write_bytes(b"webm")

    def go(name, start):
        edl = edl_of({"mode": "cutout_behind"}, source=meta, source_in=start, source_out=start + 1.5)
        compile_clip(edl, edl.clips[0], tmp_path / name, media_root=tmp_path, gsap_js=gsap, matte_fn=matte,
                     matte_cache=tmp_path / "cache")
        assert (tmp_path / name / "assets" / "cutout.webm").read_bytes() == b"webm"

    go("a", 0.0); go("b", 0.0)
    assert len(calls) == 1                                                    # a text-only edit does not redo the slow step
    go("c", 0.5)
    assert len(calls) == 2                                                    # a different cut is different footage
    assert len(list((tmp_path / "cache").glob("*.webm"))) == 2


def test_without_a_matte_the_clip_still_renders_and_says_what_was_skipped(tmp_path, monkeypatch, gsap, fonts_available):
    lavfi_video(tmp_path / "src.mp4")
    meta = {"uri": "src.mp4", "duration": 3, "width": 1280, "height": 720, "fps": 30}
    edl = edl_of({"mode": "cutout_behind"}, source=meta, source_in=0, source_out=2,
                 overlays=[{"kind": "text_behind", "id": "b", "t_in": 0.5, "text": "ज्ञान"},
                           {"kind": "hook", "id": "hook", "t_in": 0.2, "text": "हुक"}],
                 ai={"decided": ["cut points"]})
    edl = edl.model_copy(update={"assets": [Asset(id="cutout", kind="video", origin="matte", ai="processed", tool="u2net_human_seg")]})

    def no_model(src, dest, **kw):
        raise RenderError("remove-background failed (exit 1):\nmodel download blocked")
    monkeypatch.setattr(pipeline.matte, "remove_background", no_model)
    monkeypatch.setattr(render, "check", lambda project: {})
    monkeypatch.setattr(render, "check_problems", lambda r: [])

    def fake_render(project, out, **kw):
        Path(out).write_bytes(b"mp4")
        return Path(out)
    monkeypatch.setattr(render, "render", fake_render)
    out, compiled = pipeline.render_clip(edl, "c1", tmp_path / "out.mp4", workdir=tmp_path / "w", media_root=tmp_path,
                                         gsap_js=gsap)
    assert out.is_file()
    assert compiled.warnings and "text behind the speaker was skipped" in compiled.warnings[0]
    assert "model download blocked" in compiled.warnings[0]
    html = (compiled.project_dir / "index.html").read_text()
    assert "cutout" not in html and not (compiled.project_dir / "compositions" / "behind.html").exists()
    side = Path(str(out) + ".ai.json").read_text()
    assert "skipped" in side                                                  # the provenance record says so too
    assert json.loads(side)["ai_assets"]["processed"] == []                   # and does not list a matte that was never applied


def test_other_render_errors_are_not_swallowed(tmp_path, monkeypatch, gsap, fonts_available):
    lavfi_video(tmp_path / "src.mp4")
    meta = {"uri": "src.mp4", "duration": 3, "width": 1280, "height": 720, "fps": 30}
    edl = edl_of({}, source=meta, source_in=0, source_out=2)
    monkeypatch.setattr(render, "check", lambda project: {})
    monkeypatch.setattr(render, "check_problems", lambda r: [])

    def boom(project, out, **kw):
        raise RenderError("chrome crashed")
    monkeypatch.setattr(render, "render", boom)
    with pytest.raises(RenderError, match="chrome crashed"):
        pipeline.render_clip(edl, "c1", tmp_path / "o.mp4", workdir=tmp_path / "w", media_root=tmp_path, gsap_js=gsap)


# --- the editor ----------------------------------------------------------------------------------

def test_the_editor_turns_push_ins_on_and_off_and_moves_them_with_a_trim():
    words = [{"w": f"शब्द{i}" + ("." if i % 6 == 5 else ""), "s": 10 + i * 0.5, "e": 10.4 + i * 0.5} for i in range(80)]
    edl = EDL.model_validate({**edl_of().model_dump(exclude_none=True), "transcript": words})
    on, _ = apply_edit(edl, "c1", ClipEdit(punch_ins=True), source_duration=600)
    assert on.clips[0].layout.punch_ins and clip_view(on, "c1", source_duration=600)["punch_ins"] is True
    before = [p.t for p in on.clips[0].layout.punch_ins]
    later, _ = apply_edit(on, "c1", ClipEdit(source_in=13.0), source_duration=600)
    shifted = [p.t for p in later.clips[0].layout.punch_ins]
    assert shifted and all(abs((t + 3.0) - b) < 1e-6 for t, b in zip(shifted, [b for b in before if b - 3.0 >= 0]))
    off, _ = apply_edit(on, "c1", ClipEdit(punch_ins=False), source_duration=600)
    assert off.clips[0].layout.punch_ins == []
    same, _ = apply_edit(off, "c1", ClipEdit(punch_ins=False), source_duration=600)
    assert same is off


def test_push_ins_are_refused_on_a_blurred_fit():
    edl = edl_of({"framing": "fit_blur"})
    with pytest.raises(ValueError, match="do not suit"):
        apply_edit(edl, "c1", ClipEdit(punch_ins=True), source_duration=600)
