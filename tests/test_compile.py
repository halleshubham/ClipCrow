import pytest

from clipcrow.compile import compile_clip
from clipcrow.edl import EDLError


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
    assert build(make_edl, tmp_path, gsap, clip).font_family == "Noto Serif Devanagari"


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
    import clipcrow.compile as c
    orig, c.prepare_footage = c.prepare_footage, lambda *a, **k: None
    try:
        with pytest.raises(EDLError, match="cutout"):
            compile_clip(edl, edl.clips[0], tmp_path / "p", media_root=tmp_path, gsap_js=gsap)
    finally:
        c.prepare_footage = orig


def test_hook_overlay_is_a_banner_below_the_status_bar(make_edl, tmp_path, gsap, fonts_available):
    clip = {"id": "c1", "source_in": 0, "source_out": 10,
            "overlays": [{"kind": "hook", "id": "h", "t_in": 0.3, "text": "हे वाचल्यावर आयुष्य बदलेल"}]}
    html = (build(make_edl, tmp_path, gsap, clip).project_dir / "compositions" / "cards.html").read_text()
    assert 'class="hook clip"' in html and "top:200px" in html and "हे वाचल्यावर" in html


def test_captions_drop_terminal_danda_but_keep_question_marks(make_edl, tmp_path, gsap, fonts_available):
    from tests.conftest import base_edl
    d = base_edl()
    d["transcript"] = [{"w": "आयुष्य।", "s": 1.0, "e": 1.5}, {"w": "का?", "s": 1.5, "e": 2.0}, {"w": "होय.", "s": 2.0, "e": 2.5}]
    from clipcrow.edl import EDL
    edl = EDL.model_validate(d)
    res = compile_clip(edl, edl.clips[0], tmp_path / "p", media_root=tmp_path, gsap_js=gsap, prepare=False)
    html = (res.project_dir / "compositions" / "captions.html").read_text()
    assert ">आयुष्य<" in html and ">का?<" in html and ">होय<" in html


# --- fonts: Noto Serif Devanagari is the default, with a Latin companion only when needed ------------

def test_default_caption_font_is_noto_serif_devanagari():
    from clipcrow.edl import CaptionStyle
    assert CaptionStyle().font_family == "Noto Serif Devanagari"


def test_devanagari_only_text_ships_just_the_default_font(make_edl, tmp_path, gsap, fonts_available):
    res = build(make_edl, tmp_path, gsap, CLIP)
    shipped = sorted(p.name for p in (res.project_dir / "assets" / "fonts").iterdir())
    assert shipped == ["NotoSerifDevanagari-Bold.ttf"]
    html = (res.project_dir / "compositions" / "captions.html").read_text()
    assert 'font-family: "Noto Serif Devanagari", sans-serif' in html


def test_latin_words_get_a_noto_serif_companion_in_the_font_stack(make_edl, tmp_path, gsap, fonts_available):
    """Noto Serif Devanagari has no Latin letters: 'WhatsApp' or the x in '10x' would fall back to whatever
    the render machine has. The companion is shipped and named in the stack."""
    clip = {"id": "c1", "source_in": 0, "source_out": 10,
            "overlays": [{"kind": "stat_pill", "id": "p", "t_in": 3, "text": "10x"}]}
    res = build(make_edl, tmp_path, gsap, clip)
    shipped = sorted(p.name for p in (res.project_dir / "assets" / "fonts").iterdir())
    assert shipped == ["NotoSerif-Bold.ttf", "NotoSerifDevanagari-Bold.ttf"]
    cards = (res.project_dir / "compositions" / "cards.html").read_text()
    assert 'font-family: "Noto Serif Devanagari", "Noto Serif", sans-serif' in cards
    assert '@font-face { font-family: "Noto Serif"' in cards


def test_the_devanagari_font_really_lacks_latin_letters(fonts_available):
    """The reason the companion exists; if Noto ever adds Latin this test says the companion can go."""
    ft = pytest.importorskip("fontTools.ttLib")
    from clipcrow import fonts
    cmap = ft.TTFont(str(fonts.resolve_font("Noto Serif Devanagari", 700))).getBestCmap()
    assert ord("क") in cmap and ord("₹") in cmap
    if ord("x") in cmap:
        # The Google Fonts build (npm) already carries Latin; the Debian fonts-noto-core build does not.
        # The companion stays: we cannot know which build a deployment installs, and it is harmless.
        pytest.skip("this Noto Serif Devanagari build already has Latin letters; the companion is redundant here")
