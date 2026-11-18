"""The "watch the full video" banner: data on the clip, a scene in the render, a brand setting, a job option and an editor switch."""
import pytest

from clipcrow.branding import BrandKit, BrandingChoice, BrandError, apply_brand
from clipcrow.compile import banner_words
from clipcrow.edl import Banner
from clipcrow.platform_marks import MARKS
from clipcrow.service.brand import BrandStore
from tests.test_branding import CLIP, build  # noqa: F401
from tests.test_service import video_bytes  # noqa: F401  (fixture)

OWNER = "a" * 32


def test_the_words_follow_the_language_unless_the_brand_wrote_them():
    assert banner_words(Banner(handle="x"), "mr") == "पूर्ण व्हिडीओ पहा:"
    assert banner_words(Banner(handle="x"), "hi") == "पूरा वीडियो देखें:"
    assert banner_words(Banner(handle="x"), "en") == "Watch full video:"
    assert banner_words(Banner(handle="x", text="Full episode:"), "mr") == "Full episode:"


def test_a_banner_needs_a_channel_name_and_clean_text():
    with pytest.raises(Exception):
        Banner(handle="")
    with pytest.raises(Exception):
        Banner(handle="a\x00b")
    assert Banner(handle="  thet.marathi ").handle == "thet.marathi"


def test_every_platform_has_a_mark_and_a_colour():
    assert set(MARKS) >= {"youtube", "instagram", "facebook", "x", "linkedin", "telegram", "tiktok", "whatsapp"}
    assert all(v[1].startswith("#") and "<path" in v[2] or "<circle" in v[2] for v in MARKS.values())


def test_the_banner_scene_has_the_words_the_mark_and_the_name_escaped(make_edl, tmp_path, gsap, fonts_available):
    res = build(make_edl, tmp_path, gsap, {**CLIP, "banner": {"handle": "<b>thet.marathi</b>", "platform": "youtube", "top_px": 1400}})
    assert "banner" in res.scenes
    html = (res.project_dir / "compositions" / "banner.html").read_text()
    assert "पूर्ण व्हिडीओ पहा:" in html and "&lt;b&gt;thet.marathi&lt;/b&gt;" in html and "<b>thet" not in html
    assert "top: 1400px" in html and "<svg" in html and "#FF0000" in html
    idx = (res.project_dir / "index.html").read_text()
    assert 'data-composition-id="banner"' in idx
    assert "transform" not in html.split("</style>")[0].split("#banner {")[1].split("}")[0]     # HyperFrames' lint: a CSS transform on an element GSAP moves (y) is overwritten
    assert 'tl.fromTo("#banner", { opacity: 0, y: 24 }' in html


def test_a_banner_for_the_last_seconds_starts_late_and_a_disabled_one_writes_nothing(make_edl, tmp_path, gsap, fonts_available):
    res = build(make_edl, tmp_path, gsap, {**CLIP, "banner": {"handle": "x", "seconds": 4}})
    html = (res.project_dir / "compositions" / "banner.html").read_text()
    assert 'data-start="6"' in html                                         # a 10 s clip: the last 4 s
    (tmp_path / "b").mkdir()
    res = build(make_edl, tmp_path / "b", gsap, {**CLIP, "banner": {"handle": "x", "enabled": False}})
    assert "banner" not in res.scenes


def test_the_brand_keeps_the_banner_settings_and_checks_them(tmp_path):
    st = BrandStore(tmp_path)
    got = st.set_settings(OWNER, {"banner_handle": "thet.marathi", "banner_platform": "instagram", "banner_seconds": 5, "banner_top_px": 1300})
    assert got["banner_platform"] == "instagram" and st.kit(OWNER).banner_handle == "thet.marathi" and st.kit(OWNER).banner_seconds == 5.0
    for bad in ({"banner_platform": "myspace"}, {"banner_handle": "x" * 41}, {"banner_text": "a\x01"}, {"banner_top_px": 5000}, {"banner_seconds": -1}):
        with pytest.raises(BrandError):
            st.set_settings(OWNER, bad)


def test_apply_brand_adds_the_banner_and_refuses_one_without_a_name(make_edl, tmp_path):
    kit = BrandKit(banner_handle="thet.marathi", banner_platform="youtube", banner_top_px=1380)
    edl = apply_brand(make_edl(clips=[CLIP]), kit, BrandingChoice(banner=True), tmp_path / "job")
    assert edl.clips[0].banner.handle == "thet.marathi" and edl.clips[0].banner.top_px == 1380
    with pytest.raises(BrandError):
        apply_brand(make_edl(clips=[CLIP]), BrandKit(), BrandingChoice(banner=True), tmp_path / "job2")


def test_the_logo_keeps_clear_of_the_banner(make_edl, tmp_path):
    from clipcrow.branding import fit_logo
    from clipcrow.edl import Logo
    lg = Logo(asset="brand-logo", position="br", size_pct=18, opacity=0.9, margin_px=60)
    assert fit_logo(lg, (0.3, 0.2, 0.7, 0.4), None, 1.0).top_px is None                      # nothing in the way at the bottom right
    moved = fit_logo(lg, (0.3, 0.2, 0.7, 0.4), None, 1.0, banner_top=1300)
    wpx = 1080 * 0.18 * moved.shrink
    top = moved.top_px if moved.top_px is not None else 1920 - 440 - wpx
    assert top + wpx <= 1300 - 20 or top >= 1300 + 230                                        # clear of the banner's band


def test_a_job_can_ask_for_the_banner_only_when_the_brand_has_a_name(tmp_path, video_bytes):
    from tests.test_service import client, upload, wait
    c, app = client(tmp_path)
    r = c.post("/api/jobs", data={"rights": "true", "banner": "true"}, files={"file": ("a.mp4", video_bytes, "video/mp4")})
    assert r.status_code == 422 and "channel name" in r.json()["detail"]
    assert c.put("/api/brand", json={"banner_handle": "thet.marathi"}).status_code == 200
    job = wait(c, upload(c, video_bytes, banner="true").json()["id"])
    assert job["status"] == "done" and app.state.store.get(job["id"]).params.banner is True
