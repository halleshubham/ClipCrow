"""Runs jobs one at a time in a background thread and records their progress in the store."""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional, Protocol

from ..edl import EDL, EDLError, load_edl
from ..make import Cancelled, Hooks, MakeResult
from ..planner.llm import LLMError
from ..planner.plan import PlanError
from .workers import hub_view
from ..planner.transcript import TranscriptError
from ..render import RenderError
from .storage import StorageError
from .jobs import ClipInfo, Job, JobStore, LeftOut

log = logging.getLogger("clipcrow.service")
# Errors whose message is written for a person (bad input, no speech, a model refusal, a failed render).
EXPECTED = (EDLError, PlanError, TranscriptError, LLMError, RenderError, StorageError, ValueError)


class Pipeline(Protocol):
    def check_ready(self) -> str | None:
        """None when jobs can run, else a sentence saying what is missing."""

    def run(self, job: Job, video: Path, out: Path, hooks: Hooks, say) -> MakeResult: ...

    def render_clip(self, job: Job, edl: EDL, clip_id: str, out: Path, workdir: Path, media_root: Path) -> list[str]:
        """Render one clip of ``edl`` to ``out`` (used after an edit); returns warnings for a person to read."""


def clip_infos(edl: EDL, ready: set[str] = frozenset()) -> list[ClipInfo]:
    out = []
    for c in edl.clips:
        hook = next((o.text for o in c.overlays if o.kind == "hook"), "")
        out.append(ClipInfo(
            id=c.id, title=c.title, hook=hook, start=c.source_in, end=c.source_out,
            score=c.ai.score if c.ai else None, why=c.ai.why if c.ai else "",
            ai_decided=list(c.ai.decided) if c.ai else [], framing=c.layout.framing, ready=c.id in ready,
            has_cover=bool(c.cover and c.cover.enabled)))
    return out


class JobRunner:
    def __init__(self, store: JobStore, pipeline: Pipeline):
        self.store, self.pipeline = store, pipeline
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="clipcrow-job")
        self._edit_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="clipcrow-edit")      # one clip re-render does not wait behind a 30-minute job
        self.on_finish = None                                      # called with the job once it is done, failed or cancelled
        self.on_change = None                                      # called with a job id when its files changed (to mirror them)
        self.brands = None                                         # BrandStore: the logo and ending video of an account
        self.storage = None                                        # object storage, for jobs whose upload went straight to it
        self._cond = threading.Condition()
        self._active = 0                                           # tasks (jobs and clip re-renders) running right now
        self._draining = False
        self._requeue: set[str] = set()                            # jobs stopped by a drain: run again after the restart
        self._cancel: dict[str, threading.Event] = {}
        self._edit_lock = threading.Lock()                       # one edit at a time touches edl.json
        self._replan: dict[tuple[str, str], str] = {}            # (job, clip) -> multi mode to re-plan framing with
        self._gen: dict[tuple[str, str], int] = {}               # bumped by every edit; a render only finishes the latest

    def recover(self) -> None:
        """After a restart: queued jobs go back in the queue. A job a restart interrupted is queued AGAIN once (a long
        transcription cannot always be stopped in time for a deploy, and losing it would waste the person's wait); a job that
        was already interrupted once is marked failed, so one that keeps taking the server down cannot loop for ever."""
        for job in reversed(self.store.list()):
            if job.status == "queued":
                self.submit(job.id)
            elif job.status == "running" and job.cancel_requested:
                job.status = "cancelled"
                job.add_log("cancelled")
                for s in job.stages:
                    if s.state == "running":
                        s.state, s.detail = "pending", ""
                self.store.save(job)
                self._finished(job)
            elif job.status == "running":
                has_video = self.store.source_path(job.id) is not None or bool(job.source_pending)
                if job.attempts < 1 and has_video:
                    job.attempts += 1
                    job.status = "queued"
                    for s in job.stages:
                        s.state, s.detail = "pending", ""
                    job.add_log("the server restarted while this job was running; it starts again by itself")
                    self.store.save(job)
                    self.submit(job.id)
                    continue
                job.status, job.error = "failed", "The server restarted while this job was running."
                for s in job.stages:
                    if s.state == "running":                      # the step it was on stopped there, not still working
                        s.state, s.detail = "failed", "interrupted"
                self.store.save(job)
                self._finished(job)
            elif any(c.render in ("queued", "rendering") for c in job.clips):
                for c in job.clips:
                    if c.render in ("queued", "rendering"):
                        c.render, c.render_error = "failed", "The server restarted while this clip was re-rendering. Render it again."
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
        if job.status == "running":                              # remembered on disk: a deploy or a crash must not start a cancelled job again
            self.store.update(job_id, lambda j: setattr(j, "cancel_requested", True))
        if job.status == "queued":
            job.status = "cancelled"
            self.store.save(job)
            self._finished(job)
        return True

    def _fetch_source(self, job: Job, say, hooks) -> None:
        from .storage import signature
        if self.storage is None:
            raise ValueError("this job's video is in object storage, which is not configured on this server")
        rel = job.source_pending
        dest = self.store.dir(job.id) / rel
        say("fetching your upload from storage")
        hooks.check()
        self.storage.get_file(job.id, rel, dest)               # StorageError is an EXPECTED error: its message is shown
        sig = signature(dest)                                  # identical to the bucket copy, so the mirror does not upload it again
        job.source_pending = None
        job.remote[rel] = sig
        self.store.save(job)

    def _changed(self, job_id: str) -> None:
        if self.on_change:
            try:
                self.on_change(job_id)
            except Exception:
                log.exception("on_change failed for %s", job_id)

    def _finished(self, job: Job) -> None:
        if job.status == "done":
            self._changed(job.id)
        if self.on_finish:
            try:
                self.on_finish(job)
            except Exception:
                log.exception("on_finish failed for %s", job.id)

    def queue_position(self, job_id: str) -> int:
        queued = [j for j in reversed(self.store.list()) if j.status == "queued"]
        return next((i + 1 for i, j in enumerate(queued) if j.id == job_id), 0)

    # --- shutdown --------------------------------------------------------------------------------

    def drain(self, timeout: float, stop_grace: float = 10.0) -> None:
        """For a deploy or a stop: start nothing new, give what is running ``timeout`` seconds, then ask the running
        job to stop and put it back in the queue (its reserved minutes stay reserved, it is never marked failed or
        cancelled): the next start picks it up again. Waiting jobs stay queued for the same reason."""
        with self._cond:
            self._draining = True
            self._cond.wait_for(lambda: self._active == 0, timeout)
            running = [j for j in self.store.list() if j.status == "running"]
            for j in running:
                if j.cancel_requested:                           # the person already said stop: let it end as cancelled, do not put it back
                    ev = self._cancel.get(j.id)
                    if ev:
                        ev.set()
                    continue
                self._requeue.add(j.id)
                ev = self._cancel.get(j.id)
                if ev:
                    ev.set()
            if running:
                self._cond.wait_for(lambda: self._active == 0, stop_grace)

    def _enter(self) -> bool:
        with self._cond:
            if self._draining:
                return False
            self._active += 1
            return True

    def _leave(self) -> None:
        with self._cond:
            self._active -= 1
            self._cond.notify_all()

    def _run(self, job_id: str) -> None:
        if not self._enter():
            return                                              # draining: it stays queued and runs after the restart
        try:
            self._run_job(job_id)
        finally:
            self._leave()

    def _run_job(self, job_id: str) -> None:
        store = self.store
        job = store.get(job_id)
        if job.status != "queued":
            return                                              # cancelled while waiting
        ev = self._cancel.setdefault(job_id, threading.Event())
        job.status = "running"
        store.save(job)
        ready: set[str] = set()

        def stage(name: str, state: str, detail: str = "") -> None:
            job.cancel_requested = job.cancel_requested or ev.is_set()      # this thread saves its own copy of the job: keep the flag
            s = job.stage(name)
            if state == "running" and s.state != "running":
                s.started, s.seconds = time.time(), None
            elif state in ("done", "failed", "skipped") and s.started is not None and s.seconds is None:
                s.seconds = round(time.time() - s.started, 1)
            s.state, s.detail = state, detail
            store.save(job)

        def say(msg: str) -> None:
            job.cancel_requested = job.cancel_requested or ev.is_set()
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

        def clip_notes(clip_id: str, notes: list) -> None:
            for c in job.clips:
                if c.id == clip_id:
                    c.notes = notes[:5]
            store.save(job)

        hooks = Hooks(stage=stage, clip_ready=clip_ready, planned=planned, clip_notes=clip_notes, cancelled=ev.is_set)
        p = job.params
        from ..branding import BrandingChoice, BrandKit
        try:
            kit = self.brands.kit(job.owner or "shared", p.brand) if self.brands else BrandKit()
        except KeyError:                                           # the brand was deleted after the job was made: its caption look is gone, the rest was copied into the job
            kit = BrandKit()
        choice = BrandingChoice(cover=p.cover, logo=p.logo, banner=p.banner, outro=p.outro, music=p.music, captions=bool(kit.captions or kit.caption_font_latin) and p.style is None)
        if choice.any():
            hooks.choice, hooks.brand = choice, kit
        try:
            if job.source_pending:                              # a direct upload: the original is in the bucket, fetch it first
                self._fetch_source(job, say, hooks)
            src = store.source_path(job_id)
            if src is None:
                raise ValueError("the uploaded video is missing")
            if not job.source_sha:                              # the bytes' fingerprint: the same file uploaded again reuses its transcript (asr_cache)
                from .asr_cache import file_sha256
                job.source_sha = file_sha256(src)
                store.save(job)
            self.pipeline.run(job, src, store.dir(job_id) / "out", hooks, say)
            work = store.working_video(job_id)
            if work is not None and work != src:                 # the cleanup cut a new video: the editor's limits are ITS length
                try:
                    from ..planner.source import probe_source
                    info = probe_source(work)
                    job.source = {"duration": info.duration, "width": info.width, "height": info.height}
                except Exception:                                # a probe problem must not fail a finished job
                    log.exception("could not probe the cleaned video of %s", job_id)
            job.status = "done"
        except Cancelled:
            if job_id in self._requeue and not job.cancel_requested:       # stopped by a deploy, not by a person
                job.status = "queued"
                for s in job.stages:
                    s.state, s.detail = "pending", ""
                job.add_log("the server was restarted; this job starts again by itself")
            else:
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
        self._finished(job)

    # --- editing a finished clip ------------------------------------------------------------------

    def _original(self, job_id: str) -> Path:
        return self.store.dir(job_id) / "out" / "edl.original.json"

    def _write_edl(self, job_id: str, edl: EDL) -> None:
        path = self.store.edl_path(job_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(edl.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
        os.replace(tmp, path)

    def _mark(self, job_id: str, clip_id: str, edl: EDL | None = None, **fields) -> None:
        info = {c.id: c for c in clip_infos(edl)} if edl is not None else {}

        def fn(job: Job) -> None:
            for c in job.clips:
                if c.id == clip_id:
                    if clip_id in info:
                        n = info[clip_id]
                        c.title, c.hook, c.start, c.end, c.framing = n.title, n.hook, n.start, n.end, n.framing
                    for k, v in fields.items():
                        setattr(c, k, v)
        self.store.update(job_id, fn)

    def edit_clip(self, job_id: str, clip_id: str, edit) -> bool:
        """Apply ``edit`` to the clip's plan and queue a re-render. Raises ValueError (a message for a person)
        when the change is not allowed. Returns False when the edit changed nothing."""
        from .editor import apply_edit
        job = self.store.get(job_id)
        if job.status != "done":
            raise ValueError("Wait for the job to finish before editing its clips.")
        with self._edit_lock:
            edl = load_edl(self.store.edl_path(job_id))
            seat_cx = None
            if getattr(edit, "follow", None) is not None:
                from ..seats import load_map, seat_centres
                sm = load_map(self.store.dir(job_id) / "out")
                if sm is not None:
                    seat_cx = seat_centres(sm, float(job.source.get("width") or 16), float(job.source.get("height") or 9))
            new, replan = apply_edit(edl, clip_id, edit, source_duration=float(job.source.get("duration", 0)), seat_cx=seat_cx)
            if new is edl:
                return False
            if not self._original(job_id).exists():
                shutil.copyfile(self.store.edl_path(job_id), self._original(job_id))
            self._write_edl(job_id, new)
            key = (job_id, clip_id)
            self._gen[key] = self._gen.get(key, 0) + 1
            if replan:
                self._replan[key] = replan
            self._mark(job_id, clip_id, new, render="queued", render_error="", edited=True)
        self._changed(job_id)
        self._edit_pool.submit(self._render_clip, job_id, clip_id)
        return True

    def set_speakers(self, job_id: str, overrides: dict[int, int | None]) -> list[str]:
        """Say which seat each voice belongs to (a person's correction wins over the matching) and plan the framing of every clip that followed the
        voices again. Raises ValueError (a message for a person). Returns the ids of the clips that render again."""
        from ..seats import load_map, save_map
        job = self.store.get(job_id)
        if job.status != "done":
            raise ValueError("Wait for the job to finish before changing who is who.")
        out = self.store.dir(job_id) / "out"
        sm = load_map(out, usable_only=False)
        if sm is None or not sm.seats:
            raise ValueError("This video has no people to choose between.")
        voices = {t.voice for t in sm.turns}
        for v, s in overrides.items():
            if v not in voices:
                raise ValueError("Unknown voice.")
            if s is not None and not 0 <= s < len(sm.seats):
                raise ValueError("Unknown person.")
        changed = [v for v, s in overrides.items() if sm.seat_of(v) != s]
        if changed and any(c.render in ("queued", "rendering") for c in job.clips):
            raise ValueError("A clip is rendering. Try again when it has finished.")
        if not changed:
            return []
        with self._edit_lock:
            sm.overrides = {**sm.overrides, **overrides}
            save_map(out, sm)
        return self._frame_again(job_id)

    def _frame_again(self, job_id: str) -> list[str]:
        """Plan the framing of every clip that followed faces or voices afresh (with the saved speaker map) and render it again."""
        queued: list[str] = []
        with self._edit_lock:
            edl = load_edl(self.store.edl_path(job_id))
            if not self._original(job_id).exists():
                shutil.copyfile(self.store.edl_path(job_id), self._original(job_id))
            clips = []
            for c in edl.clips:
                if c.layout.framing in ("track", "split"):
                    c = c.model_copy(update={"layout": c.layout.model_copy(update={"framing": "cover", "reframe": [], "split": [], "fit_spans": []})})
                    queued.append(c.id)
                clips.append(c)
            self._write_edl(job_id, edl.model_copy(update={"clips": clips}))
            for cid in queued:
                key = (job_id, cid)
                self._gen[key] = self._gen.get(key, 0) + 1
                self._replan[key] = "switch"
                self._mark(job_id, cid, None, render="queued", render_error="", edited=True)
        for cid in queued:
            self._edit_pool.submit(self._render_clip, job_id, cid)
        self._changed(job_id)
        return queued

    def build_speakers(self, job_id: str) -> bool:
        """Find who is on screen and which voice is whose for a job that was made without it, then frame its clips again. Runs in the background;
        ``out/speakers.building`` says so. Returns False when it is already running."""
        job = self.store.get(job_id)
        if job.status != "done":
            raise ValueError("Wait for the job to finish first.")
        video = self.store.working_video(job_id)
        if video is None or job.source_removed:
            raise ValueError("The recording has been removed, so people cannot be told apart in it.")
        if any(c.render in ("queued", "rendering") for c in job.clips):
            raise ValueError("A clip is rendering. Try again when it has finished.")
        flag = self.store.dir(job_id) / "out" / "speakers.building"
        if flag.exists() and time.time() - flag.stat().st_mtime < 3600:
            return False
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text("1", encoding="utf-8")
        self._pool.submit(self._build_speakers, job_id, video, flag)
        return True

    def _build_speakers(self, job_id: str, video, flag) -> None:
        from ..seats import build_speaker_map

        def say(msg: str) -> None:
            self.store.update(job_id, lambda j: j.add_log(msg))
        try:
            (flag.parent / "speakers.json").unlink(missing_ok=True)
            hub = getattr(self.pipeline, "hub", None)
            if hub is not None:
                from .workers import RemoteSpeakers
                build_speaker_map = RemoteSpeakers(hub_view(hub, self.store.get(job_id)), job_id, check=lambda: None)          # noqa: F811 (a worker with a GPU first, else here)
            sm = build_speaker_map(video, flag.parent, say=say)
            if sm is None:
                say("no people could be told apart in this video; the clips stay as they are")
            else:
                n = len(self._frame_again(job_id))
                say(f"{n} clip(s) are being framed again to follow whoever speaks")
        except Exception as e:                                                     # never show internals; the log says what happened
            log.exception("speakers of %s failed", job_id)
            say(f"telling the people apart failed ({type(e).__name__}); the clips stay as they are")
        finally:
            flag.unlink(missing_ok=True)

    def edit_transcript(self, job_id: str, edit) -> list[str]:
        """Correct words of the plan's transcript and queue a re-render of the clips that show them. Raises ValueError (a message for a person).
        Returns the ids of those clips (empty when nothing changed)."""
        from .editor import apply_transcript
        if self.store.get(job_id).status != "done":
            raise ValueError("Wait for the job to finish before editing its transcript.")
        with self._edit_lock:
            edl = load_edl(self.store.edl_path(job_id))
            new, hit = apply_transcript(edl, edit)
            if new is edl:
                return []
            if not self._original(job_id).exists():
                shutil.copyfile(self.store.edl_path(job_id), self._original(job_id))
            self._write_edl(job_id, new)
            tj = self.store.dir(job_id) / "out" / "transcript.json"
            if tj.is_file():                                           # what a later look at the job shows matches what the captions say
                try:
                    tj.write_text(json.dumps({"language": new.language.primary, "words": [w.model_dump(exclude_none=True) for w in new.transcript]},
                                             ensure_ascii=False, indent=1), encoding="utf-8")
                except OSError:
                    pass
            for cid in hit:
                key = (job_id, cid)
                self._gen[key] = self._gen.get(key, 0) + 1
                self._mark(job_id, cid, new, render="queued", render_error="", edited=True)
        self._changed(job_id)
        for cid in hit:
            self._edit_pool.submit(self._render_clip, job_id, cid)
        return hit

    def reset_clip(self, job_id: str, clip_id: str) -> bool:
        """Put one clip back to the AI's plan and re-render it."""
        if self.store.get(job_id).status != "done":
            raise ValueError("Wait for the job to finish before editing its clips.")
        with self._edit_lock:
            if not self._original(job_id).exists():
                return False
            orig = next((c for c in load_edl(self._original(job_id)).clips if c.id == clip_id), None)
            if orig is None:
                return False
            edl = load_edl(self.store.edl_path(job_id))
            new = edl.model_copy(update={"clips": [orig if c.id == clip_id else c for c in edl.clips]})
            self._write_edl(job_id, new)
            key = (job_id, clip_id)
            self._gen[key] = self._gen.get(key, 0) + 1
            self._replan.pop(key, None)
            self._mark(job_id, clip_id, new, render="queued", render_error="", edited=False)
        self._changed(job_id)
        self._edit_pool.submit(self._render_clip, job_id, clip_id)
        return True

    def rerender(self, job_id: str, clip_id: str) -> None:
        """Render a clip again as it is (after a failed render)."""
        with self._edit_lock:
            key = (job_id, clip_id)
            self._gen[key] = self._gen.get(key, 0) + 1
            self._mark(job_id, clip_id, render="queued", render_error="")
        self._edit_pool.submit(self._render_clip, job_id, clip_id)

    def _render_clip(self, job_id: str, clip_id: str) -> None:
        if not self._enter():
            self._mark(job_id, clip_id, render="failed", render_error="The server was restarted before this clip could be rendered. Render it again.")
            return
        try:
            self._render_clip_now(job_id, clip_id)
        finally:
            self._leave()

    def _render_clip_now(self, job_id: str, clip_id: str) -> None:
        key = (job_id, clip_id)
        gen = self._gen.get(key, 0)
        try:
            self._mark(job_id, clip_id, render="rendering")
            job = self.store.get(job_id)
            video = self.store.working_video(job_id)
            if video is None:
                raise ValueError("the uploaded video is missing")
            replan = self._replan.pop(key, None)
            if replan:                                           # plan the framing afresh (slow: outside the lock)
                from ..framing import apply_framing
                with self._edit_lock:
                    edl = load_edl(self.store.edl_path(job_id))
                sub = edl.model_copy(update={"clips": [c for c in edl.clips if c.id == clip_id]})
                from ..seats import load_map
                planned = apply_framing(sub, str(video), multi=replan, graphics=job.params.graphics, speakers=load_map(self.store.dir(job_id) / "out")).clips[0]
                with self._edit_lock:                            # merge into the CURRENT plan: another edit may have landed
                    edl = load_edl(self.store.edl_path(job_id))
                    clips = []
                    for c in edl.clips:
                        if c.id == clip_id:
                            upd = {"layout": planned.layout}
                            if c.logo is not None:                                      # the face moved, so the logo's place is worked out again
                                from ..branding import fit_logo, logo_aspect
                                brand_logo = self.store.dir(job_id) / "out" / "brand"
                                found = next(iter(sorted(brand_logo.glob("logo.*"))), None) if brand_logo.is_dir() else None
                                upd["logo"] = fit_logo(c.logo, planned.layout.face_zone, c.captions.top_px if c.captions.enabled else None,
                                                       logo_aspect(found) if found else 1.0, edl.render.width, edl.render.height,
                                                       c.banner.top_px if c.banner and c.banner.enabled else None)
                            if planned.layout.framing == "split":
                                upd["captions"] = c.captions.model_copy(update={"top_px": planned.captions.top_px})
                            c = c.model_copy(update=upd)
                        clips.append(c)
                    self._write_edl(job_id, edl.model_copy(update={"clips": clips}))
            with self._edit_lock:
                edl = load_edl(self.store.edl_path(job_id))
            out = self.store.clip_path(job_id, clip_id)
            warnings = self.pipeline.render_clip(job, edl, clip_id, out, self.store.dir(job_id) / "out" / "work", video.parent)
            if self._gen.get(key, 0) == gen:                     # nobody edited again while we rendered
                self._mark(job_id, clip_id, edl, render="ready", render_error="", ready=True, notes=list(warnings or []),
                           rev=self.store.get(job_id).clip(clip_id).rev + 1)
                self._changed(job_id)
        except EXPECTED as e:
            self._mark(job_id, clip_id, render="failed", render_error=str(e)[:300])
        except Exception as e:
            log.exception("re-render of %s/%s failed", job_id, clip_id)
            self._mark(job_id, clip_id, render="failed", render_error=f"Something went wrong on the server ({type(e).__name__}).")
