"""Job records and their on-disk store.

    <data>/jobs/<id>/job.json     the record (this module)
                    source.<ext>  the uploaded video
                    out/          transcript.json, edl.json, plan-report.json, cN.mp4, cN.mp4.ai.json

Ids are 12 hex characters made here, so a request can never name a path outside a job's directory.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..styles import StyleSettings

JOB_ID = re.compile(r"^[a-f0-9]{12}\Z")          # \Z, not $: "$" also matches before a trailing newline
CLIP_ID = re.compile(r"^c\d{1,3}\Z")
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
STAGE_NAMES = ("transcribe", "plan", "assets", "frame", "render")
LOG_LIMIT = 400


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Params(_M):
    brand: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{12}$")     # which of the owner's brands this job uses (fixed when it is made, so a later change of default never moves it)
    language: Optional[str] = Field(default=None, pattern=r"^[a-z]{2,3}$")
    clips: Optional[int] = Field(default=None, ge=1, le=20)
    min_clip: int = Field(default=30, ge=5, le=900)
    max_clip: int = Field(default=120, ge=10, le=900)
    whole: bool = False                     # the whole (cleaned) recording is ONE clip: no moment picking, one title and hook for all of it
    spread: float = Field(default=0.5, ge=0, le=1)
    speaker_name: Optional[str] = Field(default=None, max_length=60)
    speaker_title: str = Field(default="", max_length=60)
    cutout: bool = False
    immersive: bool = False
    explainer: bool = False                 # pictures over the footage (screenshots of the links, AI images)
    links: list[str] = Field(default_factory=list, max_length=6)
    ai_images: bool = False
    pictures: list[str] = Field(default_factory=list, max_length=6)      # ids of the person's own pictures (copied into the job); the model places them
    multi: Literal["switch", "split", "off"] = "switch"
    graphics: bool = False                  # the source cuts to slides, cards or b-roll: show those whole instead of cropping them
    clean: bool = False                     # cut fumbles, fillers, retakes and dead air out of a raw recording first
    clean_pace: Literal["natural", "fast", "tight"] = "natural"
    gpu: Literal["auto", "home", "pod"] = "auto"     # which GPU worker may do this job: any available, only the home one, or only the on-demand pod
    fresh_transcript: bool = False          # listen to the recording again even when an earlier job of this account already transcribed the same file
    clean_take: Literal["last", "best"] = "last"     # which take of a repeated passage stays: the last one, or the one the AI judges best
    cover: bool = False                     # a bold title for the first ~250 ms (and a cover PNG)
    logo: bool = False                      # the account's logo in a corner
    overlay: bool = False                   # the brand's PNG overlay on the video
    banner: bool = False                    # the brand's "watch the full video" banner
    outro: bool = False                     # the account's ending video after each clip
    transition: Literal["none", "flash", "blur", "dip", "whip"] = "none"     # a short effect on every hard cut (a jump cut, a speaker change)
    music: bool = False                     # the account's background track under the speech, ducked while someone talks
    sounds: bool = False                    # quiet sound cues under the speech where a title, card, picture or push-in appears
    style: Optional[StyleSettings] = None   # a COPY of a saved style's settings (editing the style later changes nothing here)
    style_name: str = Field(default="", max_length=60)


class Stage(_M):
    name: str
    state: Literal["pending", "running", "done", "skipped", "failed"] = "pending"
    detail: str = ""
    started: Optional[float] = None         # when it began running (epoch seconds)
    seconds: Optional[float] = None         # how long it took, once it has finished


class ClipInfo(_M):
    id: str
    title: str
    hook: str = ""
    start: float
    end: float
    score: Optional[float] = None
    why: str = ""
    ai_decided: list[str] = Field(default_factory=list)
    framing: str = "cover"
    ready: bool = False
    # editing: a ready clip can be re-rendered after an edit; the old video stays playable meanwhile
    render: Literal["ready", "queued", "rendering", "failed"] = "ready"
    render_error: str = ""
    rev: int = 0                 # bumps after each re-render so the browser reloads the video
    edited: bool = False
    has_cover: bool = False
    notes: list[str] = Field(default_factory=list)      # e.g. text behind the speaker skipped: no matte


class LeftOut(_M):
    start: float
    end: float
    reason: str


class Job(_M):
    id: str
    name: str
    created_at: str
    status: Literal["queued", "running", "done", "failed", "cancelled"] = "queued"
    stages: list[Stage] = Field(default_factory=lambda: [Stage(name=n) for n in STAGE_NAMES])
    log: list[str] = Field(default_factory=list)
    error: Optional[str] = None
    params: Params = Field(default_factory=Params)
    source: dict = Field(default_factory=dict)          # duration, width, height
    clips: list[ClipInfo] = Field(default_factory=list)
    left_out: list[LeftOut] = Field(default_factory=list)   # clips the planner skipped, and why
    demo: bool = False
    owner: Optional[str] = None             # the user id when accounts are on; None in single-user mode
    reserved_minutes: float = 0             # source minutes taken from the owner's allowance for this job
    source_removed: bool = False            # the uploaded original was deleted by retention
    source_sha: Optional[str] = None        # SHA-256 of the uploaded original (set when the job starts): an earlier transcript of the same bytes can be reused
    source_pending: Optional[str] = None    # the original is in the bucket (a direct upload) and has not been fetched yet
    cancel_requested: bool = False          # a person asked to stop while it was running: a restart must not start it again
    attempts: int = 0                       # times a restart interrupted this job and it was queued again (one automatic retry)
    remote: dict[str, str] = Field(default_factory=dict)   # relative name -> signature of the copy in object storage
    keep: bool = False                      # kept for ever by the operator (old jobs given to an account): retention never deletes it
    sample: bool = False                    # the sample a new account starts with: costs no minutes, is not expired by retention, cannot be re-cut

    def clip(self, clip_id: str) -> ClipInfo:
        return next(c for c in self.clips if c.id == clip_id)

    def stage(self, name: str) -> Stage:
        return next(s for s in self.stages if s.name == name)

    def add_log(self, line: str) -> None:
        self.log.append(line[:300])
        del self.log[:-LOG_LIMIT]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")   # ms: queue order and listing need a total order


@dataclass(frozen=True)
class Brief:
    id: str
    owner: Optional[str]
    status: str
    created_at: str


class JobStore:
    def __init__(self, root: str | Path):
        self.root = Path(root) / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._briefs: dict[str, tuple] = {}

    def dir(self, job_id: str) -> Path:
        if not JOB_ID.match(job_id):
            raise KeyError(job_id)
        return self.root / job_id

    def create(self, name: str, params: Params, *, demo: bool = False, job_id: Optional[str] = None) -> Job:
        if job_id is not None and not JOB_ID.match(job_id):
            raise ValueError("bad job id")
        job = Job(id=job_id or uuid.uuid4().hex[:12], name=name[:200], created_at=now(), params=params, demo=demo)
        (self.root / job.id / "out").mkdir(parents=True)
        self.save(job)
        return job

    def save(self, job: Job) -> None:
        path = self.dir(job.id) / "job.json"
        tmp = path.with_suffix(".tmp")
        with self._lock:
            tmp.write_text(job.model_dump_json(indent=1), encoding="utf-8")
            os.replace(tmp, path)
            self._briefs.pop(job.id, None)                  # a rewrite inside one clock tick with the same size must not look unchanged

    def get(self, job_id: str) -> Job:
        try:
            return Job.model_validate(json.loads((self.dir(job_id) / "job.json").read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError) as e:
            raise KeyError(job_id) from e

    def update(self, job_id: str, fn) -> Job:
        """Load, change and save a job as one step (so two threads never overwrite each other's change)."""
        with self._lock:
            job = self.get(job_id)
            fn(job)
            self.save(job)
            return job

    # posts to social channels: <job>/out/posts.json, a list of {id, clip, channel, name, platform, status, detail, when, at}
    def posts(self, job_id: str, clip_id: str | None = None) -> list[dict]:
        try:
            rows = json.loads((self.dir(job_id) / "out" / "posts.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rows = []
        return [r for r in rows if clip_id is None or r.get("clip") == clip_id]

    def add_posts(self, job_id: str, rows: list[dict]) -> None:
        with self._lock:
            path = self.dir(job_id) / "out" / "posts.json"
            path.write_text(json.dumps(self.posts(job_id) + rows, ensure_ascii=False), encoding="utf-8")

    def update_post(self, job_id: str, post_id: str, status: str, detail: str = "") -> None:
        with self._lock:
            rows = self.posts(job_id)
            for r in rows:
                if r["id"] == post_id:
                    r["status"], r["detail"] = status, detail[:300]
            (self.dir(job_id) / "out" / "posts.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")

    # GPU time of a job: <job>/out/gpu_usage.json, a list of {kind, worker, seconds, ok, rate (INR an hour), at}
    def gpu_usage(self, job_id: str) -> list[dict]:
        try:
            rows = json.loads((self.dir(job_id) / "out" / "gpu_usage.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return rows if isinstance(rows, list) else []

    def add_gpu_usage(self, job_id: str, row: dict) -> None:
        with self._lock:
            path = self.dir(job_id) / "out" / "gpu_usage.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.gpu_usage(job_id) + [row], ensure_ascii=False), encoding="utf-8")

    def edl_path(self, job_id: str) -> Path:
        return self.dir(job_id) / "out" / "edl.json"

    def list(self) -> list[Job]:
        jobs = []
        for d in self.root.iterdir():
            try:
                jobs.append(self.get(d.name))
            except KeyError:
                continue
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    def briefs(self) -> list["Brief"]:
        """Owner, status and date of every job, newest first: for the checks that run on every request (limits, queue position). A job file is read again only when it
        changed (size or modification time), so with thousands of jobs this costs one ``stat`` each instead of parsing every file."""
        out, seen = [], set()
        with self._lock:
            for d in self.root.iterdir():
                path = d / "job.json"
                try:
                    st = path.stat()
                except OSError:
                    continue
                seen.add(d.name)
                key = (st.st_mtime_ns, st.st_size)
                hit = self._briefs.get(d.name)
                if hit is None or hit[0] != key:
                    try:
                        j = Job.model_validate(json.loads(path.read_text(encoding="utf-8")))
                    except (OSError, ValueError):
                        continue
                    hit = (key, Brief(j.id, j.owner, j.status, j.created_at))
                    self._briefs[d.name] = hit
                out.append(hit[1])
            for gone in [k for k in self._briefs if k not in seen]:
                del self._briefs[gone]
        return sorted(out, key=lambda b: b.created_at, reverse=True)

    def delete(self, job_id: str) -> None:
        shutil.rmtree(self.dir(job_id), ignore_errors=True)

    def source_path(self, job_id: str) -> Optional[Path]:
        return next(iter(sorted(self.dir(job_id).glob("source.*"))), None)

    def working_video(self, job_id: str) -> Optional[Path]:
        """The video the clips were planned on: the cleaned cut (``out/clean.mp4``) when the cleanup ran, else the upload. Re-renders and edits use this one,
        because every time in the plan is on ITS timeline."""
        cleaned = self.dir(job_id) / "out" / "clean.mp4"
        return cleaned if cleaned.is_file() else self.source_path(job_id)

    def cover_path(self, job_id: str, clip_id: str) -> Path:
        if not CLIP_ID.match(clip_id):
            raise KeyError(clip_id)
        return self.dir(job_id) / "out" / f"{clip_id}.cover.png"

    def clip_path(self, job_id: str, clip_id: str) -> Path:
        if not CLIP_ID.match(clip_id):
            raise KeyError(clip_id)
        return self.dir(job_id) / "out" / f"{clip_id}.mp4"
