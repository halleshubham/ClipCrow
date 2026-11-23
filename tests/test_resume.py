"""A job continues from a step it already finished: after a restart by itself, and from a step a person picks."""
import json

import pytest

from clipcrow.service import resume
from clipcrow.service.jobs import Job, JobStore, Params
from tests.test_make import fake_transcribe, video  # noqa: F401  (fixture)
from tests.test_planner import make_transcript, scripted
from tests.test_service import client, video_bytes  # noqa: F401  (fixtures)


def finished(job, *names):
    for n in names:
        job.stage(n).state = "done"
    return job


def files(out, **present):
    out.mkdir(parents=True, exist_ok=True)
    for name, text in present.items():
        (out / name.replace("_", ".", 1) if False else out / name).write_text(text, encoding="utf-8")


# --- the rules ------------------------------------------------------------------------------------------------------

def test_a_step_is_offered_only_when_its_stage_is_done_and_its_file_is_there(tmp_path):
    job = Job(id="a" * 12, name="x", created_at="t")
    out = tmp_path / "out"
    out.mkdir()
    assert resume.available(job, out) == [] and resume.resolve(job, out, "auto") is None
    finished(job, "transcribe")
    assert resume.available(job, out) == []                                # "done" without its file is not trusted
    (out / "transcript.json").write_text("{}")
    assert resume.available(job, out) == ["plan"]
    finished(job, "plan")
    (out / "edl.json").write_text("{}")
    assert resume.available(job, out) == ["plan", "frame"]
    finished(job, "frame")
    assert resume.available(job, out) == ["plan", "frame", "render"]
    assert resume.resolve(job, out, "auto") == "render" and resume.resolve(job, out, "frame") == "frame" and resume.resolve(job, out, None) is None
    (out / "edl.json").unlink()
    assert resume.available(job, out) == ["plan"] and resume.resolve(job, out, "render") is None


def test_resetting_from_a_step_keeps_the_earlier_ones_finished():
    job = finished(Job(id="a" * 12, name="x", created_at="t"), "transcribe", "plan", "assets", "frame", "render")
    resume.reset_from(job, "frame")
    assert [s.state for s in job.stages] == ["done", "done", "pending", "pending", "pending"][:len(job.stages)] or \
        {s.name: s.state for s in job.stages}["plan"] == "done" and {s.name: s.state for s in job.stages}["frame"] == "pending"
    states = {s.name: s.state for s in job.stages}
    assert states["transcribe"] == "done" and states["plan"] == "done" and states["frame"] == "pending" and states["render"] == "pending"
    resume.reset_from(job, None)
    assert all(s.state == "pending" for s in job.stages)


def test_progress_counts_finished_steps_and_ready_clips():
    job = finished(Job(id="a" * 12, name="x", created_at="t"), "transcribe", "plan")
    assert resume.progress(job) == 2


# --- make(): finished clips are kept, nothing earlier is repeated -----------------------------------------------------------

def test_continuing_from_render_renders_only_the_missing_clips_and_calls_nothing_earlier(video, tmp_path):
    from clipcrow.make import Hooks, make
    from clipcrow.planner import FakeLLM, PlannerOptions
    out = tmp_path / "out"

    def draw(edl, clip_id, path, **kw):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"mp4")
        return path, None
    first = make(video, out, llm=scripted(), opts=PlannerOptions(target_clips=2, workers=1), transcribe_fn=fake_transcribe(make_transcript()), render_fn=draw)
    ids = [c.id for c in first.edl.clips]
    assert len(ids) >= 2
    (out / f"{ids[-1]}.mp4").unlink()                                      # the last clip never finished before the "restart"
    made, stages = [], []

    def draw_again(edl, clip_id, path, **kw):
        made.append(clip_id)
        return draw(edl, clip_id, path, **kw)

    def boom(*a, **k):
        raise AssertionError("an earlier step was done again")
    hk = Hooks(stage=lambda name, state, detail="": stages.append((name, state)))
    res = make(video, out, edl_path=out / "edl.json", skip_frame=True, skip_render=set(ids[:-1]), llm=FakeLLM(boom), transcribe_fn=boom, render_fn=draw_again, hooks=hk)
    assert made == [ids[-1]] and [p.name for p in res.videos] == [f"{i}.mp4" for i in ids]
    assert not any(name in ("plan", "frame", "assets", "transcribe") and state in ("running", "done") for name, state in stages)      # none of the earlier steps ran
    assert ("render", "done") in stages


def test_a_kept_clip_whose_file_is_gone_is_rendered_again(video, tmp_path):
    from clipcrow.make import make
    from clipcrow.planner import FakeLLM, PlannerOptions
    out = tmp_path / "out"
    first = make(video, out, llm=scripted(), opts=PlannerOptions(target_clips=1, workers=1), transcribe_fn=fake_transcribe(make_transcript()), render=False)
    cid = first.edl.clips[0].id
    made = []

    def draw(edl, clip_id, path, **kw):
        made.append(clip_id)
        path.write_bytes(b"mp4")
        return path, None
    make(video, out, edl_path=out / "edl.json", skip_frame=True, skip_render={cid}, llm=FakeLLM(lambda *a, **k: 1 / 0), render_fn=draw)
    assert made == [cid]                                                    # "kept" needs the file as well as the claim


# --- the pipeline hands make() the right pieces ------------------------------------------------------------------------------

@pytest.mark.parametrize("step", ["plan", "frame", "render"])
def test_the_real_pipeline_passes_what_each_step_needs(tmp_path, monkeypatch, step):
    from clipcrow.service import pipelines
    store = JobStore(tmp_path)
    job = store.create("a.mp4", Params(clean=True))
    out = store.dir(job.id) / "out"
    (store.dir(job.id) / "source.mp4").write_bytes(b"src")
    (out / "clean.mp4").write_bytes(b"cut")                                  # the cleanup ran: this is the video the plan is on
    (out / "transcript.json").write_text("{}")
    (out / "edl.json").write_text("{}")
    (out / "c1.mp4").write_bytes(b"x")
    job.clips = []
    finished(job, "transcribe", "plan", "frame")
    from clipcrow.service.jobs import ClipInfo
    job.clips = [ClipInfo(id="c1", title="t", hook="h", start=0, end=5, framing="cover", ready=True), ClipInfo(id="c2", title="t", hook="h", start=5, end=9, framing="cover", ready=False)]
    job.resume_from = step
    store.save(job)
    seen = {}
    monkeypatch.setattr(pipelines, "make", lambda video, out_dir, **kw: seen.update(video=video, **kw) or "made")
    p = pipelines.RealPipeline()
    p.store = store
    says = []
    from clipcrow.make import Hooks
    p.run(store.get(job.id), store.dir(job.id) / "source.mp4", out, Hooks(), says.append)
    assert seen["video"] == out / "clean.mp4" and seen["clean"] is False                 # never the original, and the cleanup is not repeated
    assert any("continuing from the" in s for s in says)
    if step == "plan":
        assert seen["transcript_path"] == out / "transcript.json" and "edl_path" not in seen
    else:
        assert seen["edl_path"] == out / "edl.json" and "transcript_path" not in seen
    if step == "render":
        assert seen["skip_frame"] is True and seen["skip_render"] == {"c1"}              # c2 was never ready: it is rendered
    else:
        assert not seen.get("skip_frame")


def test_without_the_saved_results_the_pipeline_starts_over(tmp_path, monkeypatch):
    from clipcrow.make import Hooks
    from clipcrow.service import pipelines
    store = JobStore(tmp_path)
    job = store.create("a.mp4", Params())
    (store.dir(job.id) / "source.mp4").write_bytes(b"src")
    finished(job, "transcribe", "plan")                                       # claims done, but no transcript or EDL on disk
    job.resume_from = "auto"
    store.save(job)
    seen = {}
    monkeypatch.setattr(pipelines, "make", lambda video, out_dir, **kw: seen.update(kw) or "made")
    p = pipelines.RealPipeline()
    p.store = store
    says = []
    p.run(store.get(job.id), store.dir(job.id) / "source.mp4", store.dir(job.id) / "out", Hooks(), says.append)
    assert "transcript_path" not in seen and "edl_path" not in seen and any("starting from the beginning" in s for s in says)


# --- after a restart ------------------------------------------------------------------------------------------------------

def interrupted(tmp_path, video_bytes, *, done=(), attempts=0, at_restart=-1, with_files=False, ready=()):
    c, app = client(tmp_path, start_runner=False)
    store = app.state.store
    job = store.create("a.mp4", Params())
    (store.dir(job.id) / "source.mp4").write_bytes(video_bytes)
    out = store.dir(job.id) / "out"
    if with_files:
        (out / "transcript.json").write_text("{}")
        (out / "edl.json").write_text("{}")
    job.status, job.attempts, job.progress_at_restart = "running", attempts, at_restart
    finished(job, *done)
    job.stage("render").state = "running"
    from clipcrow.service.jobs import ClipInfo
    job.clips = [ClipInfo(id=i, title="t", hook="h", start=0, end=4, framing="cover", ready=True) for i in ready]
    for i in ready:
        (out / f"{i}.mp4").write_bytes(b"x")
    store.save(job)
    return job.id


def restart(tmp_path):
    """What a restart does: a new server over the same data runs recover(); the jobs it queues are collected, not run."""
    c, app = client(tmp_path, start_runner=False)
    queued = []
    app.state.runner.submit = queued.append
    app.state.runner.recover()
    return app, queued


def test_a_restart_keeps_the_finished_steps_and_continues_from_the_furthest(tmp_path, video_bytes):
    jid = interrupted(tmp_path, video_bytes, done=("transcribe", "plan", "assets", "frame"), with_files=True, ready=("c1",))
    app2, queued = restart(tmp_path)
    job = app2.state.store.get(jid)
    assert queued == [jid] and job.status == "queued" and job.attempts == 1 and job.resume_from == "auto"
    assert {s.name: s.state for s in job.stages}["frame"] == "done" and {s.name: s.state for s in job.stages}["render"] == "pending"
    assert any("continues from the render step" in line for line in job.log)


def test_a_job_with_nothing_saved_still_starts_again_by_itself(tmp_path, video_bytes):
    jid = interrupted(tmp_path, video_bytes)
    app2, queued = restart(tmp_path)
    job = app2.state.store.get(jid)
    assert job.status == "queued" and job.resume_from is None and any("starts again by itself" in line for line in job.log)


def test_a_job_that_got_further_since_the_last_restart_gets_another_try_but_one_that_did_not_fails(tmp_path, video_bytes):
    far = interrupted(tmp_path, video_bytes, done=("transcribe", "plan", "assets", "frame"), with_files=True, attempts=1, at_restart=1)
    app2, _ = restart(tmp_path)
    assert app2.state.store.get(far).status == "queued" and app2.state.store.get(far).attempts == 2
    same = interrupted(tmp_path / "b", video_bytes, done=("transcribe",), with_files=True, attempts=1, at_restart=1)
    app3, _ = restart(tmp_path / "b")
    assert app3.state.store.get(same).status == "failed" and "restarted" in app3.state.store.get(same).error


def test_the_restart_limit_stops_a_job_that_keeps_getting_further_and_dying(tmp_path, video_bytes):
    jid = interrupted(tmp_path, video_bytes, done=("transcribe", "plan", "assets", "frame"), with_files=True, attempts=resume.MAX_RESTARTS, at_restart=0)
    app2, _ = restart(tmp_path)
    assert app2.state.store.get(jid).status == "failed"


# --- choosing the step for one job -------------------------------------------------------------------------------------------

def failed_job(app, video_bytes, *, done=(), with_files=False):
    store = app.state.store
    job = store.create("a.mp4", Params())
    (store.dir(job.id) / "source.mp4").write_bytes(video_bytes)
    if with_files:
        (store.dir(job.id) / "out" / "transcript.json").write_text("{}")
        (store.dir(job.id) / "out" / "edl.json").write_text("{}")
    finished(job, *done)
    job.status, job.error = "failed", "x"
    store.save(job)
    return job.id


def test_the_job_says_which_steps_it_can_continue_from(tmp_path, video_bytes):
    c, app = client(tmp_path, start_runner=False)
    jid = failed_job(app, video_bytes, done=("transcribe", "plan", "frame"), with_files=True)
    assert c.get(f"/api/jobs/{jid}").json()["resumable"] == ["plan", "frame", "render"]
    bare = failed_job(app, video_bytes)
    assert c.get(f"/api/jobs/{bare}").json()["resumable"] == []


def test_run_again_from_a_chosen_step_keeps_the_earlier_ones(tmp_path, video_bytes):
    c, app = client(tmp_path, start_runner=False)
    jid = failed_job(app, video_bytes, done=("transcribe", "plan", "frame"), with_files=True)
    r = c.post(f"/api/jobs/{jid}/retry", json={"resume_from": "frame"})
    assert r.status_code == 200, r.text
    job = app.state.store.get(jid)
    states = {s.name: s.state for s in job.stages}
    assert job.resume_from == "frame" and states["transcribe"] == "done" and states["plan"] == "done" and states["frame"] == "pending" and states["render"] == "pending"
    assert any("continuing from the frame step" in line for line in job.log)


def test_a_step_without_its_saved_result_is_refused_and_plain_run_again_starts_over(tmp_path, video_bytes):
    c, app = client(tmp_path, start_runner=False)
    jid = failed_job(app, video_bytes, done=("transcribe", "plan"))             # no files on disk
    r = c.post(f"/api/jobs/{jid}/retry", json={"resume_from": "render"})
    assert r.status_code == 409 and "cannot be continued" in r.json()["detail"]
    assert app.state.store.get(jid).status == "failed"                           # nothing was changed or charged
    ok = c.post(f"/api/jobs/{jid}/retry")
    assert ok.status_code == 200
    job = app.state.store.get(jid)
    assert job.resume_from is None and all(s.state == "pending" for s in job.stages)


def test_the_ui_offers_the_continue_choice():
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "clipcrow" / "service" / "static" / "app.js").read_text(encoding="utf-8")
    assert "resume_from" in js and "job.resumable" in js and "Start over" in js
