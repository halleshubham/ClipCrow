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

from pydantic import BaseModel, ConfigDict, Field

from ..planner.transcript import Transcript, TranscriptError, parse_transcript

TASK_ID = r"^[0-9a-f]{32}$"
WORKER_NAME = r"^[A-Za-z0-9._-]{1,40}$"
MAX_WORDS = 400_000
KINDS = ("transcribe", "render")
FILE_NAME = re.compile(r"^[A-Za-z0-9._@ /-]{1,200}$")          # what a manifest name may look like (and no "..", see _clean_name)
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
    can: list[Literal["transcribe", "render"]] = Field(default_factory=lambda: ["transcribe"], max_length=2)
    pin: str = Field(default="", max_length=40)                    # the HyperFrames version this worker renders with


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


class WorkerHub:
    def __init__(self, token: str, *, pickup_seconds: float = 120.0, lease_seconds: float = 90.0,
                 online_seconds: float = 60.0, render_pin: str = "", max_upload_bytes: int = 300 * 1024 * 1024,
                 clock: Callable[[], float] = time.monotonic):
        if not token or len(token) < 16:
            raise ValueError("CLIPCROW_WORKER_TOKEN must be at least 16 characters (try: openssl rand -hex 32)")
        self._token = token.encode()
        self.pickup_seconds, self.lease_seconds, self.online_seconds = pickup_seconds, lease_seconds, online_seconds
        self.render_pin, self.max_upload_bytes = render_pin, max_upload_bytes
        self._clock = clock
        self._cond = threading.Condition()
        self._tasks: dict[str, Task] = {}
        self._seen: dict[str, _Seen] = {}

    # authentication ---------------------------------------------------------------------------------------------------
    def check_token(self, presented: str) -> bool:
        return hmac.compare_digest(presented.encode(), self._token)

    # who is out there ------------------------------------------------------------------------------------------------
    def _touch(self, name: str, device: str = "", model: str = "", can: Optional[tuple] = None, pin: Optional[str] = None) -> None:
        old = self._seen.get(name)
        self._seen[name] = _Seen(self._clock(), device or (old.device if old else ""), model or (old.model if old else ""),
                                 can if can is not None else (old.can if old else ("transcribe",)),
                                 pin if pin is not None else (old.pin if old else ""))

    def _eligible(self, s: _Seen, kind: Optional[str]) -> bool:
        """Is this worker one that may take a ``kind`` task? A render needs the same HyperFrames pin as this server."""
        if kind is None:
            return True
        if kind not in s.can:
            return False
        return kind != "render" or (bool(self.render_pin) and s.pin == self.render_pin)

    def online(self, kind: Optional[str] = None) -> bool:
        with self._cond:
            return self._online(kind)

    def _online(self, kind: Optional[str]) -> bool:
        now = self._clock()
        return any(now - s.at <= self.online_seconds and self._eligible(s, kind) for s in self._seen.values())

    def describe(self) -> list[dict]:
        with self._cond:
            now = self._clock()
            return [{"name": n, "device": s.device, "model": s.model, "can": list(s.can), "pin": s.pin,
                     "online": now - s.at <= self.online_seconds} for n, s in sorted(self._seen.items())]

    # the job side ----------------------------------------------------------------------------------------------------
    def _add(self, task: Task) -> Task:
        with self._cond:
            self._tasks[task.id] = task
            self._cond.notify_all()
        return task

    def submit(self, job_id: str, audio: Path, language: Optional[str]) -> Task:
        """A transcription task."""
        return self._add(Task(uuid.uuid4().hex, job_id, "transcribe", {"audio": audio}, {"language": language}, self._clock(),
                              language=language))

    def submit_render(self, job_id: str, files: dict[str, Path], meta: dict, upload_dir: Path, max_seconds: float = 3600.0) -> Task:
        """A render task. ``files`` maps manifest names to server paths; the names are checked here so a bad one never reaches a worker."""
        for name in files:
            if not _clean_name(name):
                raise WorkerError("a file in this render has a name the worker protocol does not allow")
        upload_dir.mkdir(parents=True, exist_ok=True)
        return self._add(Task(uuid.uuid4().hex, job_id, "render", dict(files), dict(meta), self._clock(),
                              max_seconds=max_seconds, upload_dir=upload_dir))

    def wait(self, task: Task, check: Callable[[], None], tick: float = 0.25) -> dict:
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
                        if now - task.created > self.pickup_seconds:
                            raise WorkerUnavailable("no GPU worker picked the task up in time")
                        if not self._online(task.kind):
                            raise WorkerUnavailable("the GPU worker went offline")
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

    # the worker side -------------------------------------------------------------------------------------------------
    def poll(self, name: str, wait: float, device: str = "", model: str = "", can: tuple = ("transcribe",), pin: str = "") -> Optional[Task]:
        """Hand the worker a pending task it can do (leased to it), holding the request open up to ``wait`` seconds."""
        end = self._clock() + wait
        with self._cond:
            self._touch(name, device, model, tuple(can), pin)
            while True:
                me = self._seen[name]
                for t in self._tasks.values():
                    if t.state == "pending" and self._eligible(me, t.kind):
                        t.state, t.worker = "leased", name
                        t.leased_at = self._clock()
                        t.lease_until = t.leased_at + self.lease_seconds
                        return t
                remaining = end - self._clock()
                if remaining <= 0:
                    return None
                self._cond.wait(min(remaining, 1.0))
                self._touch(name)                          # a worker that is long-polling is alive

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
            if not error and t.kind == "render" and "clip.mp4" not in t.uploads:
                error = "the worker reported success but sent no clip"
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
    extra = float(clip.outro.max_seconds) if clip.outro and clip.outro.enabled else 0.0
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
    max_clip_seconds: float = 100.0         # longer clips are rendered here: the MP4 comes back through a proxy that caps a request near 100 MB (0 = no limit)

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
        if self.max_clip_seconds and clip.duration > self.max_clip_seconds:
            self.say(f"{clip_id}: this clip is long, so it is rendered on the server")
            return local()
        taskdir = Path(workdir) / "worker-tasks" / uuid.uuid4().hex
        try:
            files, meta = self._bundle(edl, clip, taskdir, media_root, asset_root)
            self.say(f"{clip_id}: rendering on the GPU worker")
            task = self.hub.submit_render(edl.job_id, files, meta, taskdir / "in", self.max_seconds)
            result = self.hub.wait(task, self.check)
            produced = task.uploads["clip.mp4"]
            validate_clip(produced, edl, clip, max_bytes=self.hub.max_upload_bytes)
            warnings = self._place(produced, Path(output), edl, clip, [str(w)[:300] for w in result.get("warnings", [])][:20])
            self.say(f"{clip_id}: the GPU worker rendered it")
            return Path(output), SimpleNamespace(warnings=warnings)
        except WorkerError as e:
            reason = str(e)
        except (OSError, ValueError, KeyError, TypeError):             # never put a server path in a job's log
            reason = "an internal problem"
        finally:
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
