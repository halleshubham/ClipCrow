"""A finished clip goes from the GPU worker straight to the bucket in parts (no proxy request-size limit), then to the server's disk."""
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("multipart")
pytest.importorskip("boto3")
pytest.importorskip("moto")

import clipcrow.service.workers as workers_mod
from clipcrow.service.workers import RemoteRenderer
from clipcrow.worker import WorkerHttpError, send_clip
from tests.test_worker import AUTH, FakeTransport
from tests.test_worker_render import (PIN, app_with_worker, call_renderer, edl_for, fake_render, local_render, make_mp4, poll_render,
                                      start_render_worker, wait_render_online)

MB = 1024 * 1024
JOB = "abcdef012345"


class BucketTransport(FakeTransport):
    """The worker's HTTP, plus the PUT to a presigned URL the way ``HttpTransport.put_part`` does it."""

    def __init__(self, app, short_last=False):
        super().__init__(app)
        self.parts_put, self.short_last = 0, short_last

    def put_part(self, url, src, offset, length, *, timeout=900.0):
        with open(src, "rb") as f:
            f.seek(offset)
            data = f.read(length)
        self.parts_put += 1
        if self.short_last and offset + length >= Path(src).stat().st_size:
            data = data[:-10]                                     # a worker that sends less than it announced (the test server ignores the signature)
        req = urllib.request.Request(url, data=data, method="PUT", headers={"Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.headers["ETag"]


@pytest.fixture
def small_parts(monkeypatch):
    monkeypatch.setattr(workers_mod, "DIRECT_PART", 5 * MB)


def leased_task(pipe, tmp_path, direct=True, name="w"):
    f = tmp_path / "f.bin"
    f.write_bytes(b"x")
    t = pipe.hub.submit_render(JOB, {"edl.json": f}, {"clip_id": "c1"}, tmp_path / "in", direct=direct)
    got = pipe.hub.poll(name, 0.0, can=("render",), pin=PIN, direct=True)
    assert got is t
    return t


def a_clip(tmp_path, size):
    p = tmp_path / "made.mp4"
    p.write_bytes(bytes(range(256)) * (size // 256))
    return p


def test_a_clip_reaches_the_server_through_the_bucket_in_several_parts(tmp_path, storage, small_parts):
    c, app, pipe = app_with_worker(tmp_path, storage=storage)
    t = leased_task(pipe, tmp_path)
    clip = a_clip(tmp_path, 12 * MB)
    tr = BucketTransport(app)
    send_clip(tr, "w", t.id, clip)
    assert tr.parts_put == 3                                                   # 5 + 5 + 2 MiB
    assert (tmp_path / "in" / "clip.mp4").read_bytes() == clip.read_bytes() and t.direct_used
    assert not storage.has(JOB, f"worker/{t.id}.mp4")                           # the temporary object is gone


def test_without_a_bucket_the_clip_still_goes_through_the_server(tmp_path):
    c, app, pipe = app_with_worker(tmp_path)
    t = leased_task(pipe, tmp_path, direct=False)
    clip = a_clip(tmp_path, 4096)
    send_clip(FakeTransport(app), "w", t.id, clip)
    assert (tmp_path / "in" / "clip.mp4").read_bytes() == clip.read_bytes() and not t.direct_used


def test_the_operator_can_turn_direct_clips_off(tmp_path, storage, monkeypatch):
    monkeypatch.setenv("CLIPCROW_WORKER_DIRECT", "0")
    c, app, pipe = app_with_worker(tmp_path, storage=storage)
    t = leased_task(pipe, tmp_path, direct=False)
    r = c.post(f"/api/worker/tasks/{t.id}/direct-upload", json={"worker": "w", "size": 5000}, headers=AUTH)
    assert r.status_code == 404


def test_a_short_upload_is_refused_and_leaves_nothing_in_the_bucket(tmp_path, storage, small_parts):
    c, app, pipe = app_with_worker(tmp_path, storage=storage)
    t = leased_task(pipe, tmp_path)
    with pytest.raises(WorkerHttpError) as e:
        send_clip(BucketTransport(app, short_last=True), "w", t.id, a_clip(tmp_path, 7 * MB))
    assert e.value.status == 422
    assert "clip.mp4" not in t.uploads and not t.direct_used and not storage.has(JOB, f"worker/{t.id}.mp4")


def test_missing_parts_are_refused(tmp_path, storage, small_parts):
    c, app, pipe = app_with_worker(tmp_path, storage=storage)
    t = leased_task(pipe, tmp_path)
    r = c.post(f"/api/worker/tasks/{t.id}/direct-upload", json={"worker": "w", "size": 7 * MB}, headers=AUTH)
    assert r.status_code == 200 and [p["n"] for p in r.json()["parts"]] == [1, 2]
    done = c.post(f"/api/worker/tasks/{t.id}/direct-done", json={"worker": "w", "parts": [{"n": 1, "etag": "a" * 32}]}, headers=AUTH)
    assert done.status_code == 422 and "clip.mp4" not in t.uploads


def test_only_the_worker_holding_a_render_may_ask_and_only_once(tmp_path, storage):
    c, app, pipe = app_with_worker(tmp_path, storage=storage)
    t = leased_task(pipe, tmp_path)
    url = f"/api/worker/tasks/{t.id}/direct-upload"
    assert c.post(url, json={"worker": "mallory", "size": 5000}, headers=AUTH).status_code == 409
    assert c.post(url, json={"worker": "w", "size": 5000}, headers={}).status_code == 401
    assert c.post(url, json={"worker": "w", "size": 10}, headers=AUTH).status_code == 422             # smaller than any MP4
    assert c.post(url, json={"worker": "w", "size": 5 * 1024 ** 3}, headers=AUTH).status_code == 409        # larger than the server accepts
    assert c.post(url, json={"worker": "w", "size": 5000}, headers=AUTH).status_code == 200
    assert c.post(url, json={"worker": "w", "size": 5000}, headers=AUTH).status_code == 409           # one plan per task
    assert c.post(f"/api/worker/tasks/{'0' * 32}/direct-upload", json={"worker": "w", "size": 5000}, headers=AUTH).status_code == 409
    assert c.post("/api/worker/tasks/nope/direct-upload", json={"worker": "w", "size": 5000}, headers=AUTH).status_code == 404


def test_a_transcription_task_cannot_ask_for_an_upload(tmp_path, storage):
    c, app, pipe = app_with_worker(tmp_path, storage=storage)
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"a")
    t = pipe.hub.submit(JOB, audio, "mr")
    pipe.hub.poll("w", 0.0, can=("transcribe",))
    assert c.post(f"/api/worker/tasks/{t.id}/direct-upload", json={"worker": "w", "size": 5000}, headers=AUTH).status_code == 409


def test_the_object_name_is_the_tasks_own_and_allow_listed(tmp_path, storage):
    c, app, pipe = app_with_worker(tmp_path, storage=storage)
    t = leased_task(pipe, tmp_path)
    plan = c.post(f"/api/worker/tasks/{t.id}/direct-upload", json={"worker": "w", "size": 5000}, headers=AUTH).json()
    assert f"/jobs/{JOB}/worker/{t.id}.mp4" in plan["parts"][0]["url"] and plan["parts"][0]["url"].startswith(storage.origin)
    with pytest.raises(ValueError):
        storage.key(JOB, "worker/../source.mp4")
    with pytest.raises(ValueError):
        storage.key(JOB, "worker/abc.mp4")


def test_a_long_task_goes_only_to_a_worker_that_can_use_the_bucket(tmp_path, storage):
    c, app, pipe = app_with_worker(tmp_path, storage=storage)
    f = tmp_path / "f.bin"
    f.write_bytes(b"x")
    t = pipe.hub.submit_render(JOB, {"edl.json": f}, {}, tmp_path / "in", direct=True)
    assert pipe.hub.poll("old", 0.0, can=("render",), pin=PIN, direct=False) is None and t.state == "pending"
    assert not pipe.hub.online("render", direct=True) and pipe.hub.online("render")
    assert pipe.hub.poll("new", 0.0, can=("render",), pin=PIN, direct=True) is t
    assert pipe.hub.online("render", direct=True)


def test_without_a_bucket_no_worker_counts_as_direct(tmp_path):
    c, app, pipe = app_with_worker(tmp_path)
    assert c.post("/api/worker/poll", json={"worker": "w", "wait": 0, "can": ["render"], "pin": PIN, "direct": True}, headers=AUTH).status_code == 200
    assert pipe.hub.online("render") and not pipe.hub.online("render", direct=True)


def test_a_long_clip_is_rendered_by_the_worker_through_the_bucket(tmp_path, storage):
    c, app, pipe = app_with_worker(tmp_path, storage=storage)
    edl = edl_for(tmp_path / "media")
    calls, local_calls, msgs = [], [], []
    tr = BucketTransport(app)
    t, _ = start_render_worker(app, tmp_path, fake_render(calls), transport=tr, max_tasks=1)
    wait_render_online(pipe.hub)
    rr = RemoteRenderer(pipe.hub, say=msgs.append, fallback=local_render(local_calls), max_clip_seconds=2.0)       # the 3 s clip counts as long
    mp4, _ = call_renderer(rr, edl, tmp_path)
    t.join(15)
    assert local_calls == [] and len(calls) == 1 and mp4.is_file()
    assert tr.parts_put == 1 and any("rendered it" in m for m in msgs) and not any("long" in m for m in msgs)


def test_a_long_clip_waits_for_the_server_when_no_worker_can_use_the_bucket(tmp_path):
    c, app, pipe = app_with_worker(tmp_path)
    poll_render(c, "w")
    local_calls, msgs = [], []
    rr = RemoteRenderer(pipe.hub, say=msgs.append, fallback=local_render(local_calls), max_clip_seconds=2.0)
    call_renderer(rr, edl_for(tmp_path / "media"), tmp_path)
    assert local_calls == ["c1"] and any("rendered on the server" in m for m in msgs)


def test_a_clip_the_worker_never_finishes_leaves_no_object_behind(tmp_path, storage, small_parts):
    c, app, pipe = app_with_worker(tmp_path, storage=storage)
    t = leased_task(pipe, tmp_path)
    plan = c.post(f"/api/worker/tasks/{t.id}/direct-upload", json={"worker": "w", "size": 7 * MB}, headers=AUTH).json()
    assert plan["parts"]
    pipe.hub.discard_direct(t)                                                     # the render ended some other way
    assert not storage.has(JOB, f"worker/{t.id}.mp4")
    assert storage.client.list_multipart_uploads(Bucket=storage.bucket).get("Uploads", []) == []


def test_the_real_transport_streams_a_slice_to_a_presigned_url_without_the_worker_token(tmp_path, storage):
    from clipcrow.worker import HttpTransport
    clip = a_clip(tmp_path, 5 * MB + 1024)
    up = storage.create_multipart(JOB, f"worker/{'a' * 32}.mp4")
    n = len(clip.read_bytes())
    url = storage.presign_part(JOB, f"worker/{'a' * 32}.mp4", up, 1, n)
    tr = HttpTransport("https://example.invalid", "t" * 32)
    etag = tr.put_part(url, clip, 0, n)
    storage.complete_multipart(JOB, f"worker/{'a' * 32}.mp4", up, [(1, etag)])
    assert storage.size(JOB, f"worker/{'a' * 32}.mp4") == n
    with pytest.raises(ValueError):
        tr.put_part("http://evil.example/x", clip, 0, 10)                           # never plain http to a remote host
