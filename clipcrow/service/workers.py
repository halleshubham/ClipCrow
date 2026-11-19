"""A remote worker for the slow steps: speech recognition and rendering a clip.

The worker is a program on a machine the operator owns (``clipcrow worker``). It PULLS: it asks this server for work over HTTPS, so
the machine opens no port and needs no tunnel. The server extracts the audio of a job and queues a task; a worker leases it,
downloads the audio, transcribes it and posts the words back.

Rules this module keeps:
  * Workers are authenticated by ONE shared secret (``CLIPCROW_WORKER_TOKEN``), compared in constant time. Without it the feature is
    off and the routes do not exist (404). It is a different secret from a person's login.
  * Everything a worker sends is untrusted: bounded by ``ResultIn``, then parsed like any other transcript. A worker that fails,
    stalls or vanishes can only cost time: the job falls back to the server's own recognition.
  * Only the files a task needs leave the server (the audio; or for a render the EDL, the source video and the clip's assets), only to a
    worker that holds a lease on that task, only while the lease lasts, and only by names the server put in the task's manifest.
  * What a worker returns for a render is untrusted media: it is probed (container, codec, size, duration) before it replaces
    anything, and the cover image and the provenance sidecar are made by the server, never taken from the worker.
  * A render task goes only to a worker whose HyperFrames pin equals the server's, so the pixels are the same wherever it ran.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from types import SimpleNamespace
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Callable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..planner.transcript import Transcript, TranscriptError, parse_transcript

TASK_ID = r"^[0-9a-f]{32}$"
WORKER_NAME = r"^[A-Za-z0-9._-]{1,40}$"
MAX_WORDS = 400_000
KINDS = ("transcribe", "render")
FILE_NAME = re.compile(r"^[A-Za-z0-9._@ /-]{1,200}$")          # what a manifest name may look like (and no "..", see _clean_name)
DIRECT_PART = 32 * 1024 * 1024                                  # parts of a clip sent straight to the bucket (S3/R2 need at least 5 MiB)
ETAG = re.compile(r'^"?[0-9a-fA-F]{32}"?\Z')
UPLOADS = ("clip.mp4",)                                          # the only file a worker may send back


class WorkerError(RuntimeError):
    """The worker could not do the task; the message is safe to show in a job's log."""


class WorkerUnavailable(WorkerError):
    """No worker took the task in time, or the one that did stopped answering."""


# --- what a worker may send --------------------------------------------------------------------------------------------

class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PollIn(_M):
    worker: str = Field(pattern=WORKER_NAME)
    wait: float = Field(default=20.0, ge=0.0, le=25.0)             # how long the server may hold the request open
    device: str = Field(default="", max_length=60)                 # e.g. "NVIDIA GeForce RTX 4060 (cuda)" or "cpu"
    model: str = Field(default="", max_length=40)
    can: list[Literal["transcribe", "render", "speakers", "framing"]] = Field(default_factory=lambda: ["transcribe"], max_length=4)
    pin: str = Field(default="", max_length=40)                    # the HyperFrames version this worker renders with
    direct: bool = False                                           # can send a finished clip straight to the bucket (see DirectIn)
    pool: Literal["home", "rented"] = "home"                       # which kind of machine this is: jobs can ask for one kind
    power: int = Field(default=0, ge=0, le=1000)                   # the worker's own estimate of its capacity; only orders who is offered a task first


class DirectIn(_M):
    worker: str = Field(pattern=WORKER_NAME)
    size: int = Field(ge=1024, le=64 * 1024 ** 3)


class PartDone(_M):
    n: int = Field(ge=1, le=10_000)
    etag: str = Field(pattern=r'^"?[0-9a-fA-F]{32}"?$', max_length=80)      # (pydantic's regex engine has no \Z, and its $ never matches before a newline)


class DirectDoneIn(_M):
    worker: str = Field(pattern=WORKER_NAME)
    parts: list[PartDone] = Field(max_length=10_000)


class HeartbeatIn(_M):
    worker: str = Field(pattern=WORKER_NAME)


class WordIn(_M):
    w: str = Field(max_length=100)
    s: float = Field(ge=0.0, le=86_400.0)
    e: float = Field(ge=0.0, le=86_400.0)


MAX_SEATS, MAX_VOICES_IN, MAX_TURNS = 12, 20, 4000


class SeatIn(_M):
    cx: float = Field(ge=0.0, le=1.0)
    cy: float = Field(ge=0.0, le=1.0)
    w: float = Field(gt=0.0, le=1.0)
    h: float = Field(gt=0.0, le=1.0)
    seen: float = Field(default=0.0, ge=0.0, le=1.0)
    live: float = Field(default=-1.0, ge=-1.0, le=1000.0)


class SpeakersIn(_M):
    """A worker's answer to "who is who in this video": untrusted, so every number is bounded and every index checked before it becomes a ``SpeakerMap``."""
    duration: float = Field(ge=0.0, le=36000.0)
    seats: list[SeatIn] = Field(default_factory=list, max_length=MAX_SEATS)
    turns: list[tuple[float, float, int]] = Field(default_factory=list, max_length=MAX_TURNS)
    voice_seat: dict[int, Optional[int]] = Field(default_factory=dict, max_length=MAX_VOICES_IN)
    lift: dict[int, float] = Field(default_factory=dict, max_length=MAX_VOICES_IN)
    picture_t: Optional[float] = Field(default=None, ge=0.0, le=36000.0)

    @model_validator(mode="after")
    def _consistent(self):
        for a, b, v in self.turns:
            if not (0.0 <= a < b <= self.duration + 1.0) or not 0 <= v < MAX_VOICES_IN:
                raise ValueError("a turn is outside the video or has a voice number out of range")
        if any(not 0 <= v < MAX_VOICES_IN or (s is not None and not 0 <= s < len(self.seats)) for v, s in self.voice_seat.items()):
            raise ValueError("a voice is matched to a seat that does not exist")
        if any(not 0 <= v < MAX_VOICES_IN or not 0.0 <= x <= 100.0 for v, x in self.lift.items()):
            raise ValueError("a lift is out of range")
        return self


MAX_FRAMING_CLIPS, MAX_KEYFRAMES = 60, 12000


class FramingOneIn(_M):
    """One clip's framing as a worker worked it out: untrusted, every number bounded (the same shape as ``reframe.Framing``)."""
    id: str = Field(pattern=r"^c\d{1,3}$")
    framing: Literal["cover", "track", "fit_blur", "split"]
    keyframes: list[tuple[float, float]] = Field(default_factory=list, max_length=MAX_KEYFRAMES)
    coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    split: list[tuple[float, float]] = Field(default_factory=list, max_length=4)
    people: int = Field(default=1, ge=1, le=8)
    fit_spans: list[tuple[float, float]] = Field(default_factory=list, max_length=200)
    face_zone: Optional[tuple[float, float, float, float]] = None
    seconds: float = Field(default=0.0, ge=0.0, le=86_400.0)

    @model_validator(mode="after")
    def _in_range(self):
        if any(not (0.0 <= t <= 3600.0 and 0.0 <= cx <= 1.0) for t, cx in self.keyframes):
            raise ValueError("a keyframe is outside the clip or the frame")
        if any(not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0) for x, y in self.split):
            raise ValueError("a split point is outside the frame")
        if any(not (0.0 <= a < b <= 3600.0) for a, b in self.fit_spans):
            raise ValueError("a graphic span is outside the clip")
        if self.face_zone is not None:
            x0, y0, x1, y1 = self.face_zone
            if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
                raise ValueError("the face zone is outside the frame")
        return self


class FramingIn(_M):
    """A worker's answer to "frame these clips"."""
    clips: list[FramingOneIn] = Field(default_factory=list, max_length=MAX_FRAMING_CLIPS)


def framing_from_in(o: FramingOneIn):
    from ..reframe import Framing
    return Framing(o.framing, [tuple(k) for k in o.keyframes], o.coverage, [tuple(p) for p in o.split], o.people,
                   [tuple(s) for s in o.fit_spans], tuple(o.face_zone) if o.face_zone else None)


class ResultIn(_M):
    worker: str = Field(pattern=WORKER_NAME)
    language: Optional[str] = Field(default=None, pattern=r"^[a-z]{2,3}$")
    words: list[WordIn] = Field(default_factory=list, max_length=MAX_WORDS)
    error: str = Field(default="", max_length=300)
    speakers: Optional[SpeakersIn] = None                                   # the answer to a "speakers" task
    framing: Optional[FramingIn] = None                                     # the answer to a "framing" task
    engine: str = Field(default="", pattern=r"^[a-z0-9_.-]{0,30}$")        # what recognised the speech (shown in the job log; part of the transcript reuse record)
    warnings: list[Annotated[str, Field(max_length=300)]] = Field(default_factory=list, max_length=20)     # a render's notes, each shown in the job log


def _clean_name(name: str) -> bool:
    return bool(FILE_NAME.fullmatch(name)) and ".." not in name.split("/") and not name.startswith("/") and "//" not in name     # fullmatch: "$" also matches before a trailing newline


# --- the hub -----------------------------------------------------------------------------------------------------------

@dataclass
class Task:
    id: str
    job_id: str
    kind: str                               # "transcribe" | "render"
    files: dict[str, Path]                  # manifest: the only names a worker may fetch, and where each lives on the server
    meta: dict                              # small, JSON: language / clip id and expected size
    created: float
    state: str = "pending"                  # pending | leased | done | failed | abandoned
    worker: str = ""
    lease_until: float = 0.0
    leased_at: float = 0.0
    max_seconds: float = 7200.0             # a worker that heartbeats for ever on one task is not trusted for ever
    result: Optional[dict] = None
    error: str = ""
    upload_dir: Optional[Path] = None
    uploads: dict[str, Path] = field(default_factory=dict)
    language: Optional[str] = None
    direct: bool = False                    # only a worker that can send the clip straight to the bucket may take it (a long clip)
    pool: str = ""                          # "" any worker | "home" only a machine the person owns | "rented" only the on-demand GPU
    plan: Optional[dict] = None             # the multipart upload this worker was given: {"id", "rel", "size", "count"} ({} while it is being made)
    direct_used: bool = False

    @property
    def audio(self) -> Optional[Path]:
        return self.files.get("audio")


@dataclass
class _Seen:
    at: float
    device: str = ""
    model: str = ""
    can: tuple = ("transcribe",)
    pin: str = ""
    direct: bool = False
    pool: str = "home"                      # "home" (a machine the person owns) or "rented" (the on-demand pod)
    power: int = 0                          # how much this worker can do (a GPU box > a laptop); the stronger idle worker gets a task first


class HubView:
    """The hub as one job sees it: every task it submits and every "is a worker out there" it asks is limited to the kind of machine the person chose
    for this job (``home``: only their own GPU; ``rented``: only the on-demand pod; ``""``: whichever is available)."""

    def __init__(self, hub: "WorkerHub", pool: str = ""):
        self._hub, self.pool = hub, pool if pool in ("home", "rented") else ""

    def __getattr__(self, name):
        return getattr(self._hub, name)

    def online(self, kind: Optional[str] = None, direct: bool = False) -> bool:
        return self._hub.online(kind, direct, self.pool)

    def submit(self, job_id, audio, language):
        return self._hub.submit(job_id, audio, language, pool=self.pool)

    def submit_speakers(self, job_id, files, meta, max_seconds: float = 1800.0):
        return self._hub.submit_speakers(job_id, files, meta, max_seconds, pool=self.pool)

    def submit_framing(self, job_id, files, meta, max_seconds: float = 3600.0):
        return self._hub.submit_framing(job_id, files, meta, max_seconds, pool=self.pool)

    def submit_render(self, job_id, files, meta, upload_dir, max_seconds: float = 3600.0, direct: bool = False):
        return self._hub.submit_render(job_id, files, meta, upload_dir, max_seconds, direct, pool=self.pool)


def _machine(name: str) -> str:
    """The machine a worker name belongs to: extra render slots are named ``<machine>.r2``, ``.r3`` ..."""
    return re.sub(r"\.r\d{1,2}\Z", "", name)


class WorkerHub:
    def __init__(self, token: str, *, pickup_seconds: float = 120.0, lease_seconds: float = 90.0,
                 online_seconds: float = 60.0, render_pin: str = "", max_upload_bytes: int = 300 * 1024 * 1024,
                 max_direct_bytes: int = 2 * 1024 ** 3, storage=None, clock: Callable[[], float] = time.monotonic,
                 on_task_end: Optional[Callable[[dict], None]] = None, rate_for: Optional[Callable[[str], float]] = None):
        if not token or len(token) < 16:
            raise ValueError("CLIPCROW_WORKER_TOKEN must be at least 16 characters (try: openssl rand -hex 32)")
        self._token = token.encode()
        self.pickup_seconds, self.lease_seconds, self.online_seconds = pickup_seconds, lease_seconds, online_seconds
        self.render_pin, self.max_upload_bytes = render_pin, max_upload_bytes
        self.max_direct_bytes, self.storage = max_direct_bytes, storage      # a finished clip may go to the bucket (storage) instead of through this server
        self._clock = clock
        self.on_task_end = on_task_end                  # called with {"job", "kind", "worker", "seconds", "ok", "rate"} when a worker finishes a task (the GPU time of a job)
        self.rate_for = rate_for or (lambda _name: 0.0)  # INR an hour of a rented worker; 0 for a machine the person owns
        self._cond = threading.Condition()
        self._tasks: dict[str, Task] = {}
        self._seen: dict[str, _Seen] = {}
        self.standby: Optional[Callable[[Optional[str], bool], bool]] = None   # set by the pod controller: "a paused GPU can be woken for this kind"
        self.why_unavailable: Optional[Callable[[], str]] = None               # set by the pod controller: why the last wake failed
        self._waiting: dict[str, int] = {}              # worker name -> how many of its polls are open right now (it is idle)

    YIELD_SECONDS = 4.0                                 # how long a weaker idle worker lets a stronger one have first pick

    # authentication ---------------------------------------------------------------------------------------------------
    def check_token(self, presented: str) -> bool:
        return hmac.compare_digest(presented.encode(), self._token)

    # who is out there ------------------------------------------------------------------------------------------------
    def _touch(self, name: str, device: str = "", model: str = "", can: Optional[tuple] = None, pin: Optional[str] = None,
               direct: Optional[bool] = None, power: Optional[int] = None, pool: Optional[str] = None) -> None:
        old = self._seen.get(name)
        self._seen[name] = _Seen(self._clock(), device or (old.device if old else ""), model or (old.model if old else ""),
                                 can if can is not None else (old.can if old else ("transcribe",)),
                                 pin if pin is not None else (old.pin if old else ""),
                                 direct if direct is not None else (old.direct if old else False),
                                 pool if pool is not None else (old.pool if old else "home"),
                                 power if power is not None else (old.power if old else 0))

    def _eligible(self, s: _Seen, kind: Optional[str], direct: bool = False, pool: str = "") -> bool:
        """Is this worker one that may take a ``kind`` task? A render needs the same HyperFrames pin as this server (and, for a task that
        will come back through the bucket, a worker that knows how)."""
        if pool and s.pool != pool:
            return False
        if kind is None:
            return True
        if kind not in s.can:
            return False
        if direct and not (s.direct and self.storage is not None):
            return False
        return kind != "render" or (bool(self.render_pin) and s.pin == self.render_pin)

    def online(self, kind: Optional[str] = None, direct: bool = False, pool: str = "") -> bool:
        """Is a worker that can do this out there, or one asleep that the pod controller can wake (``standby``)?"""
        with self._cond:
            return self._online(kind, direct, pool) or (pool != "home" and self._standby(kind, direct))

    def _standby(self, kind: Optional[str], direct: bool = False) -> bool:
        if self.standby is None:
            return False
        try:
            return bool(self.standby(kind, direct))
        except Exception:                                   # noqa: BLE001
            return False

    def unserved(self) -> list[dict]:
        """Tasks waiting for a worker that no online worker could take: what the pod controller wakes a paused machine for."""
        with self._cond:
            now = self._clock()
            return [{"kind": t.kind, "direct": t.direct, "pool": t.pool, "age": round(now - t.created, 1), "job": t.job_id}
                    for t in self._tasks.values() if t.state == "pending" and not self._online(t.kind, t.direct, t.pool)]

    def load(self) -> dict:
        """How busy the workers are: pending and leased task counts (the pod controller pauses a machine only when both are 0)."""
        with self._cond:
            states = [t.state for t in self._tasks.values()]
            return {"pending": states.count("pending"), "leased": states.count("leased")}

    def _online(self, kind: Optional[str], direct: bool = False, pool: str = "") -> bool:
        now = self._clock()
        return any(now - s.at <= self.online_seconds and self._eligible(s, kind, direct, pool) for s in self._seen.values())

    def describe(self) -> list[dict]:
        with self._cond:
            now = self._clock()
            return [{"name": n, "device": s.device, "model": s.model, "can": list(s.can), "pin": s.pin, "direct": s.direct,
                     "online": now - s.at <= self.online_seconds} for n, s in sorted(self._seen.items())]

    # the job side ----------------------------------------------------------------------------------------------------
    def _add(self, task: Task) -> Task:
        with self._cond:
            self._tasks[task.id] = task
            self._cond.notify_all()
        return task

    def submit(self, job_id: str, audio: Path, language: Optional[str], pool: str = "") -> Task:
        """A transcription task."""
        return self._add(Task(uuid.uuid4().hex, job_id, "transcribe", {"audio": audio}, {"language": language}, self._clock(),
                              language=language, pool=pool))

    def submit_speakers(self, job_id: str, files: dict[str, Path], meta: dict, max_seconds: float = 1800.0, pool: str = "") -> Task:
        """A "who is who in this video" task. ``files`` is the manifest (the video under ``media/<name>``, the name a render of this job uses, so it is cached for the renders)."""
        for name in files:
            if not _clean_name(name) or not name.startswith("media/"):
                raise WorkerError("a file in this task has a name the worker protocol does not allow")
        return self._add(Task(uuid.uuid4().hex, job_id, "speakers", dict(files), dict(meta), self._clock(), max_seconds=max_seconds, pool=pool))

    def submit_framing(self, job_id: str, files: dict[str, Path], meta: dict, max_seconds: float = 3600.0, pool: str = "") -> Task:
        """A "frame these clips" task: the video under ``media/<name>`` (the name a render of this job uses, so the worker keeps it for the renders)."""
        for name in files:
            if not _clean_name(name) or not name.startswith("media/"):
                raise WorkerError("a file in this task has a name the worker protocol does not allow")
        return self._add(Task(uuid.uuid4().hex, job_id, "framing", dict(files), dict(meta), self._clock(), max_seconds=max_seconds, pool=pool))

    def submit_render(self, job_id: str, files: dict[str, Path], meta: dict, upload_dir: Path, max_seconds: float = 3600.0,
                      direct: bool = False, pool: str = "") -> Task:
        """A render task. ``files`` maps manifest names to server paths; the names are checked here so a bad one never reaches a worker."""
        for name in files:
            if not _clean_name(name):
                raise WorkerError("a file in this render has a name the worker protocol does not allow")
        upload_dir.mkdir(parents=True, exist_ok=True)
        return self._add(Task(uuid.uuid4().hex, job_id, "render", dict(files), dict(meta), self._clock(),
                              max_seconds=max_seconds, upload_dir=upload_dir, direct=direct, pool=pool))

    def wait(self, task: Task, check: Callable[[], None], tick: float = 0.25, say: Callable[[str], None] = lambda _m: None) -> dict:
        """Block until the worker finishes the task. ``check`` raises when the job was cancelled. Raises ``WorkerUnavailable`` when
        nobody takes the task within ``pickup_seconds`` (or nobody eligible is online any more), the worker stops sending heartbeats,
        or it keeps one task longer than ``task.max_seconds``."""
        try:
            while True:
                check()
                with self._cond:
                    now = self._clock()
                    if task.state == "done":
                        return task.result or {}
                    if task.state == "failed":
                        raise WorkerError(task.error or "the worker could not do this")
                    if task.state == "pending":
                        self._reroute(task, now, say)
                        if now - task.created > self.pickup_seconds:
                            raise WorkerUnavailable("no GPU worker picked the task up in time")
                        if not self._online(task.kind, task.direct, task.pool) and not (task.pool != "home" and self._standby(task.kind, task.direct)):
                            raise WorkerUnavailable("the GPU worker went offline" + (f" ({self.why_unavailable()})" if self.why_unavailable and self.why_unavailable() else ""))
                    elif task.state == "leased":
                        if now > task.lease_until:
                            raise WorkerUnavailable("the GPU worker stopped answering")
                        if now - task.leased_at > task.max_seconds:
                            raise WorkerUnavailable("the GPU worker took too long")
                    self._cond.wait(tick)
        finally:
            with self._cond:
                if task.state in ("pending", "leased"):
                    task.state = "abandoned"                # a late result from the worker is refused
                self._tasks.pop(task.id, None)

    def _reroute(self, task: Task, now: float, say: Callable[[str], None]) -> None:
        """The kind of machine the job asked for cannot take the task (the on-demand pod will not wake, no home worker is connected): let any other
        connected worker have it before the job falls back to the server. Called with the lock held."""
        if not task.pool:
            return
        mine = self._online(task.kind, task.direct, task.pool)
        failing = task.pool == "rented" and bool(self.why_unavailable and self.why_unavailable())
        wakeable = self._standby(task.kind, task.direct)
        waking = task.pool != "home" and wakeable
        if mine or (waking and not failing):
            return
        if self._online(task.kind, task.direct, "") or (task.pool == "home" and wakeable):
            was = "the on-demand GPU" if task.pool == "rented" else "the home GPU"
            task.pool, task.created = "", now                  # a fresh pickup window on whichever worker is there
            say(f"{was} is not available, so any other connected GPU worker may take this")
            self._cond.notify_all()

    # the worker side -------------------------------------------------------------------------------------------------
    def poll(self, name: str, wait: float, device: str = "", model: str = "", can: tuple = ("transcribe",), pin: str = "",
             direct: bool = False, power: int = 0, pool: str = "home") -> Optional[Task]:
        """Hand the worker a pending task it can do (leased to it), holding the request open up to ``wait`` seconds. When a stronger worker that
        could take the task is also waiting, this one leaves it for a moment (``YIELD_SECONDS``) so the more capable machine gets it."""
        end = self._clock() + wait
        with self._cond:
            self._touch(name, device, model, tuple(can), pin, bool(direct), int(power), pool)
            self._waiting[name] = self._waiting.get(name, 0) + 1
            try:
                while True:
                    me = self._seen[name]
                    for t in self._tasks.values():
                        if t.state == "pending" and self._eligible(me, t.kind, t.direct, t.pool) and not self._stronger_waits(name, me, t):
                            t.state, t.worker = "leased", name
                            t.leased_at = self._clock()
                            t.lease_until = t.leased_at + self.lease_seconds
                            return t
                    remaining = end - self._clock()
                    if remaining <= 0:
                        return None
                    self._cond.wait(min(remaining, 0.5))
                    self._touch(name)                      # a worker that is long-polling is alive
            finally:
                self._waiting[name] -= 1
                if self._waiting[name] <= 0:
                    del self._waiting[name]

    def _stronger_waits(self, name: str, me: _Seen, t: Task) -> bool:
        """Is a more capable, eligible worker idle right now (inside a poll) and is this task still young enough to wait for it?"""
        if self._clock() - t.created >= self.YIELD_SECONDS:
            return False
        now = self._clock()
        for other, n in self._waiting.items():
            s = self._seen.get(other)
            if other != name and n > 0 and s and s.power > me.power and now - s.at <= self.online_seconds and self._eligible(s, t.kind, t.direct, t.pool):
                return True
        return False

    def _leased(self, task_id: str, name: str) -> Optional[Task]:
        t = self._tasks.get(task_id)
        return t if t and t.state == "leased" and t.worker == name else None

    def kind_of(self, task_id: str, name: str) -> Optional[str]:
        with self._cond:
            t = self._leased(task_id, name)
            return t.kind if t else None

    def file_for(self, task_id: str, name: str, worker: str) -> Optional[Path]:
        """A file of the task's manifest, only for the worker holding the lease. The name is looked up, never joined to a path."""
        with self._cond:
            t = self._leased(task_id, worker)
            if t is None:
                return None
            self._touch(worker)
            return t.files.get(name)

    def audio_for(self, task_id: str, name: str) -> Optional[Path]:
        return self.file_for(task_id, "audio", name)

    def upload_path(self, task_id: str, worker: str, name: str) -> Optional[Path]:
        """Where the worker may put ``name`` (only an allowed name, only for a render task it holds)."""
        with self._cond:
            t = self._leased(task_id, worker)
            if t is None or t.kind != "render" or t.upload_dir is None or name not in UPLOADS:
                return None
            self._touch(worker)
            return t.upload_dir / name

    def commit_upload(self, task_id: str, worker: str, name: str, path: Path) -> bool:
        with self._cond:
            t = self._leased(task_id, worker)
            if t is None or name not in UPLOADS:
                return False
            t.uploads[name] = path
            return True

    # a finished clip sent straight to the bucket -------------------------------------------------------------------------
    # The MP4 would otherwise come back through the app's own domain, and the proxy in front of it refuses a request of about 100 MB.
    # The worker gets presigned part URLs for ONE object that belongs to its task (``worker/<task id>.mp4``, an allow-listed name),
    # puts the parts there, and says so; this server completes the upload, checks the size, copies the object next to the other
    # uploads, deletes it from the bucket and probes it like any other worker clip. The worker never holds a storage key.
    def direct_plan(self, task_id: str, worker: str, size: int) -> Optional[dict]:
        """Presigned part URLs for the clip, or None when there is no bucket. ``WorkerError`` when the task is not this worker's."""
        from .storage import StorageError
        st = self.storage
        if st is None:
            return None
        with self._cond:
            t = self._leased(task_id, worker)
            if t is None or t.kind != "render" or t.plan is not None:
                raise WorkerError("this task cannot take an upload now")
            if not (1024 <= size <= self.max_direct_bytes):
                raise WorkerError("that clip is not a size this server accepts")
            t.plan = {}                                         # claimed: a second request is refused while the URLs are being made
            self._touch(worker)
            job_id = t.job_id
        rel = f"worker/{task_id}.mp4"
        count = -(-size // DIRECT_PART)
        part = DIRECT_PART if count > 1 else size
        upload_id = None
        try:
            upload_id = st.create_multipart(job_id, rel)
            parts = [{"n": n, "length": part if n < count else size - part * (count - 1),
                      "url": st.presign_part(job_id, rel, upload_id, n, part if n < count else size - part * (count - 1))}
                     for n in range(1, count + 1)]
        except (StorageError, ValueError):
            if upload_id:
                self._abort(job_id, rel, upload_id)
            with self._cond:
                t.plan = None
            raise WorkerError("the file storage could not be reached") from None
        with self._cond:
            t.plan = {"id": upload_id, "rel": rel, "size": size, "count": count}
        return {"part_size": part, "parts": parts}

    def _abort(self, job_id: str, rel: str, upload_id: str) -> None:
        from .storage import StorageError
        try:
            self.storage.abort_multipart(job_id, rel, upload_id)
        except (StorageError, ValueError):
            pass                                                # the expiry sweep and the job's purge remove what is left

    def direct_finish(self, task_id: str, worker: str, parts: list[tuple[int, str]]) -> bool:
        """Put the parts together, check the size and bring the clip to the server's disk. False = refused (the worker reports a failure
        and the server renders the clip itself)."""
        from .storage import StorageError
        st = self.storage
        with self._cond:
            t = self._leased(task_id, worker)
            plan = dict(t.plan) if t and t.plan else None
            if st is None or t is None or not plan or not plan.get("id") or t.direct_used or t.upload_dir is None:
                return False
            self._touch(worker)
            job_id, upload_dir = t.job_id, t.upload_dir
        if sorted(n for n, _e in parts) != list(range(1, plan["count"] + 1)):
            self._abort(job_id, plan["rel"], plan["id"])
            return False
        dest = upload_dir / "clip.mp4"
        try:
            st.complete_multipart(job_id, plan["rel"], plan["id"], parts)
            if st.size(job_id, plan["rel"]) != plan["size"]:
                return False                                    # not the size that was announced
            upload_dir.mkdir(parents=True, exist_ok=True)
            st.get_file(job_id, plan["rel"], dest)
        except (StorageError, ValueError, OSError):
            return False
        finally:
            try:
                st.delete(job_id, plan["rel"])
            except (StorageError, ValueError):
                pass
        if not self.commit_upload(task_id, worker, "clip.mp4", dest):
            dest.unlink(missing_ok=True)
            return False
        with self._cond:
            t.direct_used = True
        return True

    def discard_direct(self, task: Task) -> None:
        """Whatever the task left in the bucket (an unfinished upload, an object) goes, whichever way the render ended."""
        plan = task.plan
        if not plan or not plan.get("id") or self.storage is None:
            return
        self._abort(task.job_id, plan["rel"], plan["id"])
        try:
            self.storage.delete(task.job_id, plan["rel"])
        except Exception:                                       # noqa: BLE001  (a purge of the job's prefix removes it later)
            pass

    def heartbeat(self, task_id: str, name: str) -> bool:
        """Extend the lease. False means the task is no longer this worker's: stop working on it."""
        with self._cond:
            t = self._leased(task_id, name)
            if t is None:
                return False
            self._touch(name)
            t.lease_until = self._clock() + self.lease_seconds
            return True

    def finish(self, task_id: str, name: str, *, result: Optional[dict] = None, error: str = "") -> bool:
        with self._cond:
            t = self._leased(task_id, name)
            if t is None:
                return False
            self._touch(name)
            base = _machine(name)                          # tasks overlapping on one machine share its hourly price, not pay it twice
            busy = sum(1 for x in self._tasks.values() if x.state == "leased" and x.worker and _machine(x.worker) == base)
            if not error and t.kind == "render" and "clip.mp4" not in t.uploads:
                error = "the worker reported success but sent no clip"
            if error:
                t.state, t.error = "failed", error[:300]
            else:
                t.state, t.result = "done", result or {}
            record = {"job": t.job_id, "kind": t.kind, "worker": name, "seconds": round(max(0.0, self._clock() - t.leased_at), 1), "ok": not error,
                      "rate": float(self.rate_for(name) or 0.0) / max(1, busy)}
            self._cond.notify_all()
        if self.on_task_end is not None:
            try:
                self.on_task_end(record)
            except Exception:                                   # noqa: BLE001  (a bookkeeping failure must never fail the task)
                pass
        return True


# --- the job side: a transcribe function that prefers a worker ---------------------------------------------------------

def extract_audio(media: str | Path, dest: Path) -> Path:
    """16 kHz mono AAC: small enough to send (about 0.3 MB a minute) and what speech recognition wants anyway."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(media), "-vn", "-ac", "1", "-ar", "16000",
                        "-c:a", "aac", "-b:a", "48k", str(dest)], capture_output=True, text=True, timeout=900)
    if r.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        raise WorkerError("the audio could not be extracted for the GPU worker")
    return dest


def _local_transcribe(media, language, progress=None, check=lambda: None):
    from ..asr import release_models, transcribe
    try:
        return transcribe(media, language, progress=progress, check=check)
    finally:
        release_models()


@dataclass
class RemoteTranscriber:
    """Drop-in for ``asr.transcribe`` (``make(transcribe_fn=...)``): try a worker, else the server's own recognition."""
    hub: WorkerHub
    workdir: Path
    check: Callable[[], None] = lambda: None
    fallback: Callable = field(default=_local_transcribe)

    def __call__(self, media, language=None, progress=None, check=None) -> Transcript:
        say = progress or (lambda _m: None)
        local_check = check or self.check                                  # the job's own cancel check (the hub's wait uses ``self.check``)
        if not self.hub.online():
            return self.fallback(media, language, progress=say, **({"check": local_check} if self.fallback is _local_transcribe else {}))           # nothing out there: do not wait for it
        audio = self.workdir / "worker-audio.m4a"
        try:
            try:
                extract_audio(media, audio)
                say("sending the audio to the GPU worker")
                task = self.hub.submit(self.workdir.name, audio, language)
                result = self.hub.wait(task, self.check, say=say)
                tr = parse_transcript({"language": result.get("language") or language or "en",
                                       "words": [{"w": w["w"], "s": w["s"], "e": w["e"]} for w in result.get("words", [])]})
                tr.engine = str(result.get("engine") or "worker")
                say(f"the GPU worker transcribed {len(tr.words)} words")
                return tr
            except (WorkerError, TranscriptError, KeyError, TypeError, ValueError) as e:      # a cancelled job raises something else
                say(f"the GPU worker did not deliver ({e}); transcribing on the server instead")
                return self.fallback(media, language, progress=say, **({"check": local_check} if self.fallback is _local_transcribe else {}))
        finally:
            audio.unlink(missing_ok=True)


# --- the job side: who is who in the video, on a worker when one can ---------------------------------------------------------

def speaker_map_from_result(got: "SpeakersIn", video: Path) -> "object":
    """A worker's validated answer as a ``SpeakerMap`` for THIS server's copy of the video (its signature is ours, never the worker's)."""
    from .. import seats
    real = 0.0
    from ..indic import media_duration
    real = media_duration(str(video)) or 0.0
    if real and abs(real - got.duration) > 2.0:
        raise WorkerError(f"the worker looked at a video of {got.duration:.0f} s, this one is {real:.0f} s")
    from .. import voices
    return seats.SpeakerMap([seats.Seat(s.cx, s.cy, s.w, s.h, s.seen, s.live) for s in got.seats], [voices.Turn(a, b, v) for a, b, v in sorted(got.turns)],
                            dict(got.voice_seat), dict(got.lift), got.duration, {}, seats.video_signature(video), got.picture_t)


@dataclass
class RemoteSpeakers:
    """Drop-in for ``seats.build_speaker_map`` (``make(speakers_fn=...)``): ask a worker that can (it has the GPU for the voice model), else do it here."""
    hub: WorkerHub
    job_id: str
    check: Callable[[], None] = lambda: None
    say: Callable[[str], None] = lambda _m: None
    fallback: Optional[Callable] = None
    max_seconds: float = 1800.0

    def __call__(self, video, out_dir, *, detector=None, say=None, check=None, **kw):
        from .. import seats
        say, check = say or self.say, check or self.check
        found, cached = seats.cached_map(video, out_dir)
        if found:
            return cached

        def local():
            return (self.fallback or seats.build_speaker_map)(video, out_dir, detector=detector, say=say, check=check, **kw)
        if not self.hub.online("speakers"):
            return local()
        video = Path(video)
        task = None
        try:
            say("asking the GPU worker who is who in the video")
            task = self.hub.submit_speakers(self.job_id, {f"media/{video.name}": video}, {"job_id": self.job_id}, self.max_seconds)
            result = self.hub.wait(task, check, say=say)
            sm = speaker_map_from_result(SpeakersIn.model_validate(result["speakers"]), video)
            say("the GPU worker matched the voices to the people")
            return seats.store_map(out_dir, video, sm, None)
        except (WorkerError, ValidationError, KeyError, TypeError, ValueError, OSError) as e:      # a cancelled job raises something else
            say(f"the GPU worker did not deliver ({str(e)[:120]}); working it out on the server instead")
            return local()


@dataclass
class RemoteFraming:
    """``framing_fn`` for ``framing.apply_framing``: the clips' framing (face tracking: CPU work on a long video) done by a worker that can, in parallel,
    else here. Only for what has to look at the picture: with a voice map the framing follows the voices, which is cheap and stays on the server."""
    hub: WorkerHub
    job_id: str
    check: Callable[[], None] = lambda: None
    say: Callable[[str], None] = lambda _m: None

    def __call__(self, video, spans, *, multi: str = "switch", graphics: bool = False, speakers=None) -> dict:
        say = self.say
        if (speakers is not None and multi != "off" and not graphics) or not spans or not self.hub.online("framing"):
            return {}
        video = Path(video)
        asked = {cid for cid, _a, _b in spans}
        try:
            say(f"asking the GPU worker to frame the {len(spans)} clip(s)")
            meta = {"job_id": self.job_id, "multi": multi, "graphics": bool(graphics),
                    "clips": [{"id": cid, "start": round(float(a), 3), "end": round(float(b), 3)} for cid, a, b in spans]}
            task = self.hub.submit_framing(self.job_id, {f"media/{video.name}": video}, meta, max_seconds=min(7200.0, 600.0 + 120.0 * len(spans)))
            result = self.hub.wait(task, self.check, say=say)
            got = FramingIn.model_validate(result["framing"])
            if any(o.id not in asked for o in got.clips) or len({o.id for o in got.clips}) != len(got.clips):
                raise WorkerError("the worker framed clips that were not asked for")
            say(f"the GPU worker framed {len(got.clips)} clip(s)")
            return {o.id: (framing_from_in(o), o.seconds) for o in got.clips}
        except (WorkerError, ValidationError, KeyError, TypeError, ValueError, OSError) as e:      # a cancelled job raises something else
            say(f"the GPU worker did not deliver ({str(e)[:120]}); framing the clips on the server instead")
            return {}


# --- the job side: a render function that prefers a worker -------------------------------------------------------------

class ClipRejected(WorkerError):
    """What the worker sent back is not the clip we asked for."""


def validate_clip(path: Path, edl, clip, *, max_bytes: int) -> None:
    """A worker's MP4 is untrusted: it must be a real MP4 with an H.264 picture of the size the EDL asks for, about as long as the clip
    (plus the ending video when the clip has one), and not absurdly large. Anything else is rejected and the server renders itself."""
    size = path.stat().st_size
    if size < 1024:
        raise ClipRejected("the clip the worker sent is empty")
    if size > max_bytes:
        raise ClipRejected("the clip the worker sent is too large")
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_entries",
                        "format=format_name,duration:stream=codec_type,codec_name,width,height", "-i", str(path)],
                       capture_output=True, text=True, timeout=60)
    try:
        info = json.loads(r.stdout)
        formats = str(info["format"]["format_name"]).split(",")
        duration = float(info["format"]["duration"])
        video = next(s for s in info["streams"] if s.get("codec_type") == "video")
    except (ValueError, KeyError, StopIteration, TypeError):
        raise ClipRejected("the clip the worker sent is not a readable video") from None
    if "mp4" not in formats:
        raise ClipRejected("the clip the worker sent is not an MP4")
    if video.get("codec_name") != "h264":
        raise ClipRejected("the clip the worker sent is not H.264")
    if (video.get("width"), video.get("height")) != (edl.render.width, edl.render.height):
        raise ClipRejected(f"the clip the worker sent is {video.get('width')}x{video.get('height')}, not {edl.render.width}x{edl.render.height}")
    base = float(clip.source_out) - float(clip.source_in)
    extra = (float(clip.outro.max_seconds) + clip.outro.gap_ms / 1000) if clip.outro and clip.outro.enabled else 0.0
    if not (base - 1.0 <= duration <= base + extra + 1.5):
        raise ClipRejected(f"the clip the worker sent is {duration:.1f} s long, expected about {base:.1f} s")


@dataclass
class RemoteRenderer:
    """Drop-in for ``pipeline.render_clip`` (``make(render_fn=...)``): render on a worker when a matching one is online, else here."""
    hub: WorkerHub
    check: Callable[[], None] = lambda: None
    say: Callable[[str], None] = lambda _m: None
    fallback: Optional[Callable] = None
    max_seconds: float = 3600.0
    max_clip_seconds: float = 60.0          # longer clips go only to a worker that can send them straight to the bucket; without one they are rendered here (0 = no limit)

    def _local(self, *args, **kw):
        fn = self.fallback
        if fn is None:
            from ..pipeline import render_clip as fn
        return fn(*args, **kw)

    def __call__(self, edl, clip_id, output, *, workdir, media_root, gsap_js=None, workers=None, asset_root=None, **kw):
        def local():
            return self._local(edl, clip_id, output, workdir=workdir, media_root=media_root, gsap_js=gsap_js, workers=workers,
                               asset_root=asset_root, **kw)
        clip = next((c for c in edl.clips if c.id == clip_id), None)
        if clip is None or not self.hub.online("render"):
            return local()                                          # nothing out there (or a bad clip id: the real renderer says so)
        long = bool(self.max_clip_seconds) and clip.duration > self.max_clip_seconds
        if long and not self.hub.online("render", direct=True):            # the MP4 would have to come back through the proxy that caps a request near 100 MB
            self.say(f"{clip_id}: this clip is long, so it is rendered on the server")
            return local()
        taskdir = Path(workdir) / "worker-tasks" / uuid.uuid4().hex
        task = None
        try:
            files, meta = self._bundle(edl, clip, taskdir, media_root, asset_root)
            self.say(f"{clip_id}: rendering on the GPU worker ({sum(p.stat().st_size for p in files.values() if p.is_file()) / 1e6:.0f} MB of files to send)")
            task = self.hub.submit_render(edl.job_id, files, meta, taskdir / "in", self.max_seconds, direct=long)
            result = self.hub.wait(task, self.check, say=self.say)
            produced = task.uploads["clip.mp4"]
            validate_clip(produced, edl, clip, max_bytes=self.hub.max_direct_bytes if task.direct_used else self.hub.max_upload_bytes)
            warnings = self._place(produced, Path(output), edl, clip, [str(w)[:300] for w in result.get("warnings", [])][:20])
            self.say(f"{clip_id}: the GPU worker rendered it")
            return Path(output), SimpleNamespace(warnings=warnings)
        except WorkerError as e:
            reason = str(e)
        except (OSError, ValueError, KeyError, TypeError):             # never put a server path in a job's log
            reason = "an internal problem"
        finally:
            if task is not None:
                self.hub.discard_direct(task)
            shutil.rmtree(taskdir, ignore_errors=True)
        self.say(f"{clip_id}: the GPU worker did not deliver ({reason}); rendering on the server instead")
        return local()

    @staticmethod
    def _bundle(edl, clip, taskdir: Path, media_root, asset_root) -> tuple[dict[str, Path], dict]:
        """Everything the render needs, named. The EDL's own paths are resolved with ``compile._inside``, so nothing outside the job's
        folders can ever be offered to a worker."""
        from ..compile import _inside
        mroot = Path(media_root)
        aroot = Path(asset_root) if asset_root else mroot
        taskdir.mkdir(parents=True, exist_ok=True)
        edl_file = taskdir / "edl.json"
        edl_file.write_text(edl.model_dump_json(exclude_none=True), encoding="utf-8")
        files: dict[str, Path] = {"edl.json": edl_file}
        source = _inside(mroot, edl.source.uri)
        if not source.is_file():
            raise WorkerError("the source video is not on the server any more")
        files[f"media/{edl.source.uri}"] = source
        if clip.layout.cutout_uri:
            files[f"media/{clip.layout.cutout_uri}"] = _inside(mroot, clip.layout.cutout_uri)
        for a in edl.assets:
            if a.uri:
                p = _inside(aroot, a.uri)
                if p.is_file():
                    files[f"asset/{a.uri}"] = p
        return files, {"job_id": edl.job_id, "clip_id": clip.id, "expected_seconds": float(clip.source_out) - float(clip.source_in)}

    @staticmethod
    def _place(produced: Path, output: Path, edl, clip, warnings: list[str]) -> list[str]:
        """Put the validated clip where a local render would have, and make the files that go with it HERE (never taken from the worker)."""
        from .. import branding
        from ..provenance import write_sidecar
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(produced), str(output))
        cover = output.with_name(output.stem + ".cover.png")
        if clip.cover and clip.cover.enabled:
            try:
                branding.extract_cover(output, cover)
            except branding.BrandError as e:
                warnings.append(f"the cover image was not saved: {e}")
        else:
            cover.unlink(missing_ok=True)
        write_sidecar(edl, clip, output)
        return warnings


def hub_view(hub, job) -> "Optional[HubView]":
    """The hub limited to the GPU the person chose for ``job`` (``auto`` | ``home`` | ``pod``); None when there is no hub."""
    if hub is None:
        return None
    choice = getattr(getattr(job, "params", None), "gpu", "auto")
    return HubView(hub, {"home": "home", "pod": "rented"}.get(choice, ""))
