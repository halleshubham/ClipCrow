"""Framing the clips on the GPU worker: who may take the task, what a worker may answer, and every fall-back to the server."""
import threading
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("multipart")
from fastapi.testclient import TestClient
from pydantic import ValidationError

from clipcrow.edl import EDL
from clipcrow.framing import apply_framing, plan_clips
from clipcrow.reframe import Framing
from clipcrow.service.api import create_app
from clipcrow.service.gpu_pod import PodController
from clipcrow.service.workers import FramingIn, FramingOneIn, RemoteFraming, WorkerError, WorkerHub, framing_from_in
from clipcrow.worker import handle_framing, run_worker
from tests.conftest import base_edl
from tests.test_service import FakePipeline
from tests.test_worker import AUTH, TOKEN, FakeTransport, gpu_transcribe


def hub(**kw):
    return WorkerHub(TOKEN, **{"pickup_seconds": 0.6, "lease_seconds": 0.6, "online_seconds": 5.0, **kw})


def one(cid="c1", **kw):
    return {"id": cid, "framing": "track", "keyframes": [[0.0, 0.4], [1.0, 0.45]], "coverage": 0.9, "split": [], "people": 1, "fit_spans": [], "face_zone": [0.3, 0.1, 0.7, 0.4],
            "seconds": 12.0, **kw}


GOOD = {"clips": [one("c1"), one("c2", framing="fit_blur", keyframes=[], coverage=0.1, face_zone=None)]}


def app_with_worker(tmp_path):
    pipe = FakePipeline()
    app = create_app(tmp_path / "data", pipe, worker_token=TOKEN)
    return TestClient(app), app, pipe


# --- who may take the task ------------------------------------------------------------------------------------------

def test_only_a_worker_that_says_it_can_gets_a_framing_task_and_the_manifest_is_the_video(tmp_path):
    h = hub()
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    for bad in ("../v.mp4", "v.mp4", "edl.json", "media/../x", "/media/v.mp4"):
        with pytest.raises(WorkerError):
            h.submit_framing("job", {bad: f}, {})
    t = h.submit_framing("job", {"media/v.mp4": f}, {"job_id": "job", "clips": []})
    assert h.poll("speech-only", 0.0, can=("transcribe", "render", "speakers")) is None and not h.online("framing")
    assert h.poll("gpu", 0.0, can=("transcribe", "framing")) is t and h.online("framing")


def test_poll_accepts_framing_and_still_bounds_the_list(tmp_path):
    c, _, _ = app_with_worker(tmp_path)
    assert c.post("/api/worker/poll", json={"worker": "w", "wait": 0, "can": ["transcribe", "render", "speakers", "framing"]}, headers=AUTH).status_code == 200
    assert c.post("/api/worker/poll", json={"worker": "w", "wait": 0, "can": ["framing"] * 5}, headers=AUTH).status_code == 422


def test_a_sleeping_pod_may_be_woken_for_framing_too():
    h = hub()
    ctl = PodController(object(), h, "p1", hourly=45.0, threaded=False)
    ctl.state = "asleep"
    assert ctl.can_wake("framing") and h.online("framing")


# --- what a worker may answer -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("change", [
    {"id": "../x"}, {"id": "c1; drop"}, {"framing": "zoom"},
    {"keyframes": [[0.0, 1.5]]}, {"keyframes": [[-1.0, 0.5]]}, {"keyframes": [[9999.0, 0.5]]}, {"keyframes": [[0.0, 0.5]] * 12001},
    {"coverage": 1.5}, {"split": [[0.5, 0.5]] * 5}, {"split": [[2.0, 0.5]]}, {"people": 0}, {"people": 99},
    {"fit_spans": [[5.0, 5.0]]}, {"fit_spans": [[0.0, 1.0]] * 201},
    {"face_zone": [0.7, 0.1, 0.3, 0.4]}, {"face_zone": [0.0, 0.0, 1.5, 1.0]}, {"seconds": -1.0}, {"unknown": 1},
])
def test_a_hostile_framing_answer_is_refused(change):
    with pytest.raises(ValidationError):
        FramingOneIn.model_validate({**one(), **change})
    assert FramingOneIn.model_validate(one()).keyframes[1] == (1.0, 0.45)
    with pytest.raises(ValidationError):
        FramingIn.model_validate({"clips": [one()] * 61})


def test_the_answer_becomes_the_same_framing_the_server_would_have_made():
    f = framing_from_in(FramingOneIn.model_validate(one(framing="split", split=[[0.25, 0.4], [0.75, 0.4]], people=2, fit_spans=[[1.0, 2.0]])))
    assert isinstance(f, Framing) and f.framing == "split" and f.keyframes == [(0.0, 0.4), (1.0, 0.45)] and f.split == [(0.25, 0.4), (0.75, 0.4)]
    assert f.fit_spans == [(1.0, 2.0)] and f.face_zone == (0.3, 0.1, 0.7, 0.4) and f.people == 2


def test_the_result_route_needs_an_answer_for_a_framing_task_and_bounds_it(tmp_path):
    c, app, pipe = app_with_worker(tmp_path)
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    task = pipe.hub.submit_framing("job", {"media/v.mp4": f}, {"job_id": "job"})
    assert pipe.hub.poll("gpu", 0.0, can=("framing",)) is task
    bad = {"clips": [one(keyframes=[[0, 7.0]])]}
    assert c.post(f"/api/worker/tasks/{task.id}/result", json={"worker": "gpu", "framing": bad}, headers=AUTH).status_code == 422
    assert task.state == "leased"
    assert c.post(f"/api/worker/tasks/{task.id}/result", json={"worker": "gpu"}, headers=AUTH).json() == {"ok": True} and task.state == "failed"    # no answer: failed, the server does it
    task = pipe.hub.submit_framing("job", {"media/v.mp4": f}, {"job_id": "job"})
    pipe.hub.poll("gpu", 0.0, can=("framing",))
    assert c.post(f"/api/worker/tasks/{task.id}/result", json={"worker": "gpu", "framing": GOOD}, headers=AUTH).json() == {"ok": True}
    assert task.state == "done" and [o["id"] for o in task.result["framing"]["clips"]] == ["c1", "c2"]


# --- the job side and the worker side together ------------------------------------------------------------------------------

def start_worker(app, tmp_path, framing_fn, **kw):
    out = {"log": []}

    def go():
        out["n"] = run_worker(FakeTransport(app), "gpu1", transcribe=gpu_transcribe, label="fake", can_framing=True, framing_fn=framing_fn,
                              cache=tmp_path / "wcache", poll_wait=0.3, log=out["log"].append, **kw)
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t, out


def wait_online(h, timeout=10):
    end = time.time() + timeout
    while not h.online("framing") and time.time() < end:
        time.sleep(0.05)
    assert h.online("framing")


SPANS = [("c1", 0.0, 10.0), ("c2", 20.0, 30.0)]


def test_a_worker_frames_the_clips_and_the_server_uses_its_answer(tmp_path):
    c, app, pipe = app_with_worker(tmp_path)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"not really a video: the fake worker only looks at the name")
    seen = {}

    def answer(path, clips, multi, graphics, say, check):
        seen.update(name=path.name, clips=clips, multi=multi, graphics=graphics)
        say("pretending to look")
        check()
        return GOOD
    t, out = start_worker(app, tmp_path, answer, max_tasks=1)
    wait_online(pipe.hub)
    said = []
    got = RemoteFraming(pipe.hub, "abcdef012345", say=said.append)(video, SPANS, multi="off", graphics=False, speakers=None)
    t.join(15)
    assert set(got) == {"c1", "c2"} and got["c1"][0].framing == "track" and got["c2"][0].framing == "fit_blur" and got["c1"][1] == 12.0
    assert seen["name"] == "v.mp4" and seen["multi"] == "off" and [x["id"] for x in seen["clips"]] == ["c1", "c2"] and seen["clips"][1]["start"] == 20.0
    assert any("GPU worker framed 2" in m for m in said) and any("framing of 2 clip(s)" in m for m in out["log"])


def test_nothing_online_a_failing_worker_a_liar_or_a_voice_map_means_the_server_does_it(tmp_path):
    c, app, pipe = app_with_worker(tmp_path)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    rf = RemoteFraming(pipe.hub, "abcdef012345", say=lambda m: None)
    assert rf(video, SPANS, multi="off", graphics=False, speakers=None) == {}                     # nobody online

    def boom(*a):
        raise RuntimeError("out of memory")
    t, out = start_worker(app, tmp_path, boom, max_tasks=1)
    wait_online(pipe.hub)
    said = []
    assert RemoteFraming(pipe.hub, "abcdef012345", say=said.append)(video, SPANS, multi="off", graphics=False, speakers=None) == {}
    assert any("did not deliver" in m for m in said)
    t.join(15)

    t, out = start_worker(app, tmp_path, lambda *a: {"clips": [one("c9")]}, max_tasks=1)       # a clip nobody asked about
    wait_online(pipe.hub)
    assert rf(video, SPANS, multi="off", graphics=False, speakers=None) == {}
    t.join(15)

    t, out = start_worker(app, tmp_path, lambda *a: pytest.fail("a voice map decides the framing: the worker must not be asked"), max_tasks=1)
    wait_online(pipe.hub)
    assert rf(video, SPANS, multi="switch", graphics=False, speakers=object()) == {}              # cheap, and it stays here
    t.join(0.1)


def test_the_worker_refuses_a_task_with_a_bad_job_id_manifest_or_clip_list(tmp_path):
    c, app, pipe = app_with_worker(tmp_path)
    t = FakeTransport(app)
    log = []
    ok = {"job_id": "abc", "clips": [{"id": "c1", "start": 0, "end": 5}]}
    files = {"media/v.mp4": 1}

    def run(meta, files=files):
        return handle_framing(t, "gpu1", {"id": "a" * 32, "meta": meta, "files": files}, log.append, cache=tmp_path / "w")
    assert run({**ok, "job_id": "../x"}) is False and run(ok, {}) is False
    for clips in (None, [], "c1", [{"id": "../x", "start": 0, "end": 5}], [{"id": "c1", "start": 5, "end": 1}], [{"id": "c1", "start": "a", "end": 9}],
                  [{"id": "c1", "start": 0, "end": 9e9}], [{"id": f"c{i}", "start": 0, "end": 5} for i in range(61)]):
        assert run({**ok, "clips": clips}) is False
    assert not (tmp_path / "w" / "abc").exists()


# --- apply_framing ---------------------------------------------------------------------------------------------------------

def edl2():
    return EDL.model_validate(base_edl(source={"uri": "src.mp4", "duration": 40, "width": 1920, "height": 1080, "fps": 30},
                                        clips=[{"id": "c1", "source_in": 0, "source_out": 10}, {"id": "c2", "source_in": 20, "source_out": 30}]))


def test_apply_framing_takes_what_the_worker_planned_and_plans_the_rest_here(tmp_path, monkeypatch):
    import clipcrow.framing as fr
    monkeypatch.setattr(fr, "plan_framing", lambda video, a, b, **kw: Framing("fit_blur", coverage=0.0))
    asked, said = [], []

    def worker_fn(video, spans, **kw):
        asked.append((video, [s[0] for s in spans], kw["multi"]))
        return {"c1": (Framing("track", [(0.0, 0.4), (9.0, 0.5)], 0.95, face_zone=(0.3, 0.1, 0.7, 0.4)), 33.0)}              # c2 left to the server
    out = apply_framing(edl2(), "v.mp4", multi="off", say=said.append, framing_fn=worker_fn)
    c1, c2 = out.clips
    assert asked == [("v.mp4", ["c1", "c2"], "off")] and c1.layout.framing == "track" and [(k.t, k.cx) for k in c1.layout.reframe] == [(0.0, 0.4), (9.0, 0.5)]
    assert c2.layout.framing == "fit_blur" and any("c1: track" in m and "[33 s]" in m for m in said)


def test_a_clip_whose_framing_is_already_chosen_is_not_asked_about(tmp_path, monkeypatch):
    import clipcrow.framing as fr
    monkeypatch.setattr(fr, "plan_framing", lambda *a, **k: Framing("fit_blur"))
    e = edl2()
    clips = [e.clips[0].model_copy(update={"layout": e.clips[0].layout.model_copy(update={"framing": "track"})}), e.clips[1]]
    asked = []
    apply_framing(e.model_copy(update={"clips": clips}), "v.mp4", framing_fn=lambda v, spans, **kw: asked.append([s[0] for s in spans]) or {})
    assert asked == [["c2"]]


def test_clips_are_planned_several_at_once_without_changing_the_answer(monkeypatch):
    import clipcrow.framing as fr
    live, peak = [0], [0]
    lock = threading.Lock()

    def slow(video, a, b, **kw):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.15)
        with lock:
            live[0] -= 1
        return Framing("track", [(0.0, round(a / 100, 3))])
    monkeypatch.setattr(fr, "plan_framing", slow)
    spans = [(f"c{i}", float(i * 10), float(i * 10 + 5)) for i in range(1, 7)]
    got = plan_clips("v.mp4", spans, workers=3)
    assert list(got) == [s[0] for s in spans] and got["c3"][0].keyframes == [(0.0, 0.3)] and all(v[1] >= 0.1 for v in got.values())
    assert 1 < peak[0] <= 3
    peak[0] = 0
    plan_clips("v.mp4", spans, detector=object())                                                    # a detector handed in is not shared between threads
    assert peak[0] == 1
