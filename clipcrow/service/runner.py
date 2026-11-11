"""Runs jobs one at a time in a background thread and records their progress in the store."""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Protocol

from ..edl import EDL, EDLError
from ..make import Cancelled, Hooks, MakeResult
from ..planner.llm import LLMError
from ..planner.plan import PlanError
from ..planner.transcript import TranscriptError
from ..render import RenderError
from .jobs import ClipInfo, Job, JobStore, LeftOut

log = logging.getLogger("clipcrow.service")
# Errors whose message is written for a person (bad input, no speech, a model refusal, a failed render).
EXPECTED = (EDLError, PlanError, TranscriptError, LLMError, RenderError, ValueError)


class Pipeline(Protocol):
    def check_ready(self) -> str | None:
        """None when jobs can run, else a sentence saying what is missing."""

    def run(self, job: Job, video: Path, out: Path, hooks: Hooks, say) -> MakeResult: ...


def clip_infos(edl: EDL, ready: set[str] = frozenset()) -> list[ClipInfo]:
    out = []
    for c in edl.clips:
        hook = next((o.text for o in c.overlays if o.kind == "hook"), "")
        out.append(ClipInfo(
            id=c.id, title=c.title, hook=hook, start=c.source_in, end=c.source_out,
            score=c.ai.score if c.ai else None, why=c.ai.why if c.ai else "",
            ai_decided=list(c.ai.decided) if c.ai else [], framing=c.layout.framing, ready=c.id in ready))
    return out


class JobRunner:
    def __init__(self, store: JobStore, pipeline: Pipeline):
        self.store, self.pipeline = store, pipeline
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="clipcrow-job")
        self._cancel: dict[str, threading.Event] = {}

    def recover(self) -> None:
        """After a restart: queued jobs go back in the queue, interrupted ones are marked failed."""
        for job in reversed(self.store.list()):
            if job.status == "queued":
                self.submit(job.id)
            elif job.status == "running":
                job.status, job.error = "failed", "The server restarted while this job was running."
                self.store.save(job)

    def submit(self, job_id: str) -> None:
        self._cancel[job_id] = threading.Event()
        self._pool.submit(self._run, job_id)

    def cancel(self, job_id: str) -> bool:
        job = self.store.get(job_id)
        if job.status in ("done", "failed", "cancelled"):
            return False
        ev = self._cancel.get(job_id)
        if ev:
            ev.set()
        if job.status == "queued":
            job.status = "cancelled"
            self.store.save(job)
        return True

    def queue_position(self, job_id: str) -> int:
        queued = [j for j in reversed(self.store.list()) if j.status == "queued"]
        return next((i + 1 for i, j in enumerate(queued) if j.id == job_id), 0)

    def _run(self, job_id: str) -> None:
        store = self.store
        job = store.get(job_id)
        if job.status != "queued":
            return                                              # cancelled while waiting
        ev = self._cancel.setdefault(job_id, threading.Event())
        job.status = "running"
        store.save(job)
        ready: set[str] = set()

        def stage(name: str, state: str, detail: str = "") -> None:
            s = job.stage(name)
            s.state, s.detail = state, detail
            store.save(job)

        def say(msg: str) -> None:
            job.add_log(msg)
            store.save(job)

        def planned(edl: EDL, report: dict) -> None:
            job.clips = clip_infos(edl)
            job.left_out = [LeftOut(start=d["start"], end=d["end"], reason=d["reason"])
                            for d in report.get("dropped", []) if "start" in d]
            store.save(job)

        def clip_ready(clip_id: str, _path: Path) -> None:
            ready.add(clip_id)
            for c in job.clips:
                if c.id == clip_id:
                    c.ready = True
            store.save(job)

        hooks = Hooks(stage=stage, clip_ready=clip_ready, planned=planned, cancelled=ev.is_set)
        src = store.source_path(job_id)
        try:
            if src is None:
                raise ValueError("the uploaded video is missing")
            self.pipeline.run(job, src, store.dir(job_id) / "out", hooks, say)
            job.status = "done"
        except Cancelled:
            job.status = "cancelled"
            job.add_log("cancelled")
        except EXPECTED as e:
            job.status, job.error = "failed", str(e)[:500]
        except Exception as e:                                  # never show internals to the browser
            log.exception("job %s failed", job_id)
            job.status, job.error = "failed", f"Something went wrong on the server ({type(e).__name__}). See the server log."
        for s in job.stages:
            if s.state == "running":
                s.state = "failed" if job.status == "failed" else "pending"
        store.save(job)
