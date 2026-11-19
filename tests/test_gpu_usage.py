"""The GPU time of a job: what each task kept a worker busy, and what that costs on a rented machine (INR)."""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from clipcrow.service.api import create_app
from clipcrow.service.gpu_cost import load_rates, rate_lookup, row_cost, summarise
from clipcrow.service.workers import WorkerHub
from tests.test_service import FakePipeline, upload, video_bytes, wait  # noqa: F401

TOKEN = "t" * 32


def test_rates_come_from_a_json_setting_and_bad_ones_are_ignored():
    assert load_rates('{"race-3090": 45, "home": 0}') == {"race-3090": 45.0, "home": 0.0}
    assert load_rates("") == {} and load_rates("not json") == {} and load_rates("[1,2]") == {}
    assert load_rates('{"a": "x", "b": -1, "c": true, "d": 1e9, "e": 12.5}') == {"e": 12.5}
    assert rate_lookup({"x": 3.0})("x") == 3.0 and rate_lookup({"x": 3.0})("other") == 0.0           # a machine you own is free


def test_a_row_costs_seconds_over_an_hour_times_the_rate():
    assert row_cost(3600, 45) == 45.0 and row_cost(1800, 45) == 22.5 and row_cost(-5, 45) == 0.0 and row_cost(100, 0) == 0.0


def test_the_summary_totals_seconds_and_rupees_and_says_whether_anything_was_rented():
    rows = [{"kind": "transcribe", "worker": "race-3090", "seconds": 60, "ok": True, "rate": 45},
            {"kind": "render", "worker": "race-3090", "seconds": 120, "ok": False, "rate": 45},
            {"kind": "speakers", "worker": "home", "seconds": 30, "ok": True, "rate": 0}, {"kind": "render", "seconds": "bad"}]
    s = summarise(rows)
    assert s["currency"] == "INR" and s["seconds"] == 210.0 and s["est_cost"] == 2.25 and s["rented"] is True and len(s["tasks"]) == 3
    assert [t["step"] for t in s["tasks"]] == ["Speech recognition", "Render", "Who is who"] and s["tasks"][1]["ok"] is False
    own = summarise([{"kind": "render", "worker": "home", "seconds": 90, "ok": True, "rate": 0}])
    assert own["rented"] is False and own["est_cost"] == 0.0 and summarise([])["tasks"] == []


def test_the_hub_reports_how_long_a_worker_kept_a_task_and_a_broken_sink_changes_nothing(tmp_path):
    now = [100.0]
    seen = []
    h = WorkerHub(TOKEN, clock=lambda: now[0], on_task_end=seen.append, rate_for=lambda n: 45.0 if n == "race" else 0.0)
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")
    t = h.submit("job1", audio, "mr")
    assert h.poll("race", 0.0) is t
    now[0] = 112.5
    assert h.finish(t.id, "race", result={"words": []})
    assert seen == [{"job": "job1", "kind": "transcribe", "worker": "race", "seconds": 12.5, "ok": True, "rate": 45.0}]
    t2 = h.submit("job1", audio, "mr")
    assert h.poll("home", 0.0) is t2
    now[0] = 130.0
    assert h.finish(t2.id, "home", error="boom") and seen[-1]["ok"] is False and seen[-1]["rate"] == 0.0 and seen[-1]["seconds"] == 17.5
    broken = WorkerHub(TOKEN, on_task_end=lambda r: 1 / 0)
    t3 = broken.submit("job1", audio, "mr")
    broken.poll("w", 0.0)
    assert broken.finish(t3.id, "w", result={})                                      # bookkeeping must not fail the task


def test_a_jobs_usage_lists_gpu_time_with_its_cost(tmp_path, video_bytes):
    app = create_app(tmp_path / "data", FakePipeline(), worker_token=TOKEN)
    c = TestClient(app)
    jid = wait(c, upload(c, video_bytes).json()["id"])["id"]
    assert c.get(f"/api/jobs/{jid}/usage").json()["gpu"]["tasks"] == []
    app.state.store.add_gpu_usage(jid, {"kind": "render", "worker": "race-3090", "seconds": 900.0, "ok": True, "rate": 45.0, "at": "2026-11-19T10:00:00+00:00"})
    g = c.get(f"/api/jobs/{jid}/usage").json()["gpu"]
    assert g["currency"] == "INR" and g["est_cost"] == 11.25 and g["seconds"] == 900.0 and g["rented"] is True
    assert g["tasks"][0]["worker"] == "race-3090" and g["tasks"][0]["step"] == "Render"


def _hub(**kw):
    return WorkerHub(TOKEN, pickup_seconds=5.0, lease_seconds=5.0, online_seconds=30.0, **kw)


def test_a_stronger_idle_worker_is_offered_the_task_first(tmp_path):
    import threading
    h = _hub()
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")
    got = {}
    strong = threading.Thread(target=lambda: got.__setitem__("strong", h.poll("pod", 3.0, power=105)))
    strong.start()
    import time
    time.sleep(0.2)                                                    # the strong worker is now waiting in its poll
    task = h.submit("j", audio, "mr")
    weak = h.poll("home", 1.0, power=10)                               # a weaker one asks while the stronger waits: it must leave it
    strong.join(5)
    assert weak is None and got["strong"] is task and task.worker == "pod"


def test_a_weaker_worker_takes_it_when_the_stronger_is_busy_or_the_task_has_waited(tmp_path):
    now = [0.0]
    h = WorkerHub(TOKEN, clock=lambda: now[0], online_seconds=30.0)
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")
    h.poll("pod", 0.0, power=105)                                      # seen, but not waiting (busy elsewhere)
    t = h.submit("j", audio, "mr")
    assert h.poll("home", 0.0, power=10) is t                           # the stronger is not idle, so nobody waits for it


def test_extra_render_slots_are_one_machine_and_share_its_price():
    from clipcrow.service.workers import _machine
    assert _machine("race-3090.r2") == "race-3090" and _machine("a.rx") == "a.rx" and _machine("race-3090") == "race-3090"
    assert rate_lookup({"race-3090": 45.0})("race-3090.r3") == 45.0


def test_overlapping_tasks_on_one_machine_split_the_hourly_price(tmp_path):
    now = [0.0]
    seen = []
    h = WorkerHub(TOKEN, clock=lambda: now[0], on_task_end=seen.append, rate_for=rate_lookup({"pod": 90.0}))
    a = tmp_path / "a.m4a"
    a.write_bytes(b"x")
    t1, t2 = h.submit("j", a, "mr"), h.submit("j", a, "mr")
    assert h.poll("pod", 0.0, can=("transcribe",)) is t1
    assert h.poll("pod.r2", 0.0, can=("transcribe",)) is t2
    now[0] = 60.0
    h.finish(t1.id, "pod", result={})
    assert seen[-1]["rate"] == 45.0                                    # two overlapped, so each carries half of ₹90/h
    h.finish(t2.id, "pod.r2", result={})
    assert seen[-1]["rate"] == 90.0


def test_the_worker_reports_its_capacity_and_runs_extra_render_slots():
    from clipcrow.worker import capacity
    assert capacity("NVIDIA GeForce RTX 3090 (cuda)") > capacity("cpu") and capacity("cuda", 3) == 110 and capacity("") == 10


def test_one_star_rate_prices_every_machine_a_home_gpu_included():
    r = load_rates('{"*": 45, "race-3090": 50}')
    look = rate_lookup(r)
    assert look("race-3090.r2") == 50.0 and look("home-pc") == 45.0 and rate_lookup({})("home-pc") == 0.0


def test_a_job_can_ask_for_the_home_gpu_or_the_on_demand_one(tmp_path):
    from types import SimpleNamespace
    from clipcrow.service.workers import hub_view
    h = _hub()
    a = tmp_path / "a.m4a"
    a.write_bytes(b"x")
    h.poll("home-pc", 0.0, pool="home", power=100)
    h.poll("pod", 0.0, pool="rented", power=105)
    job = lambda g: SimpleNamespace(params=SimpleNamespace(gpu=g))
    assert hub_view(h, job("home")).online() and hub_view(h, job("pod")).online() and hub_view(h, job("auto")).online() and hub_view(None, job("auto")) is None
    t = hub_view(h, job("pod")).submit("j", a, "mr")
    assert t.pool == "rented"
    assert h.poll("home-pc", 0.0, pool="home") is None             # the home worker may not take an on-demand job
    assert h.poll("pod", 0.0, pool="rented") is t
    t2 = hub_view(h, job("home")).submit("j", a, "mr")
    assert h.poll("pod", 0.0, pool="rented") is None and h.poll("home-pc", 0.0, pool="home") is t2


def test_asking_for_home_never_counts_a_sleeping_pod_as_available():
    h = _hub()
    h.standby = lambda kind, direct: True
    h.poll("pod", 0.0, pool="rented")
    assert h.online(pool="home") is False and h.online(pool="rented") is True


def test_the_estimate_scales_with_length_and_price():
    from clipcrow.service.gpu_cost import estimate
    short, long = estimate(600, 3, 45.0), estimate(3600, 3, 45.0)
    assert long["seconds"] > short["seconds"] and long["est_cost"] > short["est_cost"]
    assert estimate(600, 3, 0.0)["est_cost"] == 0.0 and estimate(600, 3, 45.0, wake=True)["seconds"] > short["seconds"]


def test_the_browser_gets_the_estimate_numbers_with_the_markup_in_the_config(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPCROW_GPU_RATES", '{"*": 20, "rented": 45}')
    app = create_app(tmp_path / "data", FakePipeline(), worker_token=TOKEN)
    n = TestClient(app).get("/api/config").json()["gpu_worker"]["estimate"]
    assert n["markup"] == 1.5 and n["home_rate"] == 20.0 and n["pod_rate"] == 45.0 and n["listen"] > 0 and n["render"] > 0 and n["wake"] > 0


def test_the_config_carries_the_language_model_numbers_for_the_estimate(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPCROW_USD_INR", "90")
    app = create_app(tmp_path / "data", FakePipeline(), worker_token=TOKEN)
    l = TestClient(app).get("/api/config").json()["gpu_worker"]["estimate"]["llm"]
    assert l["usd_inr"] == 90.0 and l["usd_in"] > 0 and l["usd_out"] > l["usd_in"] and l["in_per_minute"] > 0 and l["out_per_reel"] > 0


def test_the_margin_is_set_in_the_environment_and_falls_back_to_fifty_percent(monkeypatch):
    from clipcrow.service.gpu_cost import estimate_numbers
    monkeypatch.delenv("CLIPCROW_MARGIN_PERCENT", raising=False)
    assert estimate_numbers({})["markup"] == 1.5 and estimate_numbers({})["margin_percent"] == 50.0
    monkeypatch.setenv("CLIPCROW_MARGIN_PERCENT", "30")
    assert estimate_numbers({})["markup"] == 1.3
    monkeypatch.setenv("CLIPCROW_MARGIN_PERCENT", "oops")
    assert estimate_numbers({})["markup"] == 1.5
    monkeypatch.setenv("CLIPCROW_MARGIN_PERCENT", "-20")
    assert estimate_numbers({})["markup"] == 1.0


def test_when_the_pod_cannot_wake_a_connected_home_worker_gets_the_task_before_the_server(tmp_path):
    import threading
    from clipcrow.service.workers import WorkerUnavailable
    h = _hub()
    a = tmp_path / "a.m4a"
    a.write_bytes(b"x")
    h.standby = lambda kind, direct: True                           # the pod looks wakeable ...
    h.why_unavailable = lambda: "the GPU provider answered 503 to resume-pod/"    # ... but its last wake failed
    h.poll("home-pc", 0.0, pool="home", power=100)
    t = h.submit("j", a, "mr", pool="rented")
    said = []
    done = {}
    th = threading.Thread(target=lambda: done.__setitem__("r", h.wait(t, lambda: None, say=said.append)))
    th.start()
    import time
    time.sleep(0.5)
    assert t.pool == "" and any("any other connected" in m and "not ready" in m for m in said) and any("wake up" in m for m in said)
    got = h.poll("home-pc", 1.0, pool="home", power=100)
    assert got is t
    h.finish(t.id, "home-pc", result={"ok": 1})
    th.join(5)
    assert done["r"] == {"ok": 1}


def test_with_no_other_worker_connected_the_job_goes_to_the_server(tmp_path):
    import pytest as _pt
    from clipcrow.service.workers import WorkerUnavailable
    h = _hub()
    a = tmp_path / "a.m4a"
    a.write_bytes(b"x")
    h.standby = lambda kind, direct: False
    t = h.submit("j", a, "mr", pool="rented")
    with _pt.raises(WorkerUnavailable):
        h.wait(t, lambda: None)


def test_a_home_only_job_may_use_the_pod_when_no_home_worker_is_connected(tmp_path):
    h = _hub()
    a = tmp_path / "a.m4a"
    a.write_bytes(b"x")
    h.standby = lambda kind, direct: True
    t = h.submit("j", a, "mr", pool="home")
    said = []
    import threading, time
    th = threading.Thread(target=lambda: h.wait(t, lambda: None, say=said.append), daemon=True)
    th.start()
    time.sleep(0.5)
    assert t.pool == "" and said


def test_renders_go_only_to_a_worker_running_the_same_render_code_as_the_server(tmp_path):
    from clipcrow.buildid import render_build
    b = render_build()
    assert len(b) == 12 and b == render_build()
    h = WorkerHub(TOKEN, render_pin="hf1", render_build=b, online_seconds=30.0)
    h.poll("old", 0.0, can=("transcribe", "render"), pin="hf1", build="")                 # a worker from before the check, or an older release
    h.poll("other", 0.0, can=("transcribe", "render"), pin="hf1", build="0123456789ab")
    assert not h.online("render") and h.stale_workers() == ["old", "other"]
    assert h.online("transcribe")                                                          # speech recognition does not depend on the render code
    h.poll("new", 0.0, can=("transcribe", "render"), pin="hf1", build=b)
    assert h.online("render") and h.stale_workers() == ["old", "other"]
    assert WorkerHub(TOKEN, render_pin="hf1").stale_workers() == []                         # no build on the server: nothing is checked


def test_the_job_log_names_who_did_a_task_and_how_long_it_took(tmp_path):
    now = [100.0]
    h = WorkerHub(TOKEN, clock=lambda: now[0])
    a = tmp_path / "a.m4a"
    a.write_bytes(b"x")
    t = h.submit("j", a, "mr")
    h.poll("race-3090", 0.0, pool="rented")
    now[0] = 190.0
    h.finish(t.id, "race-3090", result={})
    assert h.who(t) == "race-3090, on-demand GPU, 90 s"
    t2 = h.submit("j", a, "mr")
    h.poll("home-gpu", 0.0, pool="home")
    now[0] = 195.0
    h.finish(t2.id, "home-gpu", result={})
    assert h.who(t2) == "home-gpu, home GPU, 5 s"


def test_the_config_names_workers_that_run_older_render_code(tmp_path):
    app = create_app(tmp_path / "data", FakePipeline(), worker_token=TOKEN)
    c = TestClient(app)
    c.post("/api/worker/poll", json={"worker": "old-pc", "wait": 0, "can": ["transcribe", "render"], "pin": "x"}, headers={"Authorization": f"Bearer {TOKEN}"})
    assert c.get("/api/config").json()["gpu_worker"]["stale"] == ["old-pc"]
