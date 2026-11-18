"""Telling the people in a video apart on the GPU worker: who may take the task, what a worker may answer, and the fall-backs."""
import threading
import time
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("multipart")
from fastapi.testclient import TestClient
from pydantic import ValidationError

from clipcrow import seats
from clipcrow.service.api import create_app
from clipcrow.service.workers import RemoteSpeakers, SpeakersIn, WorkerError, WorkerHub
from clipcrow.worker import handle_speakers, run_worker
from tests.test_service import FakePipeline
from tests.test_speakers import ToneEmbedder, make_video, talk_audio
from tests.test_worker import AUTH, TOKEN, FakeTransport, gpu_transcribe


def hub(**kw):
    return WorkerHub(TOKEN, **{"pickup_seconds": 0.6, "lease_seconds": 0.6, "online_seconds": 5.0, **kw})


GOOD = {"duration": 36.0, "seats": [{"cx": 0.25, "cy": 0.4, "w": 0.09, "h": 0.17, "seen": 0.9, "live": 4.2}, {"cx": 0.75, "cy": 0.4, "w": 0.09, "h": 0.17, "seen": 0.8}],
        "turns": [[0.0, 10.0, 0], [12.0, 22.0, 1], [24.0, 34.0, 0]], "voice_seat": {"0": 0, "1": 1}, "lift": {"0": 2.1, "1": 1.9}, "picture_t": 5.0}


def app_with_worker(tmp_path):
    pipe = FakePipeline()
    app = create_app(tmp_path / "data", pipe, worker_token=TOKEN)
    return TestClient(app), app, pipe


# --- who may take the task --------------------------------------------------------------------------------------------

def test_only_a_worker_that_says_it_can_gets_a_speakers_task(tmp_path):
    h = hub()
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    t = h.submit_speakers("job", {"media/v.mp4": f}, {"job_id": "job"})
    assert h.poll("speech-only", 0.0, can=("transcribe",)) is None and h.poll("renderer", 0.0, can=("transcribe", "render")) is None and t.state == "pending"
    assert not h.online("speakers")
    assert h.poll("gpu", 0.0, can=("transcribe", "speakers")) is t and h.online("speakers")


def test_the_manifest_of_a_speakers_task_is_the_video_by_a_media_name(tmp_path):
    h = hub()
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    for bad in ("../v.mp4", "v.mp4", "edl.json", "media/../x", "/media/v.mp4"):
        with pytest.raises(WorkerError):
            h.submit_speakers("job", {bad: f}, {})


def test_poll_accepts_speakers_and_still_bounds_the_list(tmp_path):
    c, _, _ = app_with_worker(tmp_path)
    ok = c.post("/api/worker/poll", json={"worker": "w", "wait": 0, "can": ["transcribe", "render", "speakers"]}, headers=AUTH)
    assert ok.status_code == 200
    assert c.post("/api/worker/poll", json={"worker": "w", "wait": 0, "can": ["speakers"] * 4}, headers=AUTH).status_code == 422


# --- what a worker may answer ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("change", [
    {"seats": [{"cx": -0.1, "cy": 0.4, "w": 0.1, "h": 0.1}]},                              # off the frame
    {"seats": [{"cx": 0.5, "cy": 0.5, "w": 0.0, "h": 0.1}]},
    {"seats": [{"cx": 0.5, "cy": 0.5, "w": 0.1, "h": 0.1}] * 13},                            # too many people
    {"turns": [[0.0, 10_000.0, 0]]},                                                      # past the end of the video
    {"turns": [[5.0, 5.0, 0]]},                                                           # empty
    {"turns": [[0.0, 5.0, 10 ** 9]]},                                                     # a voice number out of range
    {"turns": [[0.0, 1.0, 0]] * 4001},                                                    # a million turns, in spirit
    {"voice_seat": {"0": 7}},                                                             # a seat that does not exist
    {"voice_seat": {"99": 0}},
    {"lift": {"0": 10 ** 9}},
    {"duration": -1.0},
    {"unknown": 1},                                                                       # extra fields are refused
])
def test_a_hostile_answer_is_refused(change):
    with pytest.raises(ValidationError):
        SpeakersIn.model_validate({**GOOD, **change})
    assert SpeakersIn.model_validate(GOOD).seats[0].cx == 0.25


def test_the_result_route_needs_an_answer_for_a_speakers_task_and_bounds_it(tmp_path):
    c, app, pipe = app_with_worker(tmp_path)
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    task = pipe.hub.submit_speakers("job", {"media/v.mp4": f}, {"job_id": "job"})
    assert pipe.hub.poll("gpu", 0.0, can=("speakers",)) is task
    assert c.post(f"/api/worker/tasks/{task.id}/result", json={"worker": "gpu", "speakers": {**GOOD, "turns": [[0, 9e9, 0]]}}, headers=AUTH).status_code == 422
    assert task.state == "leased"                                                          # a refused answer does not finish the task
    assert c.post(f"/api/worker/tasks/{task.id}/result", json={"worker": "gpu", "speakers": GOOD}, headers=AUTH).json() == {"ok": True}
    assert task.state == "done" and task.result["speakers"]["seats"][1]["cx"] == 0.75


# --- the job side and the worker side together --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def video(tmp_path_factory):
    path = tmp_path_factory.mktemp("spk") / "talk.mp4"
    make_video(path, talk_audio([(200, 10), (None, 2), (400, 10), (None, 2), (200, 10)]), [(0, 10, 0), (12, 22, 1), (24, 34, 0)])
    return path


def start_worker(app, tmp_path, speakers_fn, **kw):
    out = {"log": []}

    def go():
        out["n"] = run_worker(FakeTransport(app), "gpu1", transcribe=gpu_transcribe, label="fake", can_speakers=True, speakers_fn=speakers_fn,
                              cache=tmp_path / "wcache", poll_wait=0.3, log=out["log"].append, **kw)
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t, out


def wait_online(h, timeout=10):
    end = time.time() + timeout
    while not h.online("speakers") and time.time() < end:
        time.sleep(0.05)
    assert h.online("speakers")


def good_answer(video, say, check):
    say("pretending to look")
    check()
    assert Path(video).is_file() and Path(video).name == "talk.mp4"                          # it arrived under the name a render would use
    return {**GOOD, "turns": [list(t) for t in GOOD["turns"]]}


def test_a_worker_finds_who_is_who_and_the_server_keeps_its_own_signature_and_draws_its_own_picture(tmp_path, video):
    c, app, pipe = app_with_worker(tmp_path)
    t, out = start_worker(app, tmp_path, good_answer, max_tasks=1)
    wait_online(pipe.hub)
    said, local = [], []
    rs = RemoteSpeakers(pipe.hub, "abcdef012345", say=said.append, fallback=lambda *a, **k: local.append(1))
    sm = rs(video, tmp_path / "out")
    t.join(15)
    assert sm is not None and local == [] and len(sm.seats) == 2 and sm.voice_seat == {0: 0, 1: 1}
    assert sm.signature == seats.video_signature(video) and sm.picture_t == 5.0
    assert (tmp_path / "out" / "speakers.json").is_file() and (tmp_path / "out" / "speakers.jpg").stat().st_size > 1000      # drawn here, from our own copy
    assert any("GPU worker matched" in m for m in said) and any("who-is-who" in m for m in out["log"])
    again = rs(video, tmp_path / "out")                                                     # cached: no second task
    assert again is not None and again.voice_seat == sm.voice_seat


def test_no_worker_a_failing_worker_or_a_worker_that_lies_means_the_server_does_it(tmp_path, video):
    c, app, pipe = app_with_worker(tmp_path)
    calls = []

    def local(v, out, **kw):
        calls.append("local")
        return None
    rs = RemoteSpeakers(pipe.hub, "abcdef012345", fallback=local, max_seconds=5)
    assert rs(video, tmp_path / "o1") is None and calls == ["local"]                        # nobody online

    def boom(video, say, check):
        raise RuntimeError("CUDA out of memory")
    t, out = start_worker(app, tmp_path, boom, max_tasks=1)
    wait_online(pipe.hub)
    said = []
    rs = RemoteSpeakers(pipe.hub, "abcdef012345", say=said.append, fallback=local)
    assert rs(video, tmp_path / "o2") is None and calls == ["local", "local"] and any("did not deliver" in m for m in said)
    t.join(15)

    def liar(video, say, check):
        return {**GOOD, "duration": 5000.0, "turns": [[0.0, 10.0, 0]]}                      # a different video's answer
    t, out = start_worker(app, tmp_path, liar, max_tasks=1)
    wait_online(pipe.hub)
    assert rs(video, tmp_path / "o3") is None and calls == ["local", "local", "local"]
    t.join(15)
    assert not (tmp_path / "o3" / "speakers.json").exists()                                  # the lie was never saved


def test_a_one_person_answer_is_saved_as_nothing_to_tell_apart(tmp_path, video):
    c, app, pipe = app_with_worker(tmp_path)
    t, out = start_worker(app, tmp_path, lambda v, say, check: {"duration": 36.0, "seats": [], "turns": [], "voice_seat": {}, "lift": {}, "picture_t": None}, max_tasks=1)
    wait_online(pipe.hub)
    rs = RemoteSpeakers(pipe.hub, "abcdef012345", fallback=lambda *a, **k: pytest.fail("the worker answered"))
    assert rs(video, tmp_path / "o") is None
    t.join(15)
    assert seats.cached_map(video, tmp_path / "o") == (True, None)


def test_the_worker_refuses_a_task_with_a_bad_job_id_or_manifest(tmp_path):
    c, app, pipe = app_with_worker(tmp_path)
    t = FakeTransport(app)
    log = []
    assert handle_speakers(t, "gpu1", {"id": "a" * 32, "meta": {"job_id": "../x"}, "files": {"media/v.mp4": 1}}, log.append, cache=tmp_path / "w") is False
    assert handle_speakers(t, "gpu1", {"id": "a" * 32, "meta": {"job_id": "abc"}, "files": {}}, log.append, cache=tmp_path / "w") is False
    assert not (tmp_path / "w" / "abc").exists()
