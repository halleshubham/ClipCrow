"""Cancelling a running job: noticed inside a long AI call and between recognition windows, never swallowed by a "carry on" handler, and remembered across a restart."""
import threading
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("multipart")

from clipcrow.make import Cancelled, Hooks
from clipcrow.planner import FakeLLM
from clipcrow.planner.llm import Interruptible
from clipcrow.planner.models import BeatsResult
from clipcrow.service.jobs import Job
from tests.test_service import FakePipeline, client, upload, video_bytes, wait  # noqa: F401


def test_a_cancel_is_noticed_while_an_ai_call_is_still_running_and_the_caller_returns_at_once():
    release = threading.Event()
    stop = threading.Event()

    def slow(purpose, prompt, schema):
        release.wait(10)                                      # a call that takes "minutes"
        return BeatsResult(beats=[])

    def check():
        if stop.is_set():
            raise Cancelled()
    llm = Interruptible(FakeLLM(slow), check, poll=0.02)
    threading.Timer(0.2, stop.set).start()
    t0 = time.time()
    with pytest.raises(Cancelled):
        llm.generate("s", "u", BeatsResult, purpose="x")
    assert time.time() - t0 < 2
    release.set()


def test_a_normal_call_passes_its_answer_and_its_errors_through_and_usage_stays_visible():
    ok = Interruptible(FakeLLM(lambda p, u, s: BeatsResult(beats=[])), lambda: None, poll=0.01)
    assert ok.generate("s", "u", BeatsResult, purpose="x") == BeatsResult(beats=[])
    assert len(ok.usage()) == 1 and ok.calls[0][0] == "x"      # the wrapped model records the call and its attributes are reachable
    boom = Interruptible(FakeLLM(lambda p, u, s: RuntimeError("down")), lambda: None, poll=0.01)
    with pytest.raises(RuntimeError, match="down"):
        boom.generate("s", "u", BeatsResult, purpose="x")


def test_a_cancel_is_not_an_ordinary_error_so_carry_on_handlers_cannot_swallow_it():
    assert not issubclass(Cancelled, Exception)
    try:
        try:
            raise Cancelled()
        except Exception:                                      # what the recogniser, cleanup and text correction do with a failure
            pytest.fail("a cancel was swallowed")
    except Cancelled:
        pass


def test_the_text_correction_and_the_recogniser_stop_instead_of_carrying_on():
    from clipcrow import asr, textfix
    from tests.test_textfix import tr_of

    def check():
        raise Cancelled()
    with pytest.raises(Cancelled):
        textfix.refine(tr_of(("नहीं", "आहे")), Interruptible(FakeLLM(lambda p, u, s: textfix.Fixes(items=[])), check))

    class M:
        def transcribe(self, *a, **k):
            pytest.fail("a window was decoded after the cancel")
    import numpy as np
    with pytest.raises(Cancelled):
        asr.transcribe_windowed(M(), "x", "mr", audio_loader=lambda m: np.zeros(asr.SAMPLE_RATE * 20, np.float32), check=check)


def test_the_stop_flag_is_saved_and_a_restart_ends_the_job_as_cancelled_instead_of_running_it_again(tmp_path, video_bytes):
    c, app = client(tmp_path)
    store, runner = app.state.store, app.state.runner
    job = store.create("a.mp4", __import__("clipcrow.service.jobs", fromlist=["Params"]).Params())
    (store.dir(job.id) / "source.mp4").write_bytes(video_bytes)
    job.status = "running"
    store.save(job)
    runner._cancel[job.id] = threading.Event()
    assert runner.cancel(job.id) is True
    again = store.get(job.id)
    assert again.status == "running" and again.cancel_requested is True and runner._cancel[job.id].is_set()
    runner.recover()                                           # what a restart does with a job that was running
    final = store.get(job.id)
    assert final.status == "cancelled" and "cancelled" in final.log[-1]


def test_a_deploy_does_not_put_a_stopped_job_back_in_the_queue(tmp_path, video_bytes):
    c, app = client(tmp_path)
    store, runner = app.state.store, app.state.runner
    from clipcrow.service.jobs import Params
    job = store.create("a.mp4", Params())
    job.status, job.cancel_requested = "running", True
    store.save(job)
    runner._cancel[job.id] = threading.Event()
    runner.drain(0.0, 0.0)
    assert job.id not in runner._requeue and runner._cancel[job.id].is_set()
