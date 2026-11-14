import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("multipart")
from fastapi.testclient import TestClient

from clipcrow.edl import EDL
from clipcrow.render import HYPERFRAMES_VERSION
from clipcrow.service.api import create_app
from clipcrow.service.workers import ClipRejected, RemoteRenderer, WorkerError, WorkerHub, WorkerUnavailable, validate_clip
from clipcrow.worker import HttpTransport, evict_cache, handle_render, run_worker, safe_join
from tests.conftest import base_edl
from tests.test_service import FakePipeline
from tests.test_worker import TOKEN, AUTH, FakeTransport, gpu_transcribe

PIN = HYPERFRAMES_VERSION


def make_mp4(path: Path, seconds: float = 3.0, size: str = "1080x1920", vcodec: str = "libx264") -> Path:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c=blue:s={size}:r=5:d={seconds}",
                    "-f", "lavfi", "-i", f"sine=f=330:d={seconds}", "-shortest", "-c:v", vcodec, "-pix_fmt", "yuv420p", "-preset", "ultrafast",
                    "-c:a", "aac", str(path)], check=True)
    return path


def edl_for(media: Path, assets=None, **clip) -> EDL:
    media.mkdir(parents=True, exist_ok=True)
    (media / "src.mp4").write_bytes(b"pretend this is a long source video" * 100)
    c = {"id": "c1", "source_in": 0, "source_out": 3, **clip}
    return EDL.model_validate(base_edl(job_id="abcdef012345", clips=[c], assets=assets or []))


def hub(**kw):
    return WorkerHub(TOKEN, **{"pickup_seconds": 0.5, "lease_seconds": 0.5, "online_seconds": 5.0, "render_pin": PIN, **kw})


def render_task(h, tmp_path, name="edl.json"):
    f = tmp_path / "f.bin"
    f.write_bytes(b"x")
    return h.submit_render("job", {name: f, "media/src.mp4": f}, {"clip_id": "c1"}, tmp_path / "in")


# --- who may take a render ---------------------------------------------------------------------------------------------

def test_only_a_worker_that_can_render_with_the_servers_pin_gets_a_render_task(tmp_path):
    h = hub()
    t = render_task(h, tmp_path)
    assert h.poll("speech-only", 0.0, can=("transcribe",), pin=PIN) is None and t.state == "pending"
    assert h.poll("old-image", 0.0, can=("transcribe", "render"), pin="0.0.1") is None and t.state == "pending"     # a different HyperFrames
    assert not h.online("render") and h.online("transcribe")
    assert h.poll("good", 0.0, can=("transcribe", "render"), pin=PIN) is t and t.worker == "good"
    assert h.online("render")


def test_a_hub_without_a_pin_never_offers_renders(tmp_path):
    h = hub(render_pin="")
    t = render_task(h, tmp_path)
    assert h.poll("w", 0.0, can=("render",), pin="") is None and not h.online("render") and t.state == "pending"


def test_a_transcription_worker_still_gets_transcription_tasks_while_renders_wait(tmp_path):
    h = hub()
    render_task(h, tmp_path)
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"a")
    tt = h.submit("job", audio, "mr")
    got = h.poll("speech-only", 0.0, can=("transcribe",), pin=PIN)
    assert got is tt and got.kind == "transcribe"


# --- what a worker may fetch and send ----------------------------------------------------------------------------------

def test_a_worker_can_fetch_only_manifest_names_and_only_while_it_holds_the_lease(tmp_path):
    h = hub()
    t = render_task(h, tmp_path)
    h.poll("w", 0.0, can=("render",), pin=PIN)
    assert h.file_for(t.id, "edl.json", "w") == tmp_path / "f.bin"
    for name in ("../../etc/passwd", "/etc/passwd", "media/other.mp4", "", "edl.json/../edl.json"):
        assert h.file_for(t.id, name, "w") is None, name                     # a dictionary lookup: there is nothing to traverse
    assert h.file_for(t.id, "edl.json", "someone-else") is None


def test_manifest_names_are_checked_when_the_task_is_made(tmp_path):
    h = hub()
    f = tmp_path / "f"
    f.write_bytes(b"x")
    for bad in ("../x", "/abs", "a/../b", "a//b", "a b\n", "x" * 201, "a\\b"):
        with pytest.raises(WorkerError):
            h.submit_render("job", {bad: f}, {}, tmp_path / "in")


def test_only_the_clip_may_be_uploaded_by_the_worker_holding_the_task(tmp_path):
    h = hub()
    t = render_task(h, tmp_path)
    assert h.upload_path(t.id, "w", "clip.mp4") is None                       # not leased yet
    h.poll("w", 0.0, can=("render",), pin=PIN)
    assert h.upload_path(t.id, "w", "clip.mp4") == tmp_path / "in" / "clip.mp4"
    for name in ("clip.exe", "../clip.mp4", "cover.png", "provenance.json", "edl.json"):
        assert h.upload_path(t.id, "w", name) is None, name
    assert h.upload_path(t.id, "other", "clip.mp4") is None


def test_a_render_that_reports_success_without_a_clip_is_a_failure(tmp_path):
    h = hub()
    t = render_task(h, tmp_path)
    h.poll("w", 0.0, can=("render",), pin=PIN)
    assert h.finish(t.id, "w", result={"warnings": []})
    with pytest.raises(WorkerError, match="no clip"):
        h.wait(t, lambda: None)


def test_one_task_cannot_be_held_for_ever_by_a_worker_that_keeps_heartbeating(tmp_path):
    h = hub(lease_seconds=5)
    t = h.submit_render("job", {"edl.json": tmp_path}, {}, tmp_path / "in", max_seconds=0.4)
    h.poll("w", 0.0, can=("render",), pin=PIN)
    stop = threading.Event()
    threading.Thread(target=lambda: [h.heartbeat(t.id, "w") or time.sleep(0.05) for _ in iter(stop.is_set, True)], daemon=True).start()
    with pytest.raises(WorkerUnavailable, match="took too long"):
        h.wait(t, lambda: None)
    stop.set()


# --- the clip a worker sends back is untrusted ------------------------------------------------------------------------

def test_a_good_clip_passes_and_every_kind_of_wrong_clip_is_rejected(tmp_path):
    edl = edl_for(tmp_path / "m")
    clip = edl.clips[0]
    ok = make_mp4(tmp_path / "ok.mp4", 3.0)
    validate_clip(ok, edl, clip, max_bytes=10_000_000)
    with pytest.raises(ClipRejected, match="too large"):
        validate_clip(ok, edl, clip, max_bytes=100)
    with pytest.raises(ClipRejected, match="1080x1920"):
        validate_clip(make_mp4(tmp_path / "small.mp4", 3.0, size="540x960"), edl, clip, max_bytes=10_000_000)
    with pytest.raises(ClipRejected, match="long"):
        validate_clip(make_mp4(tmp_path / "long.mp4", 9.0), edl, clip, max_bytes=10_000_000)
    with pytest.raises(ClipRejected, match="long"):
        validate_clip(make_mp4(tmp_path / "short.mp4", 1.0), edl, clip, max_bytes=10_000_000)
    with pytest.raises(ClipRejected, match="H.264"):
        validate_clip(make_mp4(tmp_path / "mpeg4.mp4", 3.0, vcodec="mpeg4"), edl, clip, max_bytes=10_000_000)
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"<html><script>alert(1)</script></html>" * 100)
    with pytest.raises(ClipRejected, match="readable"):
        validate_clip(junk, edl, clip, max_bytes=10_000_000)
    tiny = tmp_path / "tiny.mp4"
    tiny.write_bytes(b"x")
    with pytest.raises(ClipRejected, match="empty"):
        validate_clip(tiny, edl, clip, max_bytes=10_000_000)


def test_a_clip_with_an_ending_video_may_be_longer_by_that_much(tmp_path):
    edl = edl_for(tmp_path / "m", assets=[{"id": "brand-outro", "kind": "video", "origin": "upload", "uri": "brand/outro.mp4"}],
                  outro={"enabled": True, "asset": "brand-outro", "max_seconds": 4})
    clip = edl.clips[0]
    longer = make_mp4(tmp_path / "l.mp4", 6.0)
    validate_clip(longer, edl, clip, max_bytes=10_000_000)


# --- the routes --------------------------------------------------------------------------------------------------------

def app_with_worker(tmp_path, pipe=None, **kw):
    pipe = pipe or FakePipeline()
    app = create_app(tmp_path / "data", pipe, worker_token=TOKEN, **kw)
    return TestClient(app), app, pipe


def poll_render(c, name="w", pin=PIN):
    return c.post("/api/worker/poll", json={"worker": name, "wait": 0, "can": ["transcribe", "render"], "pin": pin}, headers=AUTH)


def test_poll_validates_what_a_worker_claims_it_can_do(tmp_path):
    c, _, _ = app_with_worker(tmp_path)
    for body in ({"worker": "w", "can": ["format-disk"]}, {"worker": "w", "can": ["render"] * 3}, {"worker": "w", "pin": "x" * 41}):
        assert c.post("/api/worker/poll", json={**body, "wait": 0}, headers=AUTH).status_code == 422, body
    assert c.get("/api/config").json()["gpu_worker"]["render"] is False
    poll_render(c)
    assert c.get("/api/config").json()["gpu_worker"] == {"enabled": True, "online": True, "render": True}
    poll_render(c, name="old", pin="0.0.1")
    assert c.get("/api/config").json()["gpu_worker"]["render"] is True               # the matching one still counts


def test_file_and_upload_routes_need_the_token_and_a_lease(tmp_path):
    c, _, pipe = app_with_worker(tmp_path)
    f = tmp_path / "f.bin"
    f.write_bytes(b"hello")
    t = pipe.hub.submit_render("job", {"edl.json": f}, {"clip_id": "c1"}, tmp_path / "in")
    poll_render(c, "w")
    url = f"/api/worker/tasks/{t.id}/files/edl.json?worker=w"
    assert c.get(url).status_code == 401
    assert c.get(url, headers=AUTH).content == b"hello"
    assert c.get(f"/api/worker/tasks/{t.id}/files/edl.json?worker=mallory", headers=AUTH).status_code == 404
    for name in ("..%2F..%2Fetc%2Fpasswd", "media/nope.mp4", "%2Fetc%2Fpasswd"):
        assert c.get(f"/api/worker/tasks/{t.id}/files/{name}?worker=w", headers=AUTH).status_code == 404, name
    up = f"/api/worker/tasks/{t.id}/uploads/clip.mp4?worker=w"
    assert c.put(up, content=b"data").status_code == 401
    assert c.put(f"/api/worker/tasks/{t.id}/uploads/evil.sh?worker=w", content=b"x", headers=AUTH).status_code == 404
    assert c.put(f"/api/worker/tasks/{t.id}/uploads/clip.mp4?worker=mallory", content=b"x", headers=AUTH).status_code == 404
    r = c.put(up, content=b"clipdata", headers=AUTH)
    assert r.status_code == 200 and r.json() == {"ok": True, "size": 8}
    assert (tmp_path / "in" / "clip.mp4").read_bytes() == b"clipdata" and not list((tmp_path / "in").glob("*.part"))


def test_an_upload_larger_than_the_cap_is_refused_and_leaves_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPCROW_WORKER_MAX_UPLOAD_MB", "0.001")                    # about 1 KB
    c, _, pipe = app_with_worker(tmp_path)
    f = tmp_path / "f.bin"
    f.write_bytes(b"x")
    t = pipe.hub.submit_render("job", {"edl.json": f}, {}, tmp_path / "in")
    poll_render(c, "w")
    r = c.put(f"/api/worker/tasks/{t.id}/uploads/clip.mp4?worker=w", content=b"z" * 5000, headers=AUTH)
    assert r.status_code == 413
    assert not list((tmp_path / "in").glob("*"))                                    # neither the clip nor a half-written part


def test_a_result_for_a_render_is_bounded_and_needs_the_lease(tmp_path):
    c, _, pipe = app_with_worker(tmp_path)
    f = tmp_path / "f.bin"
    f.write_bytes(b"x")
    t = pipe.hub.submit_render("job", {"edl.json": f}, {}, tmp_path / "in")
    poll_render(c, "w")
    url = f"/api/worker/tasks/{t.id}/result"
    assert c.post(url, json={"worker": "w", "warnings": ["w"] * 21}, headers=AUTH).status_code == 422
    assert c.post(url, json={"worker": "w", "warnings": ["x" * 301]}, headers=AUTH).status_code == 422
    assert c.post(url, json={"worker": "mallory", "warnings": []}, headers=AUTH).json() == {"ok": False}
    assert c.post(url, json={"worker": "w", "warnings": ["fine"]}, headers=AUTH).json() == {"ok": True}      # accepted, but no clip was sent
    assert t.state == "failed" and "no clip" in t.error


# --- the worker program ------------------------------------------------------------------------------------------------

def test_the_worker_never_writes_outside_its_cache(tmp_path):
    base = tmp_path / "cache"
    base.mkdir()
    assert safe_join(base, "a/b.png") == (base / "a" / "b.png").resolve()
    for bad in ("../evil", "a/../../evil", "/etc/passwd", "a\\b", ""):
        with pytest.raises(ValueError):
            safe_join(base, bad)
    (base / "link").symlink_to(tmp_path)
    with pytest.raises(ValueError):
        safe_join(base, "link/escape.txt")                                          # a symlink out of the cache is not followed


def test_the_cache_drops_the_least_recently_used_jobs_first_and_never_the_one_in_use(tmp_path):
    root = tmp_path / "c"
    for i, name in enumerate(("old", "mid", "new")):
        d = root / name
        d.mkdir(parents=True)
        (d / "f").write_bytes(b"x" * 1000)
        import os
        os.utime(d, (1000 + i, 1000 + i))
    evict_cache(root, 10 ** 9)                                                       # room for everything: nothing goes
    assert {d.name for d in root.iterdir()} == {"old", "mid", "new"}
    evict_cache(root, 1500, keep=root / "old")                                       # 3 KB in a 1.5 KB cache: "old" is protected, the rest go
    assert {d.name for d in root.iterdir()} == {"old"}
    evict_cache(root, 0, keep=root / "old")
    assert (root / "old").exists()                                                   # the job in use is never evicted, even over the limit


def fake_render(calls=None, warnings=("a note",), good=True, boom=False):
    def render(edl, clip_id, output, *, workdir, media_root, gsap_js=None, workers=None, asset_root=None):
        if calls is not None:
            calls.append(SimpleNamespace(media=Path(media_root) / edl.source.uri, asset_root=Path(asset_root)))
        assert (Path(media_root) / edl.source.uri).is_file()                         # the source arrived where compile looks for it
        if boom:
            raise RuntimeError("Chrome crashed")
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        if good:
            make_mp4(Path(output), 3.0)
        else:
            Path(output).write_bytes(b"not a video" * 200)
        return Path(output), SimpleNamespace(warnings=list(warnings))
    return render


class CountingTransport(FakeTransport):
    def __init__(self, app):
        super().__init__(app)
        self.downloads = []

    def download(self, path, dest, *, timeout=600.0):
        self.downloads.append(path.split("?")[0].split("/files/")[-1])
        super().download(path, dest, timeout=timeout)


def start_render_worker(app, tmp_path, render, transport=None, **kw):
    transport = transport or FakeTransport(app)
    out = {}

    def go():
        out["n"] = run_worker(transport, "gpu1", transcribe=gpu_transcribe, label="fake", can_render=True, render_fn=render,
                              cache=tmp_path / "wcache", pin=PIN, poll_wait=0.3, log=lambda m: out.setdefault("log", []).append(m), **kw)
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t, out


def wait_render_online(hub, timeout=10):
    end = time.time() + timeout
    while not hub.online("render") and time.time() < end:
        time.sleep(0.05)
    assert hub.online("render")


def local_render(calls):
    def local(edl, clip_id, output, **kw):
        calls.append(clip_id)
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        make_mp4(Path(output), 3.0)
        return Path(output), SimpleNamespace(warnings=["rendered here"])
    return local


def call_renderer(rr, edl, tmp_path, clip="c1"):
    return rr(edl, clip, tmp_path / "out" / f"{clip}.mp4", workdir=tmp_path / "out" / "work", media_root=tmp_path / "media",
              gsap_js=None, workers=None, asset_root=tmp_path / "out")


def test_a_clip_is_rendered_by_the_worker_and_placed_like_a_local_render(tmp_path):
    c, app, pipe = app_with_worker(tmp_path)
    edl = edl_for(tmp_path / "media")
    calls, local_calls, msgs = [], [], []
    ct = CountingTransport(app)
    t, out = start_render_worker(app, tmp_path, fake_render(calls), transport=ct, max_tasks=2)
    wait_render_online(pipe.hub)
    rr = RemoteRenderer(pipe.hub, say=msgs.append, fallback=local_render(local_calls))
    mp4, compiled = call_renderer(rr, edl, tmp_path)
    assert mp4 == tmp_path / "out" / "c1.mp4" and mp4.is_file() and mp4.stat().st_size > 1000
    assert local_calls == [] and compiled.warnings == ["a note"]                       # the worker's notes are forwarded
    assert (tmp_path / "out" / "c1.mp4.ai.json").is_file()                              # the provenance is written by THIS server
    assert any("rendered it" in m for m in msgs) and not list((tmp_path / "out" / "work").glob("worker-tasks/*"))
    assert ct.downloads.count("media/src.mp4") == 1 and "edl.json" in ct.downloads
    call_renderer(rr, edl, tmp_path)                                                    # the same job's next clip: the source is cached
    t.join(15)
    assert ct.downloads.count("media/src.mp4") == 1 and len(calls) == 2
    assert not list((tmp_path / "wcache").rglob("*.mp4")) or all("c1." not in p.name for p in (tmp_path / "wcache" / "abcdef012345" / "out").glob("*"))


def test_assets_arrive_where_the_compiler_looks_for_them(tmp_path):
    c, app, pipe = app_with_worker(tmp_path)
    edl = edl_for(tmp_path / "media", assets=[{"id": "pic", "kind": "image", "origin": "capture", "uri": "assets/pic.png"}])
    (tmp_path / "out" / "assets").mkdir(parents=True)
    (tmp_path / "out" / "assets" / "pic.png").write_bytes(b"\x89PNG....")
    seen = {}

    def render(edl_, clip_id, output, *, workdir, media_root, gsap_js=None, workers=None, asset_root=None):
        seen["asset"] = (Path(asset_root) / "assets" / "pic.png").read_bytes()
        make_mp4(Path(output), 3.0)
        return Path(output), SimpleNamespace(warnings=[])
    t, _ = start_render_worker(app, tmp_path, render, max_tasks=1)
    wait_render_online(pipe.hub)
    call_renderer(RemoteRenderer(pipe.hub, fallback=local_render([])), edl, tmp_path)
    t.join(15)
    assert seen["asset"] == b"\x89PNG...."


def test_with_no_render_worker_the_server_renders_at_once(tmp_path):
    c, app, pipe = app_with_worker(tmp_path)
    local_calls = []
    call_renderer(RemoteRenderer(pipe.hub, fallback=local_render(local_calls)), edl_for(tmp_path / "media"), tmp_path)
    assert local_calls == ["c1"]
    poll_only_speech = c.post("/api/worker/poll", json={"worker": "s", "wait": 0}, headers=AUTH)
    assert poll_only_speech.status_code == 200
    call_renderer(RemoteRenderer(pipe.hub, fallback=local_render(local_calls)), edl_for(tmp_path / "media"), tmp_path)
    assert local_calls == ["c1", "c1"]                                                   # a transcription-only worker is not waited for


@pytest.mark.parametrize("kind", ["garbage", "crash", "silent"])
def test_a_bad_worker_costs_only_time_the_server_renders_itself(tmp_path, kind):
    c, app, pipe = app_with_worker(tmp_path, worker_pickup_seconds=0.8)
    edl = edl_for(tmp_path / "media")
    local_calls, msgs = [], []
    if kind == "silent":
        poll_render(c, "ghost")                                                          # online once, never picks the task up
    else:
        t, _ = start_render_worker(app, tmp_path, fake_render(good=False, boom=(kind == "crash")), max_tasks=1)
        wait_render_online(pipe.hub)
    rr = RemoteRenderer(pipe.hub, say=msgs.append, fallback=local_render(local_calls))
    mp4, compiled = call_renderer(rr, edl, tmp_path)
    assert local_calls == ["c1"] and compiled.warnings == ["rendered here"] and mp4.is_file()
    assert any("did not deliver" in m and "rendering on the server instead" in m for m in msgs)
    assert not any("/" in m and "tmp" in m for m in msgs)                                # no server path in the log


def test_a_worker_that_lost_its_lease_does_not_upload(tmp_path):
    uploaded = []

    class Gone(FakeTransport):
        def __init__(self):
            pass

        def post(self, path, body, *, timeout=60.0):
            if path.endswith("/heartbeat"):
                return {"ok": False}
            if path.endswith("/result"):
                uploaded.append("result")
            return {"ok": True}

        def download(self, path, dest, *, timeout=600.0):
            Path(dest).write_bytes(b"{}")

        def upload(self, path, src, *, timeout=900.0):
            uploaded.append("upload")

    def slow_render(edl, clip_id, output, **kw):
        time.sleep(0.4)
        make_mp4(Path(output), 3.0)
        return Path(output), SimpleNamespace(warnings=[])
    import clipcrow.edl as edlmod
    orig = edlmod.load_edl
    edlmod.load_edl = lambda p: edl_for(tmp_path / "m")
    logs = []
    try:
        task = {"id": "d" * 32, "kind": "render", "lease_seconds": 1, "meta": {"job_id": "abcdef012345", "clip_id": "c1"},
                "files": {"edl.json": 2, "media/src.mp4": 2}}
        assert handle_render(Gone(), "w", task, logs.append, cache=tmp_path / "wc", render_fn=slow_render, beat_seconds=0.05) is False
    finally:
        edlmod.load_edl = orig
    assert uploaded == [] and any("no longer wants" in m or "gave this task up" in m for m in logs)


def test_a_task_naming_an_unexpected_file_is_refused_not_written(tmp_path):
    reported = []

    class T(FakeTransport):
        def __init__(self):
            pass

        def post(self, path, body, *, timeout=60.0):
            reported.append((path, body))
            return {"ok": True}

        def download(self, path, dest, *, timeout=600.0):
            raise AssertionError("must not download a file with an unsafe name")

    for bad in ("media/../../evil", "asset//etc/passwd", "other/thing", "asset/..\\x"):
        task = {"id": "e" * 32, "kind": "render", "meta": {"job_id": "abcdef012345", "clip_id": "c1"}, "files": {bad: 1}}
        assert handle_render(T(), "w", task, lambda m: None, cache=tmp_path / "wc", render_fn=fake_render()) is False
    assert all(path.endswith("/result") and "error" in body for path, body in reported)
    assert not (tmp_path / "evil").exists()


# --- the real HTTP transport streams the clip up ----------------------------------------------------------------------------

def test_a_clip_travels_over_a_real_socket_with_a_streamed_upload(tmp_path):
    import socket

    import uvicorn
    pipe = FakePipeline()
    app = create_app(tmp_path / "data", pipe, worker_token=TOKEN)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    end = time.time() + 10
    while not server.started and time.time() < end:
        time.sleep(0.05)
    try:
        edl = edl_for(tmp_path / "media")
        t, out = start_render_worker(app, tmp_path, fake_render(), transport=HttpTransport(f"http://127.0.0.1:{port}", TOKEN), max_tasks=1)
        wait_render_online(pipe.hub)
        local_calls = []
        mp4, compiled = call_renderer(RemoteRenderer(pipe.hub, fallback=local_render(local_calls)), edl, tmp_path)
        t.join(20)
        assert local_calls == [] and mp4.is_file() and mp4.stat().st_size > 1000 and out.get("n") == 1
    finally:
        server.should_exit = True
        th.join(10)
