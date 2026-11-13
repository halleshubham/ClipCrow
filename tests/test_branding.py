import subprocess

import pytest
from pydantic import ValidationError

from clipcrow import branding
from clipcrow.branding import BrandError, BrandingChoice, BrandKit, apply_brand, cover_text
from clipcrow.compile import compile_clip, cover_font_size
from clipcrow.edl import EDL

CLIP = {"id": "c1", "source_in": 0, "source_out": 10, "title": "तुमची झोप",
        "overlays": [{"kind": "hook", "id": "hook", "t_in": 0.3, "text": "झोप का महत्त्वाची आहे?"}]}


def png(path, size="200x100"):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c=red:s={size}", "-frames:v", "1", str(path)], check=True)
    return path


def mp4(path, secs=2, audio=True, size="640x360", color="blue"):
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c={color}:s={size}:r=30:d={secs}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=f=440:d={secs}"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p"] + (["-c:a", "aac"] if audio else []) + [str(path)]
    subprocess.run(cmd, check=True)
    return path


def with_assets(make_edl, clip, **extra):
    return make_edl(clips=[clip], assets=[
        {"id": "brand-logo", "kind": "image", "origin": "upload", "uri": "brand/logo.png"},
        {"id": "brand-outro", "kind": "video", "origin": "upload", "uri": "brand/outro.mp4"}], **extra)


# --- the model --------------------------------------------------------------------------------------------------

def test_cover_logo_outro_must_point_at_the_right_kind_of_asset(make_edl):
    with pytest.raises(ValidationError, match="logo needs a image asset"):
        with_assets(make_edl, {**CLIP, "logo": {"asset": "brand-outro"}})
    with pytest.raises(ValidationError, match="outro needs a video asset"):
        with_assets(make_edl, {**CLIP, "outro": {"asset": "brand-logo"}})
    with pytest.raises(ValidationError, match="not in the EDL"):
        make_edl(clips=[{**CLIP, "logo": {"asset": "nope"}}])
    with_assets(make_edl, {**CLIP, "logo": {"asset": "brand-logo"}, "outro": {"asset": "brand-outro"}})


def test_cover_bounds(make_edl):
    with pytest.raises(ValidationError):
        make_edl(clips=[{**CLIP, "cover": {"text": "x" * 81}}])
    with pytest.raises(ValidationError):
        make_edl(clips=[{**CLIP, "cover": {"text": "x", "duration_ms": 50}}])
    with pytest.raises(ValidationError, match="shorter than half"):
        make_edl(clips=[{"id": "c1", "source_in": 0, "source_out": 2, "cover": {"text": "x", "duration_ms": 1100}}])
    with pytest.raises(ValidationError):
        make_edl(clips=[{**CLIP, "cover": {"text": "x", "color": "red"}}])


def test_cover_text_prefers_the_hook_then_the_title():
    from clipcrow.edl import Clip
    assert cover_text(Clip.model_validate(CLIP)) == "झोप का महत्त्वाची आहे?"
    assert cover_text(Clip.model_validate({**CLIP, "overlays": []})) == "तुमची झोप"
    long = Clip.model_validate({**CLIP, "overlays": [], "title": "word " * 40})
    assert len(cover_text(long)) <= 80 and not cover_text(long).endswith(" ")


def test_font_size_is_a_pure_function_of_length():
    sizes = [cover_font_size("a" * n) for n in (5, 20, 40, 60, 80)]
    assert sizes == sorted(sizes, reverse=True) and len(set(sizes)) == 5
    assert cover_font_size("a" * 20) == cover_font_size("b" * 20)


# --- the compiler -----------------------------------------------------------------------------------------------

def build(make_edl, tmp_path, gsap, clip):
    edl = with_assets(make_edl, clip)
    (tmp_path / "brand").mkdir(exist_ok=True)
    png(tmp_path / "brand" / "logo.png")
    return compile_clip(edl, edl.clips[0], tmp_path / "p", media_root=tmp_path, gsap_js=gsap, asset_root=tmp_path, prepare=False)


def test_cover_scene_is_top_most_and_solid_from_frame_zero(make_edl, tmp_path, gsap, fonts_available):
    res = build(make_edl, tmp_path, gsap, {**CLIP, "cover": {"text": "झोप का महत्त्वाची आहे?", "duration_ms": 300}})
    assert "cover" in res.scenes
    idx = (res.project_dir / "index.html").read_text()
    assert 'data-composition-id="cover"' in idx and "z-index:9" in idx
    html = (res.project_dir / "compositions" / "cover.html").read_text()
    assert 'data-start="0" data-duration="0.3"' in html
    assert "fromTo" not in html                      # no fade-in: frame 0 already shows the title
    assert '<span class="ac">आहे?</span>' in html


def test_cover_hostile_text_is_escaped(make_edl, tmp_path, gsap, fonts_available):
    res = build(make_edl, tmp_path, gsap, {**CLIP, "cover": {"text": '</div><script>alert(1)</script>'}})
    html = (res.project_dir / "compositions" / "cover.html").read_text()
    assert "<script>alert(1)" not in html and "&lt;script&gt;" in html


def test_disabled_cover_and_logo_write_no_scene(make_edl, tmp_path, gsap, fonts_available):
    res = build(make_edl, tmp_path, gsap, {**CLIP, "cover": {"text": "x", "enabled": False}, "logo": {"asset": "brand-logo", "enabled": False}})
    assert "cover" not in res.scenes and "logo" not in res.scenes
    assert not (res.project_dir / "compositions" / "cover.html").exists()


def test_logo_starts_after_the_cover_and_sits_in_the_corner(make_edl, tmp_path, gsap, fonts_available):
    res = build(make_edl, tmp_path, gsap, {**CLIP, "cover": {"text": "x", "duration_ms": 400},
                                           "logo": {"asset": "brand-logo", "position": "bl", "size_pct": 20}})
    html = (res.project_dir / "compositions" / "logo.html").read_text()
    assert 'data-start="0.4"' in html and "left:60px" in html and "bottom:440px" in html and "width: 216px" in html
    idx = (res.project_dir / "index.html").read_text()
    assert idx.index('id="logo"') < idx.index('id="cover"')


def test_logo_must_be_a_picture_inside_the_asset_root(make_edl, tmp_path, gsap, fonts_available):
    from clipcrow.edl import EDLError
    edl = make_edl(clips=[{**CLIP, "logo": {"asset": "l"}}],
                   assets=[{"id": "l", "kind": "image", "origin": "upload", "uri": "../etc/passwd"}])
    with pytest.raises(EDLError, match="not a supported logo type|outside"):
        compile_clip(edl, edl.clips[0], tmp_path / "p", media_root=tmp_path, gsap_js=gsap, asset_root=tmp_path)
    edl = make_edl(clips=[{**CLIP, "logo": {"asset": "l"}}],
                   assets=[{"id": "l", "kind": "image", "origin": "upload", "uri": "../logo.png"}])
    with pytest.raises(EDLError, match="outside"):
        compile_clip(edl, edl.clips[0], tmp_path / "p", media_root=tmp_path, gsap_js=gsap, asset_root=tmp_path)


# --- the brand kit applied to an EDL -----------------------------------------------------------------------------

def test_apply_brand_copies_files_and_sets_every_clip(make_edl, tmp_path):
    kit = BrandKit(logo=png(tmp_path / "up-logo.png"), outro=mp4(tmp_path / "up-outro.mp4"), logo_position="br", cover_ms=300)
    out = tmp_path / "job"
    edl = apply_brand(make_edl(clips=[CLIP]), kit, BrandingChoice(cover=True, logo=True, outro=True), out)
    c = edl.clips[0]
    assert c.cover.text == "झोप का महत्त्वाची आहे?" and c.cover.duration_ms == 300
    assert c.logo.position == "br" and c.outro.max_seconds == 6
    assert (out / "brand" / "logo.png").is_file() and (out / "brand" / "outro.mp4").is_file()
    assert {a.id for a in edl.assets} == {"brand-logo", "brand-outro"}
    again = apply_brand(edl, kit, BrandingChoice(logo=True), out)              # idempotent: no duplicate assets
    assert [a.id for a in again.assets].count("brand-logo") == 1


def test_apply_brand_without_the_file_is_a_sentence_not_a_crash(make_edl, tmp_path):
    with pytest.raises(BrandError, match="No logo"):
        apply_brand(make_edl(clips=[CLIP]), BrandKit(), BrandingChoice(logo=True), tmp_path)
    with pytest.raises(BrandError, match="No ending video"):
        apply_brand(make_edl(clips=[CLIP]), BrandKit(), BrandingChoice(outro=True), tmp_path)


def test_cover_alone_needs_no_kit(make_edl, tmp_path):
    edl = apply_brand(make_edl(clips=[CLIP]), BrandKit(), BrandingChoice(cover=True), tmp_path)
    assert edl.clips[0].cover and not edl.clips[0].logo and not edl.assets


# --- FFmpeg ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("outro_audio", [True, False])
@pytest.mark.parametrize("main_audio", [True, False])
def test_append_outro_joins_normalises_and_keeps_one_audio_stream(tmp_path, outro_audio, main_audio):
    main = mp4(tmp_path / "m.mp4", 2, audio=main_audio, size="540x960", color="green")
    tail = mp4(tmp_path / "o.mp4", 3, audio=outro_audio, size="640x360")
    out = branding.append_outro(main, tail, tmp_path / "j.mp4", width=540, height=960, max_seconds=1.5)
    info = branding.probe_media(out)
    assert (info["width"], info["height"]) == (540, 960) and info["has_audio"]
    assert abs(info["duration"] - 3.5) < 0.2


def test_append_outro_rejects_a_file_without_picture(tmp_path):
    main = mp4(tmp_path / "m.mp4", 1)
    audio = tmp_path / "a.m4a"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=d=1", str(audio)], check=True)
    with pytest.raises(BrandError):
        branding.append_outro(main, audio, tmp_path / "j.mp4", width=540, height=960)


def test_extract_cover_is_frame_zero(tmp_path):
    out = branding.extract_cover(mp4(tmp_path / "m.mp4", 1), tmp_path / "m.cover.png")
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
