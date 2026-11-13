"""A remote worker for the slow, GPU-friendly step: speech recognition.

The worker is a program on a machine the operator owns (``clipcrow worker``). It PULLS: it asks this server for work over HTTPS, so
the machine opens no port and needs no tunnel. The server extracts the audio of a job and queues a task; a worker leases it,
downloads the audio, transcribes it and posts the words back.

Rules this module keeps:
  * Workers are authenticated by ONE shared secret (``CLIPCROW_WORKER_TOKEN``), compared in constant time. Without it the feature is
    off and the routes do not exist (404). It is a different secret from a person's login.
  * Everything a worker sends is untrusted: bounded by ``ResultIn``, then parsed like any other transcript. A worker that fails,
    stalls or vanishes can only cost time: the job falls back to the server's own recognition.
  * Only audio leaves the server, only to a worker that holds a lease on that task, and only while the lease lasts.
"""
from __future__ import annotations

import hmac
import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..planner.transcript import Transcript, TranscriptError, parse_transcript

TASK_ID = r"^[0-9a-f]{32}$"
WORKER_NAME = r"^[A-Za-z0-9._-]{1,40}$"
MAX_WORDS = 400_000


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


class HeartbeatIn(_M):
    worker: str = Field(pattern=WORKER_NAME)


class WordIn(_M):
    w: str = Field(max_length=100)
    s: float = Field(ge=0.0, le=86_400.0)
    e: float = Field(ge=0.0, le=86_400.0)


class ResultIn(_M):
    worker: str = Field(pattern=WORKER_NAME)
    language: Optional[str] = Field(default=None, pattern=r"^[a-z]{2,3}$")
    words: list[WordIn] = Field(default_factory=list, max_length=MAX_WORDS)
    error: str = Field(default="", max_length=300)


# --- the hub -----------------------------------------------------------------------------------------------------------

@dataclass
class Task:
    id: str
    job_id: str
    audio: Path
    language: Optional[str]
    created: float
    state: str = "pending"                  # pending | leased | done | failed | abandoned
    worker: str = ""
    lease_until: float = 0.0
    result: Optional[dict] = None
    error: str = ""


@dataclass
class _Seen:
    at: float
    device: str = ""
    model: str = ""


class WorkerHub:
    def __init__(self, token: str, *, pickup_seconds: float = 120.0, lease_seconds: float = 90.0,
                 online_seconds: float = 60.0, clock: Callable[[], float] = time.monotonic):
        if not token or len(token) < 16:
            raise ValueError("CLIPCROW_WORKER_TOKEN must be at least 16 characters (try: openssl rand -hex 32)")
        self._token = token.encode()
        self.pickup_seconds, self.lease_seconds, self.online_seconds = pickup_seconds, lease_seconds, online_seconds
        self._clock = clock
        self._cond = threading.Condition()
        self._tasks: dict[str, Task] = {}
        self._seen: dict[str, _Seen] = {}

    # authentication ---------------------------------------------------------------------------------------------------
    def check_token(self, presented: str) -> bool:
        return hmac.compare_digest(presented.encode(), self._token)

    # who is out there ------------------------------------------------------------------------------------------------
    def _touch(self, name: str, device: str = "", model: str = "") -> None:
        old = self._seen.get(name)
        self._seen[name] = _Seen(self._clock(), device or (old.device if old else ""), model or (old.model if old else ""))

    def online(self) -> bool:
        with self._cond:
            now = self._clock()
            return any(now - s.at <= self.online_seconds for s in self._seen.values())

    def describe(self) -> list[dict]:
        with self._cond:
            now = self._clock()
            return [{"name": n, "device": s.device, "model": s.model, "online": now - s.at <= self.online_seconds}
                    for n, s in sorted(self._seen.items())]

    # the job side ----------------------------------------------------------------------------------------------------
    def submit(self, job_id: str, audio: Path, language: Optional[str]) -> Task:
        task = Task(uuid.uuid4().hex, job_id, audio, language, self._clock())
        with self._cond:
            self._tasks[task.id] = task
            self._cond.notify_all()
        return task

    def wait(self, task: Task, check: Callable[[], None], tick: float = 0.25) -> dict:
        """Block until the worker finishes the task. ``check`` raises when the job was cancelled. Raises ``WorkerUnavailable`` when
        nobody takes the task within ``pickup_seconds`` (or nobody is online any more), or the worker stops sending heartbeats."""
        try:
            while True:
                check()
                with self._cond:
                    now = self._clock()
                    if task.state == "done":
                        return task.result or {}
                    if task.state == "failed":
                        raise WorkerError(task.error or "the worker could not transcribe this")
                    if task.state == "pending":
                        if now - task.created > self.pickup_seconds:
                            raise WorkerUnavailable("no GPU worker picked the task up in time")
                        if not any(now - s.at <= self.online_seconds for s in self._seen.values()):
                            raise WorkerUnavailable("the GPU worker went offline")
                    elif task.state == "leased" and now > task.lease_until:
                        raise WorkerUnavailable("the GPU worker stopped answering")
                    self._cond.wait(tick)
        finally:
            with self._cond:
                if task.state in ("pending", "leased"):
                    task.state = "abandoned"                # a late result from the worker is refused
                self._tasks.pop(task.id, None)

    # the worker side -------------------------------------------------------------------------------------------------
    def poll(self, name: str, wait: float, device: str = "", model: str = "") -> Optional[Task]:
        """Hand the worker a pending task (leased to it), holding the request open up to ``wait`` seconds."""
        end = self._clock() + wait
        with self._cond:
            self._touch(name, device, model)
            while True:
                for t in self._tasks.values():
                    if t.state == "pending":
                        t.state, t.worker, t.lease_until = "leased", name, self._clock() + self.lease_seconds
                        return t
                remaining = end - self._clock()
                if remaining <= 0:
                    return None
                self._cond.wait(min(remaining, 1.0))
                self._touch(name)                          # a worker that is long-polling is alive

    def _leased(self, task_id: str, name: str) -> Optional[Task]:
        t = self._tasks.get(task_id)
        return t if t and t.state == "leased" and t.worker == name else None

    def audio_for(self, task_id: str, name: str) -> Optional[Path]:
        with self._cond:
            t = self._leased(task_id, name)
            if t is None:
                return None
            self._touch(name)
            return t.audio

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
            if error:
                t.state, t.error = "failed", error[:300]
            else:
                t.state, t.result = "done", result or {}
            self._cond.notify_all()
            return True


# --- the job side: a transcribe function that prefers a worker ---------------------------------------------------------

def extract_audio(media: str | Path, dest: Path) -> Path:
    """16 kHz mono AAC: small enough to send (about 0.3 MB a minute) and what speech recognition wants anyway."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(media), "-vn", "-ac", "1", "-ar", "16000",
                        "-c:a", "aac", "-b:a", "48k", str(dest)], capture_output=True, text=True, timeout=900)
    if r.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        raise WorkerError("the audio could not be extracted for the GPU worker")
    return dest


def _local_transcribe(media, language, progress=None):
    from ..asr import release_models, transcribe
    try:
        return transcribe(media, language, progress=progress)
    finally:
        release_models()


@dataclass
class RemoteTranscriber:
    """Drop-in for ``asr.transcribe`` (``make(transcribe_fn=...)``): try a worker, else the server's own recognition."""
    hub: WorkerHub
    workdir: Path
    check: Callable[[], None] = lambda: None
    fallback: Callable = field(default=_local_transcribe)

    def __call__(self, media, language=None, progress=None) -> Transcript:
        say = progress or (lambda _m: None)
        if not self.hub.online():
            return self.fallback(media, language, progress=say)           # nothing out there: do not wait for it
        audio = self.workdir / "worker-audio.m4a"
        try:
            try:
                extract_audio(media, audio)
                say("sending the audio to the GPU worker")
                task = self.hub.submit(self.workdir.name, audio, language)
                result = self.hub.wait(task, self.check)
                tr = parse_transcript({"language": result.get("language") or language or "en",
                                       "words": [{"w": w["w"], "s": w["s"], "e": w["e"]} for w in result.get("words", [])]})
                say(f"the GPU worker transcribed {len(tr.words)} words")
                return tr
            except (WorkerError, TranscriptError, KeyError, TypeError, ValueError) as e:      # a cancelled job raises something else
                say(f"the GPU worker did not deliver ({e}); transcribing on the server instead")
                return self.fallback(media, language, progress=say)
        finally:
            audio.unlink(missing_ok=True)
