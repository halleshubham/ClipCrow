import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("multipart")
from fastapi.testclient import TestClient

from clipcrow.make import Cancelled, make
from clipcrow.service.api import create_app
from clipcrow.service.pipelines import planner_options
from clipcrow.service.workers import RemoteTranscriber, WorkerError, WorkerHub, WorkerUnavailable
from clipcrow.worker import Transport, WorkerHttpError, handle, run_worker
from tests.test_planner import make_transcript, scripted
from tests.test_service import FakePipeline, upload, video_bytes, wait  # noqa: F401  (fixtures)

TOKEN = "t" * 32


def hub(**kw):
    return WorkerHub(TOKEN, **{"pickup_seconds": 0.4, "lease_seconds": 0.4, "online_seconds": 5.0, **kw})


# --- the hub's rules ---------------------------------------------------------------------------------------------------

def test_the_token_must_be_long_and_is_compared_exactly():
    with pytest.raises(ValueError):
        WorkerHub("short")
    h = hub()
    assert h.check_token(TOKEN) and not h.check_token(TOKEN + "x") and not h.check_token("") and not h.check_token(TOKEN[:-1])


def test_a_worker_is_online_only_while_it_keeps_asking():
    h = hub(online_seconds=0.3)
    assert not h.online()
    assert h.poll("gpu", 0.0, "NVIDIA RTX", "large-v3") is None
    assert h.online() and h.describe()[0]["device"] == "NVIDIA RTX"
    time.sleep(0.45)
    assert not h.online()


def test_a_task_is_leased_to_one_worker_and_only_that_worker_may_use_it(tmp_path):
    h = hub()
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"audio")
    t = h.submit("job1", audio, "mr")
    got = h.poll("gpu1", 1.0)
    assert got is t and t.state == "leased" and t.worker == "gpu1"
    assert h.poll("gpu2", 0.0) is None                                        # already leased
    assert h.audio_for(t.id, "gpu2") is None and not h.heartbeat(t.id, "gpu2") and not h.finish(t.id, "gpu2", result={})
    assert h.audio_for(t.id, "gpu1") == audio
    assert h.finish(t.id, "gpu1", result={"language": "mr", "words": [{"w": "a", "s": 0, "e": 1}]})
    assert h.wait(t, lambda: None)["language"] == "mr"


def test_a_worker_that_stops_sending_heartbeats_loses_the_task(tmp_path):
    h = hub()
    t = h.submit("j", tmp_path / "x", None)
    h.poll("gpu", 1.0)
    with pytest.raises(WorkerUnavailable, match="stopped answering"):
        h.wait(t, lambda: None)
    assert t.state == "abandoned" and not h.finish(t.id, "gpu", result={"words": []})      # a late result is refused


def test_heartbeats_keep_a_slow_task_alive(tmp_path):
    h = hub(lease_seconds=0.5)
    t = h.submit("j", tmp_path / "x", None)
    h.poll("gpu", 1.0)
    stop = threading.Event()

    def beat():
        while not stop.wait(0.1):
            h.heartbeat(t.id, "gpu")
    threading.Thread(target=beat, daemon=True).start()
    threading.Timer(1.2, lambda: h.finish(t.id, "gpu", result={"words": []})).start()    # slower than the lease
    assert h.wait(t, lambda: None) == {"words": []}
    stop.set()


def test_nobody_picking_a_task_up_is_not_waited_for_forever(tmp_path):
    h = hub(pickup_seconds=0.3)
    h.poll("gpu", 0.0)                                                         # online, but never takes it
    t = h.submit("j", tmp_path / "x", None)
    started = time.monotonic()
    with pytest.raises(WorkerUnavailable, match="in time"):
        h.wait(t, lambda: None)
    assert time.monotonic() - started < 3


def test_a_worker_going_offline_releases_a_waiting_job_at_once(tmp_path):
    h = hub(pickup_seconds=30, online_seconds=0.3)
    h.poll("gpu", 0.0)
    t = h.submit("j", tmp_path / "x", None)
    started = time.monotonic()
    with pytest.raises(WorkerUnavailable, match="went offline"):
        h.wait(t, lambda: None)
    assert time.monotonic() - started < 3


def test_a_cancelled_job_stops_waiting_and_the_task_is_abandoned(tmp_path):
    h = hub(pickup_seconds=30)
    h.poll("gpu", 0.0)
    t = h.submit("j", tmp_path / "x", None)
    calls = {"n": 0}

    def check():
        calls["n"] += 1
        if calls["n"] > 2:
            raise Cancelled()
    with pytest.raises(Cancelled):
        h.wait(t, check)
    assert t.state == "abandoned" and h.poll("gpu", 0.0) is None


def test_a_failed_task_reports_the_workers_message(tmp_path):
    h = hub()
    t = h.submit("j", tmp_path / "x", None)
    h.poll("gpu", 1.0)
    h.finish(t.id, "gpu", error="CUDA out of memory")
    with pytest.raises(WorkerError, match="CUDA out of memory"):
        h.wait(t, lambda: None)


# --- the routes --------------------------------------------------------------------------------------------------------

def app_with_worker(tmp_path, pipeline=None, **kw):
    app = create_app(tmp_path / "data", pipeline or FakePipeline(), worker_token=TOKEN, **kw)
    return TestClient(app), app


AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_the_routes_do_not_exist_without_a_worker_token(tmp_path):
    c = TestClient(create_app(tmp_path / "data", FakePipeline()))
    assert c.post("/api/worker/poll", json={"worker": "w", "wait": 0}, headers=AUTH).status_code == 404
    assert c.get("/api/config").json()["gpu_worker"] == {"enabled": False, "online": False, "render": False}


def test_a_wrong_or_missing_token_is_refused_and_a_right_one_polls(tmp_path):
    c, _ = app_with_worker(tmp_path)
    body = {"worker": "gpu1", "wait": 0}
    assert c.post("/api/worker/poll", json=body).status_code == 401
    assert c.post("/api/worker/poll", json=body, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.post("/api/worker/poll", json=body, headers={"Authorization": TOKEN}).status_code == 401       # not a Bearer header
    assert c.get("/api/worker/tasks/" + "a" * 32 + "/audio?worker=gpu1").status_code == 401
    r = c.post("/api/worker/poll", json=body, headers=AUTH)
    assert r.status_code == 200 and r.json() == {"task": None}
    assert c.get("/api/config").json()["gpu_worker"] == {"enabled": True, "online": True, "render": False}      # transcription only


def test_a_workers_token_is_not_a_login_and_a_login_is_not_a_worker_token(tmp_path):
    c, _ = app_with_worker(tmp_path, token="login-token-1234")
    assert c.get("/api/jobs", headers=AUTH).status_code == 401                      # the worker secret does not open the app
    assert c.post("/api/worker/poll", json={"worker": "w", "wait": 0}, headers={"Authorization": "Bearer login-token-1234"}).status_code == 401


def test_what_a_worker_sends_is_bounded(tmp_path):
    c, _ = app_with_worker(tmp_path)
    tid = "b" * 32
    bad = [
        {"worker": "w", "words": [{"w": "x" * 101, "s": 0, "e": 1}]},
        {"worker": "w", "words": [{"w": "x", "s": -1, "e": 1}]},
        {"worker": "w", "words": [{"w": "x", "s": 0, "e": 1, "evil": 1}]},
        {"worker": "w/../x", "words": []},
        {"worker": "w", "language": "english", "words": []},
        {"worker": "w", "words": [], "error": "e" * 301},
        {"worker": "w", "unknown": 1},
    ]
    for body in bad:
        assert c.post(f"/api/worker/tasks/{tid}/result", json=body, headers=AUTH).status_code == 422, body
    assert c.post("/api/worker/poll", json={"worker": "w", "wait": 999}, headers=AUTH).status_code == 422


def test_task_ids_and_audio_access_are_strict(tmp_path):
    c, _ = app_with_worker(tmp_path)
    for tid in ("short", "A" * 32, "g" * 32, "../" + "a" * 29, "a" * 32 + "%0A"):
        assert c.get(f"/api/worker/tasks/{tid}/audio?worker=w", headers=AUTH).status_code in (404, 405, 422), tid
        assert c.post(f"/api/worker/tasks/{tid}/heartbeat", json={"worker": "w"}, headers=AUTH).status_code in (404, 405, 422), tid
    assert c.get("/api/worker/tasks/" + "a" * 32 + "/audio?worker=w", headers=AUTH).status_code == 404      # no such task
    assert c.post("/api/worker/tasks/" + "a" * 32 + "/heartbeat", json={"worker": "w"}, headers=AUTH).json() == {"ok": False}


# --- the worker program ------------------------------------------------------------------------------------------------

class FakeTransport(Transport):
    """The worker's HTTP, served by the app in-process."""

    def __init__(self, app, token=TOKEN):
        self.c, self.h = TestClient(app), {"Authorization": f"Bearer {token}"}

    def post(self, path, body, *, timeout=60.0):
        r = self.c.post(path, json=body, headers=self.h)
        if r.status_code >= 400:
            raise WorkerHttpError(r.status_code, str(r.status_code))
        return r.json()

    def download(self, path, dest, *, timeout=600.0):
        r = self.c.get(path, headers=self.h)
        if r.status_code >= 400:
            raise WorkerHttpError(r.status_code, str(r.status_code))
        Path(dest).write_bytes(r.content)

    def upload(self, path, src, *, timeout=900.0):
        r = self.c.put(path, content=Path(src).read_bytes(), headers=self.h)
        if r.status_code >= 400:
            raise WorkerHttpError(r.status_code, str(r.status_code))


def gpu_transcribe(media, language=None, progress=None):
    assert Path(media).is_file() and Path(media).stat().st_size > 0                # it really received audio
    tr = make_transcript()
    tr.language = "mr"
    return tr


def test_a_worker_refused_by_the_server_stops_instead_of_hammering_it(tmp_path):
    _, app = app_with_worker(tmp_path)
    logs = []
    assert run_worker(FakeTransport(app, token="x" * 32), "w", transcribe=gpu_transcribe, label="cpu", log=logs.append, poll_wait=0) == 0
    assert any("refused" in m for m in logs)


class HubPipeline(FakePipeline):
    """What RealPipeline does with a hub, with the slow and paid parts faked."""
    hub = None

    def __init__(self):
        super().__init__()
        self.local_calls = 0

    def local(self, media, language=None, progress=None):
        self.local_calls += 1
        return make_transcript()

    def run(self, job, video, out, hooks, say):
        def fake_render(edl, clip_id, path, **kw):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"0123456789" * 1000)
            return path, None
        fn = RemoteTranscriber(self.hub, video.parent, check=hooks.check, fallback=self.local) if self.hub and self.hub.online() else self.local
        return make(video, out, llm=scripted(), opts=planner_options(job), hooks=hooks, say=say, transcribe_fn=fn, render_fn=fake_render)


def start_worker(app, **kw):
    out = {"done": None}

    def go():
        out["done"] = run_worker(FakeTransport(app), "gpu1", transcribe=kw.pop("transcribe", gpu_transcribe), label="fake-gpu",
                                 max_tasks=kw.pop("max_tasks", 1), poll_wait=0.3, log=lambda m: None)
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t, out


def test_a_job_uses_the_gpu_worker_when_one_is_online(tmp_path, video_bytes):
    pipe = HubPipeline()
    c, app = app_with_worker(tmp_path, pipe)
    t, out = start_worker(app)
    end = time.time() + 10
    while not pipe.hub.online() and time.time() < end:                             # the worker has introduced itself
        time.sleep(0.05)
    job = wait(c, upload(c, video_bytes).json()["id"])
    t.join(10)
    assert job["status"] == "done", job
    assert pipe.local_calls == 0 and out["done"] == 1
    assert any("sending the audio to the GPU worker" in m for m in job["log"])
    assert any("the GPU worker transcribed" in m for m in job["log"])
    assert not list((tmp_path / "data" / "jobs").glob("*/worker-audio.m4a"))      # the audio copy is removed afterwards


def test_with_no_worker_online_the_server_transcribes_at_once(tmp_path, video_bytes):
    pipe = HubPipeline()
    c, _ = app_with_worker(tmp_path, pipe)
    started = time.monotonic()
    job = wait(c, upload(c, video_bytes).json()["id"])
    assert job["status"] == "done" and pipe.local_calls == 1 and time.monotonic() - started < 20
    assert not any("GPU worker" in m for m in job["log"])


def test_a_worker_that_never_picks_up_costs_only_the_pickup_wait(tmp_path, video_bytes):
    pipe = HubPipeline()
    c, app = app_with_worker(tmp_path, pipe, worker_pickup_seconds=0.6)
    FakeTransport(app).post("/api/worker/poll", {"worker": "gpu1", "wait": 0})      # online once, then silent
    job = wait(c, upload(c, video_bytes).json()["id"])
    assert job["status"] == "done" and pipe.local_calls == 1
    assert any("did not deliver" in m and "transcribing on the server instead" in m for m in job["log"])


def test_a_worker_that_fails_sends_the_job_to_the_server_without_waiting_for_the_lease(tmp_path, video_bytes):
    pipe = HubPipeline()
    c, app = app_with_worker(tmp_path, pipe)

    def broken(media, language=None, progress=None):
        raise RuntimeError("CUDA out of memory")
    t, _ = start_worker(app, transcribe=broken)
    end = time.time() + 10
    while not pipe.hub.online() and time.time() < end:
        time.sleep(0.05)
    started = time.monotonic()
    job = wait(c, upload(c, video_bytes).json()["id"])
    t.join(10)
    assert job["status"] == "done" and pipe.local_calls == 1 and time.monotonic() - started < 30
    assert any("CUDA out of memory" in m for m in job["log"])


def test_garbage_from_a_worker_is_refused_and_the_job_still_finishes(tmp_path, video_bytes):
    pipe = HubPipeline()
    c, app = app_with_worker(tmp_path, pipe)

    def nonsense(media, language=None, progress=None):
        tr = make_transcript()
        tr.words = []                                                              # a worker that "heard" nothing
        return tr
    t, _ = start_worker(app, transcribe=nonsense)
    end = time.time() + 10
    while not pipe.hub.online() and time.time() < end:
        time.sleep(0.05)
    job = wait(c, upload(c, video_bytes).json()["id"])
    t.join(10)
    assert job["status"] == "done" and pipe.local_calls == 1


def test_a_worker_that_lost_its_lease_drops_the_result(tmp_path):
    lost = []

    class Gone(Transport):
        def post(self, path, body, *, timeout=60.0):
            if path.endswith("/heartbeat"):
                return {"ok": False}                                               # the server has given the task up
            if path.endswith("/result"):
                lost.append("delivered")
                return {"ok": True}
            raise AssertionError(path)

        def download(self, path, dest, *, timeout=600.0):
            Path(dest).write_bytes(b"audio")

    logs = []

    def slow(media, language=None, progress=None):
        time.sleep(0.5)
        return gpu_transcribe(str(media)) if False else make_transcript()
    assert handle(Gone(), "w", {"id": "c" * 32, "language": "mr", "lease_seconds": 1}, slow, logs.append, beat_seconds=0.05) is False
    assert lost == [] and any("no longer wants" in m for m in logs)


# --- the real transport, over a real socket ----------------------------------------------------------------------------

def test_the_real_http_transport_round_trips_with_a_live_server(tmp_path, video_bytes):
    import socket

    import uvicorn
    from clipcrow.worker import HttpTransport

    pipe = HubPipeline()
    app = create_app(tmp_path / "data", pipe, worker_token=TOKEN)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    end = time.time() + 10
    while not server.started and time.time() < end:
        time.sleep(0.05)
    assert server.started
    try:
        base = f"http://127.0.0.1:{port}"
        done = {}

        def go():
            done["n"] = run_worker(HttpTransport(base, TOKEN), "gpu-live", transcribe=gpu_transcribe, label="fake", max_tasks=1,
                                   poll_wait=1, log=lambda m: None)
        w = threading.Thread(target=go, daemon=True)
        w.start()
        c = TestClient(app)
        end = time.time() + 10
        while not pipe.hub.online() and time.time() < end:
            time.sleep(0.05)
        job = wait(c, upload(c, video_bytes).json()["id"])
        w.join(15)
        assert job["status"] == "done" and done.get("n") == 1 and pipe.local_calls == 0
        # a wrong token over the real transport is refused and ends the worker instead of looping
        assert run_worker(HttpTransport(base, "z" * 32), "bad", transcribe=gpu_transcribe, label="x", poll_wait=0, log=lambda m: None) == 0
    finally:
        server.should_exit = True
        thread.join(10)


def test_the_transport_refuses_plain_http_to_a_remote_host():
    from clipcrow.worker import HttpTransport
    with pytest.raises(ValueError):
        HttpTransport("http://example.com", TOKEN)
    HttpTransport("https://example.com", TOKEN)
    HttpTransport("http://127.0.0.1:8000", TOKEN)
