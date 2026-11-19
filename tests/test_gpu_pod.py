"""The pod controller: wakes a sleeping GPU when a job waits for it, pauses it when idle, keeps to a daily budget. No network."""
import pytest

from clipcrow.service.gpu_pod import PodController, PodError, RaceClient, from_env
from clipcrow.service.workers import WorkerHub

TOKEN = "t" * 32


class FakeClient:
    def __init__(self, status="stopped"):
        self.st, self.calls = status, []

    def status(self, pod_id):
        return self.st

    def resume(self, pod_id):
        self.calls.append("resume")
        self.st = "running"
        return "https://pod.invalid/lab?token=SECRET"

    def pause(self, pod_id):
        self.calls.append("pause")
        self.st = "stopped"


def setup(tmp_path, status="stopped", **kw):
    now = [1000.0]
    hub = WorkerHub(TOKEN, clock=lambda: now[0], online_seconds=30.0)
    booted = []
    ctl = PodController(FakeClient(status), hub, "pod1", hourly=45.0, idle_seconds=600, boot=booted.append, clock=lambda: now[0], threaded=False, **kw)
    a = tmp_path / "a.m4a"
    a.write_bytes(b"x")
    return now, hub, ctl, booted, a


def test_a_sleeping_pod_counts_as_available_and_is_woken_for_a_waiting_job(tmp_path):
    now, hub, ctl, booted, a = setup(tmp_path)
    assert hub.online() is False                                  # not looked at the provider yet
    ctl.tick()
    assert ctl.state == "asleep" and hub.online() and hub.online("render") and not hub.online("nonsense")
    hub.submit("j", a, "mr")
    ctl.tick()
    assert ctl.client.calls == ["resume"] and booted == ["https://pod.invalid/lab?token=SECRET"] and ctl.state == "awake"
    assert "SECRET" not in str(ctl.status())


def test_nothing_is_woken_without_a_waiting_job_and_an_idle_pod_is_paused(tmp_path):
    now, hub, ctl, booted, a = setup(tmp_path)
    ctl.tick(); ctl.tick()
    assert ctl.client.calls == []
    hub.submit("j", a, "mr")
    ctl.tick()
    hub.poll("pod", 0.0)                                          # the worker on the pod takes the job
    now[0] = 1100.0
    ctl.tick()
    assert ctl.state == "awake"                                   # busy: stays up
    t = next(iter(hub._tasks.values()))
    hub.finish(t.id, "pod", result={})
    hub._tasks.clear()
    now[0] = 1200.0
    ctl.tick()
    assert ctl.state == "awake"                                   # idle, but not for ten minutes yet
    now[0] = 1200.0 + 601
    ctl.tick()
    assert ctl.client.calls == ["resume", "pause"] and ctl.state == "asleep"


def test_the_daily_budget_stops_waking_and_pauses(tmp_path):
    now, hub, ctl, booted, a = setup(tmp_path, max_daily=1.0)     # ₹1 is 80 s of a ₹45/h pod
    ctl.tick()
    hub.submit("j", a, "mr")
    ctl.tick()
    assert ctl.state == "awake"
    hub._tasks.clear()
    now[0] = 1120.0
    ctl.tick()                                                    # over budget: paused even before the idle time
    assert ctl.state == "asleep" and ctl.status()["spent_today"] >= 1.0
    assert not hub.online()                                       # and no job is queued for a pod that will not be woken
    hub.submit("j2", a, "mr")
    ctl.tick()
    assert ctl.client.calls.count("resume") == 1


def test_a_failed_wake_is_reported_without_secrets_and_retried_after_a_look(tmp_path):
    now, hub, ctl, booted, a = setup(tmp_path)
    ctl.client.resume = lambda pid: (_ for _ in ()).throw(PodError("the pod would not resume"))
    ctl.tick()
    hub.submit("j", a, "mr")
    ctl.tick()
    assert ctl.state == "unknown" and ctl.status()["error"] == "the pod would not resume"
    ctl.tick()
    assert ctl.state in ("asleep", "waking", "unknown")


def test_a_pod_that_is_already_running_is_adopted(tmp_path):
    now, hub, ctl, booted, a = setup(tmp_path, status="running")
    ctl.tick()
    assert ctl.state == "awake" and ctl.client.calls == []


def test_the_race_client_uses_the_documented_calls():
    seen = []

    def http(method, path, body):
        seen.append((method, path, body))
        if path == "pod-list/":
            return {"status": "success", "data": [{"id": "p1", "status": "Running"}]}
        if path == "resume-pod/":
            return {"status": "success", "resume_state": "pending"}
        if path.startswith("resume-status"):
            return {"status": "success", "deploy_state": "ready", "web_url": "https://x/lab?token=T"}
        return {"status": "success"}
    c = RaceClient("k" * 20, http=http)
    assert c.status("p1") == "running" and c.status("other") == "missing"
    assert c.resume("p1", sleep=lambda s: None) == "https://x/lab?token=T"
    c.pause("p1")
    assert ("POST", "pause-pod/", {"pod_id": "p1"}) in seen and ("GET", "resume-status/?pod_id=p1", None) in seen


def test_the_controller_exists_only_when_a_key_and_a_pod_are_configured():
    hub = WorkerHub(TOKEN)
    assert from_env(hub, {}) is None and from_env(hub, {"RACE_GPU_API_KEY": "k" * 20}) is None
    c = from_env(hub, {"RACE_GPU_API_KEY": "k" * 20, "RACE_POD_ID": "p1", "CLIPCROW_POD_HOURLY": "55", "CLIPCROW_POD_IDLE_MINUTES": "5"})
    assert c.hourly == 55.0 and c.idle_seconds == 300.0 and hub.standby is not None
