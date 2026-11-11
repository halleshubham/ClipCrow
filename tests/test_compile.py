import pytest

from reelcore.compile import compile_clip
from reelcore.edl import EDLError


def build(make_edl, tmp_path, gsap, clip, **kw):
    edl = make_edl(clips=[clip], **kw)
    return compile_clip(edl, edl.clips[0], tmp_path / "p", media_root=tmp_path, gsap_js=gsap, prepare=False)


CLIP = {"id": "c1", "source_in": 0, "source_out": 10}


def test_writes_a_complete_project(make_edl, tmp_path, gsap, fonts_available):
    res = build(make_edl, tmp_path, gsap, CLIP)
    p = res.project_dir
    for f in ("index.html", "compositions/captions.html", "assets/gsap.min.js", "hyperframes.json"):
        assert (p / f).is_file(), f
    assert res.scenes == ["captions"]
    assert list((p / "assets" / "fonts").glob("*Devanagari*"))
    idx = (p / "index.html").read_text()
    assert 'src="assets/gsap.min.js"' in idx and "cdn" not in idx


def test_no_scene_is_written_when_it_has_nothing_to_show(make_edl, tmp_path, gsap, fonts_available):
    clip = {**CLIP, "captions": {"enabled": False}}
    res = build(make_edl, tmp_path, gsap, clip)
    assert res.scenes == []
    assert not (res.project_dir / "compositions" / "captions.html").exists()


def test_latin_font_is_swapped_for_devanagari_text(make_edl, tmp_path, gsap, fonts_available):
    clip = {**CLIP, "captions": {"font_family": "Noto Sans"}}
    assert build(make_edl, tmp_path, gsap, clip).font_family == "Noto Sans Devanagari"


def test_hostile_text_is_escaped(make_edl, tmp_path, gsap, fonts_available):
    evil = '</div><script>alert(1)</script>"onmouseover="x'
    clip = {"id": "c1", "source_in": 0, "source_out": 10,
            "overlays": [{"kind": "lower_third", "id": "lt", "t_in": 1, "title": evil, "subtitle": evil}]}
    res = build(make_edl, tmp_path, gsap, clip)
    html = (res.project_dir / "compositions" / "cards.html").read_text()
    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;alert(1)" in html
    assert 'onmouseover="x' not in html.replace("&quot;onmouseover=&quot;x", "")


def test_overlay_anchored_to_a_word_is_placed_at_that_word(make_edl, tmp_path, gsap, fonts_available):
    clip = {"id": "c1", "source_in": 0.5, "source_out": 10,
            "overlays": [{"kind": "stat_pill", "id": "p", "trigger_word_index": 3, "text": "x"}]}
    html = (build(make_edl, tmp_path, gsap, clip).project_dir / "compositions" / "cards.html").read_text()
    assert 'data-start="3"' in html          # word 3 starts at 3.5 s; clip starts at 0.5 s


def test_overlay_outside_the_clip_is_an_error(make_edl, tmp_path, gsap, fonts_available):
    clip = {"id": "c1", "source_in": 0, "source_out": 5,
            "overlays": [{"kind": "stat_pill", "id": "p", "t_in": 6, "text": "x"}]}
    with pytest.raises(EDLError, match="outside"):
        build(make_edl, tmp_path, gsap, clip)


def test_trigger_word_outside_the_transcript_is_an_error(make_edl, tmp_path, gsap, fonts_available):
    clip = {"id": "c1", "source_in": 0, "source_out": 5,
            "overlays": [{"kind": "stat_pill", "id": "p", "trigger_word_index": 99, "text": "x"}]}
    with pytest.raises(EDLError, match="transcript"):
        build(make_edl, tmp_path, gsap, clip)


def test_text_behind_scene_sits_between_footage_and_cutout(make_edl, tmp_path, gsap, fonts_available):
    clip = {"id": "c1", "source_in": 0, "source_out": 10, "layout": {"mode": "cutout_behind"},
            "overlays": [{"kind": "text_behind", "id": "w", "t_in": 1, "text": "आयुष्य"}]}
    idx = (build(make_edl, tmp_path, gsap, clip).project_dir / "index.html").read_text()
    import re
    z = lambda name: int(re.search(rf'<(?:div|video) id="{name}"[^>]*z-index:(\d+)', idx).group(1))
    assert z("footage") < z("behind") < z("cutout") < z("captions")


def test_missing_gsap_is_a_clear_error(make_edl, tmp_path, fonts_available):
    edl = make_edl(clips=[CLIP])
    with pytest.raises(EDLError, match="gsap"):
        compile_clip(edl, edl.clips[0], tmp_path / "p", media_root=tmp_path, gsap_js=None, prepare=False)


def test_media_must_live_under_the_media_root(make_edl, tmp_path, gsap, fonts_available):
    outside = tmp_path / "outside.mp4"; outside.write_bytes(b"x")
    root = tmp_path / "root"; root.mkdir()
    for uri in ("../outside.mp4", str(outside), "https://example.com/a.mp4"):
        edl = make_edl(source={"uri": uri, "duration": 20, "width": 1080, "height": 1920}, clips=[CLIP])
        with pytest.raises(EDLError):
            compile_clip(edl, edl.clips[0], tmp_path / "p", media_root=root, gsap_js=gsap)


def test_cutout_layout_without_cutout_or_matte_is_an_error(make_edl, tmp_path, gsap, fonts_available):
    (tmp_path / "src.mp4").write_bytes(b"x")
    clip = {"id": "c1", "source_in": 0, "source_out": 5, "layout": {"mode": "cutout_behind"}}
    edl = make_edl(clips=[clip])
    import reelcore.compile as c
    orig, c.prepare_footage = c.prepare_footage, lambda *a, **k: None
    try:
        with pytest.raises(EDLError, match="cutout"):
            compile_clip(edl, edl.clips[0], tmp_path / "p", media_root=tmp_path, gsap_js=gsap)
    finally:
        c.prepare_footage = orig
