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
    language: Optional[str] = Field(default=None, pattern=r"^[a-z]{2,3}$")
    clips: Optional[int] = Field(default=None, ge=1, le=20)
    min_clip: int = Field(default=15, ge=5, le=120)
    max_clip: int = Field(default=60, ge=10, le=180)
    spread: float = Field(default=0.5, ge=0, le=1)
    speaker_name: Optional[str] = Field(default=None, max_length=60)
    speaker_title: str = Field(default="", max_length=60)
    cutout: bool = False
    immersive: bool = False
    explainer: bool = False                 # pictures over the footage (screenshots of the links, AI images)
    links: list[str] = Field(default_factory=list, max_length=6)
    ai_images: bool = False
    multi: Literal["switch", "split", "off"] = "switch"
    cover: bool = False                     # a bold title for the first ~250 ms (and a cover PNG)
    logo: bool = False                      # the account's logo in a corner
    outro: bool = False                     # the account's ending video after each clip
    style: Optional[StyleSettings] = None   # a COPY of a saved style's settings (editing the style later changes nothing here)
    style_name: str = Field(default="", max_length=60)


class Stage(_M):
    name: str
    state: Literal["pending", "running", "done", "skipped", "failed"] = "pending"
    detail: str = ""


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
    source_pending: Optional[str] = None    # the original is in the bucket (a direct upload) and has not been fetched yet
    remote: dict[str, str] = Field(default_factory=dict)   # relative name -> signature of the copy in object storage

    def clip(self, clip_id: str) -> ClipInfo:
        return next(c for c in self.clips if c.id == clip_id)

    def stage(self, name: str) -> Stage:
        return next(s for s in self.stages if s.name == name)

    def add_log(self, line: str) -> None:
        self.log.append(line[:300])
        del self.log[:-LOG_LIMIT]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")   # ms: queue order and listing need a total order


class JobStore:
    def __init__(self, root: str | Path):
        self.root = Path(root) / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

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

    def delete(self, job_id: str) -> None:
        shutil.rmtree(self.dir(job_id), ignore_errors=True)

    def source_path(self, job_id: str) -> Optional[Path]:
        return next(iter(sorted(self.dir(job_id).glob("source.*"))), None)

    def cover_path(self, job_id: str, clip_id: str) -> Path:
        if not CLIP_ID.match(clip_id):
            raise KeyError(clip_id)
        return self.dir(job_id) / "out" / f"{clip_id}.cover.png"

    def clip_path(self, job_id: str, clip_id: str) -> Path:
        if not CLIP_ID.match(clip_id):
            raise KeyError(clip_id)
        return self.dir(job_id) / "out" / f"{clip_id}.mp4"
