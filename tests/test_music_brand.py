"""The account's background track: stored safely, copied into the job, switched on per job and per clip."""
import subprocess

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("multipart")

from clipcrow.branding import BrandError, BrandingChoice, BrandKit, apply_brand
from clipcrow.edl import EDL, load_edl
from clipcrow.service.brand import BrandStore
from clipcrow.service.editor import ClipEdit, apply_edit, clip_view
from tests.conftest import base_edl
from tests.test_service import client, upload, video_bytes, wait  # noqa: F401  (fixtures)

OWNER = "a" * 32


def track(path, seconds=8.0, ext="mp3"):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=f=220:d={seconds}", str(path.with_suffix("." + ext))], check=True)
    return path.with_suffix("." + ext)


@pytest.fixture
def store(tmp_path):
    return BrandStore(tmp_path)


def test_a_track_is_converted_to_one_clean_file_and_can_be_removed(store, tmp_path):
    d = store.put_music(OWNER, track(tmp_path / "t"))
    assert d["music"] is True and 7 <= d["music_seconds"] <= 9 and store.music_path(OWNER).suffix == ".m4a"
    store.delete(OWNER, "music")
    assert store.music_path(OWNER) is None and store.describe(OWNER)["music"] is False


def test_a_bad_track_is_refused_and_the_old_one_stays(store, tmp_path):
    store.put_music(OWNER, track(tmp_path / "t"))
    before = store.music_path(OWNER).read_bytes()
    junk = tmp_path / "j.mp3"
    junk.write_bytes(b"not audio")
    for bad, msg in ((junk, "could not be read|no sound|converted"), (track(tmp_path / "short", 2.0), "between 5 seconds")):
        with pytest.raises(BrandError, match=msg):
            store.put_music(OWNER, bad)
    silent_video = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=64x64:r=10:d=8", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(silent_video)], check=True)
    with pytest.raises(BrandError, match="no sound"):
        store.put_music(OWNER, silent_video)
    assert store.music_path(OWNER).read_bytes() == before


def test_the_job_copies_the_track_and_the_clips_get_the_setting(store, tmp_path):
    store.put_music(OWNER, track(tmp_path / "t"))
    kit = store.kit(OWNER)
    edl = EDL.model_validate(base_edl(source={"uri": "s.mp4", "duration": 60, "width": 1080, "height": 1920, "fps": 30}, clips=[{"id": "c1", "source_in": 0, "source_out": 20}]))
    out = tmp_path / "job" / "out"
    new = apply_brand(edl, kit, BrandingChoice(music=True), out)
    a = next(x for x in new.assets if x.id == "brand-music")
    assert a.kind == "audio" and a.uri == "brand/music.m4a" and (out / "brand" / "music.m4a").is_file()
    assert new.clips[0].music.asset == "brand-music" and new.clips[0].music.enabled and new.clips[0].music.level_db == -10.0
    with pytest.raises(BrandError, match="No background music"):
        apply_brand(edl, BrandKit(), BrandingChoice(music=True), tmp_path / "x")
    assert BrandingChoice(music=True).any() and not BrandingChoice().any()


def test_the_editor_switches_the_music_on_and_off_only_when_the_job_has_one():
    edl = EDL.model_validate(base_edl(source={"uri": "s.mp4", "duration": 60, "width": 1080, "height": 1920, "fps": 30},
                                      assets=[{"id": "brand-music", "kind": "audio", "origin": "upload", "uri": "brand/music.m4a"}],
                                      clips=[{"id": "c1", "source_in": 0, "source_out": 20, "music": {"asset": "brand-music", "level_db": -14}}]))
    v = clip_view(edl, "c1", source_duration=60)["music"]
    assert v == {"available": True, "on": True}
    off, _ = apply_edit(edl, "c1", ClipEdit(music=False), source_duration=60)
    assert off.clips[0].music.enabled is False and off.clips[0].music.level_db == -14
    on, _ = apply_edit(off, "c1", ClipEdit(music=True), source_duration=60)
    assert on.clips[0].music.enabled
    bare = EDL.model_validate(base_edl(source={"uri": "s.mp4", "duration": 60, "width": 1080, "height": 1920, "fps": 30}, clips=[{"id": "c1", "source_in": 0, "source_out": 20}]))
    assert clip_view(bare, "c1", source_duration=60)["music"] == {"available": False, "on": False}
    with pytest.raises(ValueError, match="no background music"):
        apply_edit(bare, "c1", ClipEdit(music=True), source_duration=60)


def test_the_api_stores_serves_and_uses_the_track(tmp_path, video_bytes):
    c, app = client(tmp_path)
    r = upload(c, video_bytes, music="1")
    assert r.status_code == 422 and "music" in r.json()["detail"] and app.state.store.list() == []          # asked for music it does not have
    mp3 = track(tmp_path / "up")
    assert c.post("/api/brand/music", files={"file": ("m.mp3", b"nope", "audio/mpeg")}).status_code == 422
    assert c.post("/api/brand/music", files={"file": ("m.mp3", mp3.read_bytes(), "audio/mpeg")}).json()["music"] is True
    got = c.get("/api/brand/music/file")
    assert got.status_code == 200 and got.headers["content-type"] == "audio/mp4"
    assert c.put("/api/brand", json={"default_music": True}).json()["settings"]["default_music"] is True
    job = wait(c, upload(c, video_bytes, music="1").json()["id"])
    assert job["status"] == "done", job["error"]
    edl = load_edl(app.state.store.edl_path(job["id"]))
    assert edl.clips[0].music.asset == "brand-music" and (tmp_path / "data" / "jobs" / job["id"] / "out" / "brand" / "music.m4a").is_file()
    assert c.delete("/api/brand/music").json()["music"] is False and c.get("/api/brand/music/file").status_code == 404
