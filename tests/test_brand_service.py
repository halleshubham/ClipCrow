import subprocess

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("multipart")
from fastapi.testclient import TestClient

from clipcrow.branding import BrandError
from clipcrow.edl import load_edl
from clipcrow.service.brand import BrandStore, sniff_image
from clipcrow.service.editor import ClipEdit, apply_edit, clip_view
from tests.test_accounts import app_client, signup
from tests.test_branding import mp4, png
from tests.test_service import client, upload, video_bytes, wait  # noqa: F401  (fixtures)

OWNER = "a" * 32


@pytest.fixture
def store(tmp_path):
    return BrandStore(tmp_path)


def test_sniff_goes_by_the_first_bytes_not_the_name():
    assert sniff_image(b"\x89PNG\r\n\x1a\n....") == ".png" and sniff_image(b"\xff\xd8\xff\xe0") == ".jpg"
    assert sniff_image(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == ".webp"
    assert sniff_image(b"<svg xmlns='http://www.w3.org/2000/svg'/>") is None and sniff_image(b"GIF89a") is None


def test_owner_ids_cannot_escape_the_brand_folder(store):
    for bad in ("../x", "a/b", "", "A" * 32, "x" * 31):
        with pytest.raises(KeyError):
            store.describe(bad)


def test_a_logo_is_checked_and_replaced(store, tmp_path):
    d = store.put_logo(OWNER, png(tmp_path / "l.png").read_bytes())
    assert d["logo"] and store.logo_path(OWNER).suffix == ".png"
    for bad, msg in ((b"", "empty"), (b"<svg onload=alert(1)/>", "SVG"), (b"\x89PNG\r\n\x1a\n" + b"x" * 50, "pixels"),
                     (png(tmp_path / "big.png").read_bytes() + b"0" * (3 * 1024 * 1024), "at most")):
        with pytest.raises(BrandError, match=msg):
            store.put_logo(OWNER, bad)
    assert store.logo_path(OWNER).is_file()                      # a refused upload does not remove the old logo
    store.delete(OWNER, "logo")
    assert store.logo_path(OWNER) is None


def test_an_outro_is_converted_to_the_canonical_shape(store, tmp_path):
    d = store.put_outro(OWNER, mp4(tmp_path / "o.mp4", 2, audio=False, size="640x360"))
    assert d["outro"] and 1.5 < d["outro_seconds"] < 2.5
    from clipcrow.branding import probe_media
    info = probe_media(store.outro_path(OWNER))
    assert (info["width"], info["height"]) == (1080, 1920) and info["has_audio"]


def test_outros_without_picture_or_too_long_are_refused(store, tmp_path):
    audio = tmp_path / "a.m4a"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=d=2", str(audio)], check=True)
    with pytest.raises(BrandError, match="no picture"):
        store.put_outro(OWNER, audio)
    junk = tmp_path / "j.mp4"
    junk.write_bytes(b"not a video")
    with pytest.raises(BrandError):
        store.put_outro(OWNER, junk)
    with pytest.raises(BrandError, match="between 0.5 and 20"):
        store.put_outro(OWNER, mp4(tmp_path / "long.mp4", 22, size="64x64"))
    assert store.outro_path(OWNER) is None


def test_settings_are_validated(store):
    assert store.settings(OWNER)["logo_position"] == "tr"
    store.set_settings(OWNER, {"logo_position": "bl", "cover_ms": 400, "cover_accent": "#00ff00", "default_cover": True})
    s = store.settings(OWNER)
    assert (s["logo_position"], s["cover_ms"], s["cover_accent"], s["default_cover"]) == ("bl", 400, "#00ff00", True)
    for bad in ({"logo_position": "middle"}, {"cover_ms": 5000}, {"cover_ms": True}, {"cover_color": "red"},
                {"nope": 1}, {"default_logo": "yes"}, {"logo_opacity": 0}, {"cover_background": "video"}):
        with pytest.raises(BrandError):
            store.set_settings(OWNER, bad)
    assert store.settings(OWNER)["logo_position"] == "bl"        # nothing half-applied


def test_a_corrupt_settings_file_falls_back_to_defaults(store):
    store.set_settings(OWNER, {"cover_ms": 400})
    (store.root / OWNER / "settings.json").write_text('{"cover_ms": "soon", "logo_position": 7}')
    assert store.settings(OWNER)["cover_ms"] == 250 and store.settings(OWNER)["logo_position"] == "tr"


# --- the API -------------------------------------------------------------------------------------------------------

def test_brand_api_roundtrip_and_a_job_that_uses_it(tmp_path, video_bytes):  # noqa: F811
    c, app = client(tmp_path)
    assert c.get("/api/brand").json()["logo"] is False
    r = c.post("/api/jobs", data={"rights": "true", "logo": "1"}, files={"file": ("a.mp4", video_bytes, "video/mp4")})
    assert r.status_code == 422 and "logo" in r.json()["detail"]                 # asked for a logo it does not have
    assert app.state.store.list() == []

    assert c.post("/api/brand/logo", files={"file": ("l.svg", b"<svg/>", "image/svg+xml")}).status_code == 422
    assert c.post("/api/brand/logo", files={"file": ("l.png", png(tmp_path / "l.png").read_bytes(), "image/png")}).json()["logo"]
    got = c.get("/api/brand/logo/file")
    assert got.status_code == 200 and got.headers["content-type"] == "image/png"
    assert c.post("/api/brand/outro", files={"file": ("o.mp4", mp4(tmp_path / "o.mp4", 2, size="320x180").read_bytes(), "video/mp4")}).json()["outro"]
    assert c.put("/api/brand", json={"logo_position": "bl"}).json()["settings"]["logo_position"] == "bl"
    assert c.put("/api/brand", json={"logo_position": "x"}).status_code == 422

    job = wait(c, upload(c, video_bytes, cover="1", logo="1", outro="1").json()["id"])
    assert job["status"] == "done", job["error"]
    edl = load_edl(app.state.store.edl_path(job["id"]))
    ck = edl.clips[0]
    assert ck.cover.text and ck.logo.position == "bl" and ck.outro.asset == "brand-outro"
    assert {a.id for a in edl.assets} >= {"brand-logo", "brand-outro"}
    out = app.state.store.dir(job["id"]) / "out" / "brand"
    assert (out / "logo.png").is_file() and (out / "outro.mp4").is_file()
    assert job["clips"][0]["has_cover"] is True

    c.delete("/api/brand/logo")                                                  # changing the kit leaves the job as it was
    assert (out / "logo.png").is_file()
    assert c.delete("/api/brand/everything").status_code == 404


def test_the_cover_image_is_served_when_it_exists(tmp_path, video_bytes):  # noqa: F811
    c, app = client(tmp_path)
    job = wait(c, upload(c, video_bytes, cover="1").json()["id"])
    url = f"/api/jobs/{job['id']}/clips/c1/cover"
    assert c.get(url).status_code == 404                                        # the fake renderer makes no PNG
    (app.state.store.dir(job["id"]) / "out" / "c1.cover.png").write_bytes(b"\x89PNG\r\n\x1a\nxx")
    r = c.get(url, params={"download": 1})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and "ep12-c1-cover.png" in r.headers["content-disposition"]
    assert c.get(f"/api/jobs/{job['id']}/clips/c1x/cover").status_code == 404


def test_brand_kits_and_covers_belong_to_their_owner(tmp_path, video_bytes):  # noqa: F811
    c, accounts, app = app_client(tmp_path)
    signup(c, "a@example.com")
    c.post("/api/brand/logo", files={"file": ("l.png", png(tmp_path / "l.png").read_bytes(), "image/png")})
    job = wait(c, upload(c, video_bytes, cover="1").json()["id"])
    (app.state.store.dir(job["id"]) / "out" / "c1.cover.png").write_bytes(b"\x89PNG\r\n\x1a\nxx")
    assert c.get(f"/api/jobs/{job['id']}/clips/c1/cover").status_code == 200

    other = TestClient(app)
    signup(other, "b@example.com")
    assert other.get("/api/brand").json()["logo"] is False                       # b does not see a's logo
    assert other.get("/api/brand/logo/file").status_code == 404
    assert other.get(f"/api/jobs/{job['id']}/clips/c1/cover").status_code == 404
    anon = TestClient(app)
    assert anon.get("/api/brand").status_code == 401


def test_deleting_the_account_removes_the_brand_files(tmp_path):
    from tests.test_accounts import PW
    c, accounts, app = app_client(tmp_path)
    signup(c, "a@example.com")
    c.post("/api/brand/logo", files={"file": ("l.png", png(tmp_path / "l.png").read_bytes(), "image/png")})
    folders = [p for p in (tmp_path / "data" / "brand").iterdir()]
    assert len(folders) == 1 and (folders[0] / "logo.png").is_file()
    assert c.request("DELETE", "/api/account", json={"password": PW}).status_code == 200
    assert not folders[0].exists()


# --- the editor --------------------------------------------------------------------------------------------------

def edl_with_brand(make_edl, hook=True):
    clip = {"id": "c1", "source_in": 0, "source_out": 10, "title": "Sleep matters",
            "overlays": [{"kind": "hook", "id": "hook", "t_in": 0.3, "text": "Why sleep?"}] if hook else [],
            "ai": {"decided": ["title"], "notes": []}}
    return make_edl(clips=[clip], assets=[{"id": "brand-logo", "kind": "image", "origin": "upload", "uri": "brand/logo.png"}])


def test_editor_switches_the_cover_on_off_and_back_to_the_hook(make_edl):
    edl = edl_with_brand(make_edl)
    on, _ = apply_edit(edl, "c1", ClipEdit(cover=True), source_duration=600)
    assert on.clips[0].cover.text == "Why sleep?" and on.clips[0].cover.enabled
    assert "cover" in on.clips[0].ai.notes[-1]
    custom, _ = apply_edit(on, "c1", ClipEdit(cover_text="  Sleep 8 hours  "), source_duration=600)
    assert custom.clips[0].cover.text == "Sleep 8 hours"
    back, _ = apply_edit(custom, "c1", ClipEdit(cover_text=""), source_duration=600)
    assert back.clips[0].cover.text == "Why sleep?"
    off, _ = apply_edit(back, "c1", ClipEdit(cover=False), source_duration=600)
    assert off.clips[0].cover.enabled is False and off.clips[0].cover.text == "Why sleep?"   # the style is kept
    same, _ = apply_edit(off, "c1", ClipEdit(cover=False), source_duration=600)
    assert same is off                                                           # nothing changed: no re-render
    assert clip_view(off, "c1", source_duration=600)["cover"]["on"] is False


def test_editor_cover_falls_back_to_the_title_and_refuses_when_empty(make_edl):
    edl = edl_with_brand(make_edl, hook=False)
    assert apply_edit(edl, "c1", ClipEdit(cover=True), source_duration=600)[0].clips[0].cover.text == "Sleep matters"
    bare = make_edl(clips=[{"id": "c1", "source_in": 0, "source_out": 10}])
    with pytest.raises(ValueError, match="needs some words"):
        apply_edit(bare, "c1", ClipEdit(cover=True), source_duration=600)
    with pytest.raises(ValueError):
        ClipEdit(cover_text="x" * 81)


def test_editor_logo_and_outro_only_when_the_job_has_them(make_edl):
    edl = edl_with_brand(make_edl)
    new, _ = apply_edit(edl, "c1", ClipEdit(logo=True), source_duration=600)
    assert new.clips[0].logo.asset == "brand-logo"
    gone, _ = apply_edit(new, "c1", ClipEdit(logo=False), source_duration=600)
    assert gone.clips[0].logo.enabled is False
    with pytest.raises(ValueError, match="no ending video"):
        apply_edit(edl, "c1", ClipEdit(outro=True), source_duration=600)
    v = clip_view(edl, "c1", source_duration=600)
    assert v["logo"] == {"available": True, "on": False} and v["outro"]["available"] is False and v["cover"]["text"] == "Why sleep?"


def test_a_cover_longer_than_half_a_trimmed_clip_is_refused_with_a_sentence(make_edl):
    edl = edl_with_brand(make_edl)
    on, _ = apply_edit(edl, "c1", ClipEdit(cover=True), source_duration=600)
    ok, _ = apply_edit(on, "c1", ClipEdit(source_out=3.5), source_duration=600)
    assert ok.clips[0].duration == 3.5
