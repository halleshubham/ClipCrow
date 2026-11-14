"""``clipcrow worker``: run speech recognition for a ClipCrow server on THIS machine (typically one with an NVIDIA GPU).

The worker only ever makes outgoing HTTPS requests to the server: it asks for a task, downloads the task's audio, transcribes it
here and uploads the words. No port is opened on this machine. It authenticates with the shared secret in the environment variable
CLIPCROW_WORKER_TOKEN (never a command-line argument, which would end up in shell history and process lists).

It can also RENDER clips (when the render tools are present, as in the ClipCrow image): it downloads the clip's EDL, source video and
assets, renders with the same pinned HyperFrames the server uses, and uploads the MP4. A source video is cached between the clips of one
job and the cache is trimmed to a size limit.

The server treats everything this program sends as untrusted input and falls back to its own work when a worker is slow or gone, so
stopping this program at any time is safe.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

POLL_WAIT = 20.0
BACKOFF = (2, 5, 10, 30, 60)


class Transport:
    """The HTTP the worker needs. Tests plug in a fake one; the real one is ``HttpTransport``."""

    def post(self, path: str, body: dict, *, timeout: float = 60.0) -> dict:
        raise NotImplementedError

    def download(self, path: str, dest: Path, *, timeout: float = 600.0) -> None:
        raise NotImplementedError

    def upload(self, path: str, src: Path, *, timeout: float = 900.0) -> None:
        raise NotImplementedError


class HttpTransport(Transport):
    def __init__(self, base: str, token: str):
        if not base.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise ValueError("use an https:// address (plain http is only allowed for localhost)")
        self.base, self._token = base.rstrip("/"), token

    def _req(self, method: str, path: str, data: Optional[bytes] = None) -> urllib.request.Request:
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", "clipcrow-worker")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        return req

    def post(self, path, body, *, timeout=60.0):
        try:
            with urllib.request.urlopen(self._req("POST", path, json.dumps(body).encode()), timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise WorkerHttpError(e.code, f"{e.code} from the server") from None

    def download(self, path, dest, *, timeout=600.0):
        try:
            with urllib.request.urlopen(self._req("GET", path), timeout=timeout) as r, open(dest, "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
        except urllib.error.HTTPError as e:
            raise WorkerHttpError(e.code, f"{e.code} from the server") from None

    def upload(self, path, src, *, timeout=900.0):
        """PUT a file as the request body, streamed from disk (not read into memory)."""
        try:
            with open(src, "rb") as f:
                req = self._req("PUT", path)
                req.data = f
                req.add_header("Content-Type", "application/octet-stream")
                req.add_header("Content-Length", str(os.path.getsize(src)))
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    r.read()
        except urllib.error.HTTPError as e:
            raise WorkerHttpError(e.code, f"{e.code} from the server") from None


class WorkerHttpError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def default_transcribe(model: str) -> tuple[Callable, str]:
    """A transcribe function using ``model`` on the GPU when CTranslate2 can see one, plus a label for the server's dashboard."""
    from .asr import AsrConfig, _cuda_devices, resolve_device, transcribe
    cfg = AsrConfig(model=model, indic_model=None)              # a GPU copes with the large model for every language
    device, compute = resolve_device(cfg.device, cfg.compute_type)
    label = f"{device} ({compute})"
    if device == "cpu":
        print("warning: no NVIDIA GPU is visible to faster-whisper, so this worker will run on the CPU. "
              "Install the CUDA 12 libraries (pip install nvidia-cublas-cu12 'nvidia-cudnn-cu12==9.*') and a current NVIDIA driver.",
              file=sys.stderr)
    return (lambda media, language=None, progress=None: transcribe(media, language, config=cfg, progress=progress)), label


def render_ready() -> tuple[bool, str]:
    """Can this machine render a clip? The same tool checks as ``clipcrow selftest`` (FFmpeg, HyperFrames, Chrome, fonts, GSAP)."""
    try:
        from .selftest import checks
        for label, fn in checks():
            if label in ("ffmpeg", "ffprobe", "hyperframes", "chrome", "fonts", "gsap"):
                fn()
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:120]}"


def cache_root() -> Path:
    return Path(os.environ.get("CLIPCROW_WORKER_CACHE") or Path.home() / ".cache" / "clipcrow-worker")


def safe_join(base: Path, rel: str) -> Path:
    """``base / rel``, refused unless it stays inside ``base``. Names come from the server, but a worker never trusts a path."""
    if not rel or rel.startswith("/") or "\\" in rel or ".." in rel.split("/"):
        raise ValueError(f"unsafe file name {rel!r}")
    path = (base / rel).resolve()
    if base.resolve() not in path.parents:
        raise ValueError(f"unsafe file name {rel!r}")
    return path


def evict_cache(root: Path, max_bytes: int, keep: Optional[Path] = None) -> None:
    """Delete the least recently used job folders until the cache fits ``max_bytes`` (never ``keep``, the job in use)."""
    if not root.is_dir():
        return
    def size(d: Path) -> int:
        return sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
    jobs = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime)
    sizes = {d: size(d) for d in jobs}
    total = sum(sizes.values())
    for d in jobs:
        if total <= max_bytes:
            break
        if keep is not None and d.resolve() == keep.resolve():
            continue
        shutil.rmtree(d, ignore_errors=True)
        total -= sizes[d]


def run_worker(transport: Transport, name: str, *, model: str = "large-v3", transcribe: Optional[Callable] = None,
               label: str = "", stop: Callable[[], bool] = lambda: False, log: Callable[[str], None] = print,
               max_tasks: Optional[int] = None, poll_wait: float = POLL_WAIT, can_render: bool = False,
               render_fn: Optional[Callable] = None, cache: Optional[Path] = None, cache_gb: float = 20.0,
               pin: Optional[str] = None) -> int:
    """Poll, do the task, upload; until ``stop()`` or ``max_tasks`` tasks. Returns the number of tasks finished."""
    if transcribe is None:
        transcribe, label = default_transcribe(model)
    if pin is None:
        from .render import HYPERFRAMES_VERSION as pin
    can = ["transcribe"] + (["render"] if can_render else [])
    done, failures = 0, 0
    while not stop() and (max_tasks is None or done < max_tasks):
        try:
            reply = transport.post("/api/worker/poll", {"worker": name, "wait": poll_wait, "device": label[:60], "model": model[:40],
                                                         "can": can, "pin": pin}, timeout=poll_wait + 20)
            failures = 0
        except WorkerHttpError as e:
            if e.status in (401, 404):
                log(f"the server refused this worker ({e.status}): check CLIPCROW_WORKER_TOKEN and that the server has one set")
                return done
            log(f"server error: {e}")
            time.sleep(BACKOFF[min(failures, len(BACKOFF) - 1)]); failures += 1
            continue
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            log(f"cannot reach the server ({type(e).__name__}); retrying")
            time.sleep(BACKOFF[min(failures, len(BACKOFF) - 1)]); failures += 1
            continue
        task = reply.get("task")
        if not task:
            continue
        if task.get("kind") == "render":
            ok = handle_render(transport, name, task, log, cache=cache or cache_root(), render_fn=render_fn, cache_gb=cache_gb)
        else:
            ok = handle(transport, name, task, transcribe, log)
        if ok:
            done += 1
    return done


class Lease:
    """Keeps the server's lease on a task alive while this worker is busy, and notices when the server has given the task up."""

    def __init__(self, transport: Transport, name: str, tid: str, lease_seconds: float, beat_seconds: Optional[float] = None):
        self.transport, self.name, self.tid = transport, name, tid
        self.every = beat_seconds if beat_seconds is not None else max(5.0, lease_seconds / 3)
        self.lost, self._finished = threading.Event(), threading.Event()

    def __enter__(self) -> "Lease":
        threading.Thread(target=self._beat, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self._finished.set()

    def _beat(self) -> None:
        while not self._finished.wait(self.every):
            try:
                if not self.transport.post(f"/api/worker/tasks/{self.tid}/heartbeat", {"worker": self.name}, timeout=30).get("ok"):
                    self.lost.set()                              # the server gave the task to someone else or gave up on it
                    return
            except Exception:
                pass                                              # a missed beat is fine; the lease is longer than three of them


def _report_failure(transport: Transport, name: str, tid: str, e: Exception, log: Callable[[str], None]) -> None:
    log(f"task {tid[:8]} failed: {type(e).__name__}: {str(e)[:150]}")
    try:                                                          # tell the server so the job falls back at once, not after the lease
        transport.post(f"/api/worker/tasks/{tid}/result", {"worker": name, "error": f"{type(e).__name__}: {str(e)[:200]}"}, timeout=30)
    except Exception:
        pass


def handle(transport: Transport, name: str, task: dict, transcribe: Callable, log: Callable[[str], None],
           beat_seconds: Optional[float] = None) -> bool:
    """A transcription task. Returns True when a result was delivered."""
    tid = task["id"]
    started = time.monotonic()
    with Lease(transport, name, tid, float(task.get("lease_seconds") or 90), beat_seconds) as lease:
        try:
            with tempfile.TemporaryDirectory(prefix="clipcrow-worker-") as tmp:
                audio = Path(tmp) / "audio.m4a"
                transport.download(f"/api/worker/tasks/{tid}/audio?worker={name}", audio)
                log(f"task {tid[:8]}: transcribing {audio.stat().st_size / 1e6:.1f} MB of audio")
                tr = transcribe(str(audio), task.get("language"))
                body = {"worker": name, "language": tr.language,
                        "words": [{"w": w.w, "s": round(float(w.s), 3), "e": round(float(w.e), 3)} for w in tr.words]}
        except Exception as e:
            _report_failure(transport, name, tid, e, log)
            return False
    if lease.lost.is_set():
        log(f"task {tid[:8]}: the server no longer wants this result; dropped")
        return False
    try:
        ok = transport.post(f"/api/worker/tasks/{tid}/result", body, timeout=120).get("ok")
    except Exception as e:
        log(f"task {tid[:8]}: could not deliver the result ({type(e).__name__})")
        return False
    log(f"task {tid[:8]}: {len(body['words'])} words in {time.monotonic() - started:.0f} s" + ("" if ok else " (the server had already moved on)"))
    return bool(ok)


def handle_render(transport: Transport, name: str, task: dict, log: Callable[[str], None], *, cache: Path,
                  render_fn: Optional[Callable] = None, cache_gb: float = 20.0, beat_seconds: Optional[float] = None) -> bool:
    """A render task: fetch what the manifest lists (the source video is cached per job), render, upload the MP4."""
    tid, meta, files = task["id"], task.get("meta") or {}, task.get("files") or {}
    started = time.monotonic()
    job_id, clip_id = str(meta.get("job_id", "")), str(meta.get("clip_id", ""))
    if not job_id.replace("-", "").isalnum() or not clip_id.replace("_", "").isalnum():
        _report_failure(transport, name, tid, ValueError("bad job or clip id"), log)
        return False
    jobdir = cache / job_id
    out_name = f"{clip_id}.{tid[:8]}.mp4"
    with Lease(transport, name, tid, float(task.get("lease_seconds") or 90), beat_seconds) as lease:
        try:
            jobdir.mkdir(parents=True, exist_ok=True)
            os.utime(jobdir)                                     # most recently used
            got = 0
            for fname, size in files.items():
                if fname == "edl.json":
                    dest = safe_join(jobdir, f"edl-{tid[:8]}.json")
                elif fname.startswith("media/"):
                    dest = safe_join(jobdir / "media", fname[len("media/"):])
                elif fname.startswith("asset/"):
                    dest = safe_join(jobdir / "out", fname[len("asset/"):])
                else:
                    raise ValueError(f"unexpected file {fname!r}")
                if lease.lost.is_set():
                    raise RuntimeError("the server gave this task up")
                if not (dest.is_file() and dest.stat().st_size == size and fname != "edl.json"):       # cached by name and size
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    transport.download(f"/api/worker/tasks/{tid}/files/{fname}?worker={name}", dest)
                    got += size
            log(f"task {tid[:8]}: rendering {clip_id} of job {job_id} ({got / 1e6:.1f} MB fetched, rest cached)")
            from .edl import load_edl
            edl = load_edl(jobdir / f"edl-{tid[:8]}.json")
            if render_fn is None:
                from .pipeline import render_clip as render_fn
            (jobdir / "out").mkdir(parents=True, exist_ok=True)
            mp4, compiled = render_fn(edl, clip_id, jobdir / "out" / out_name, workdir=jobdir / "work", media_root=jobdir / "media",
                                      gsap_js=os.environ.get("CLIPCROW_GSAP") or "vendor/gsap.min.js", asset_root=jobdir / "out")
            warnings = [str(w)[:300] for w in (getattr(compiled, "warnings", None) or [])][:20]
            if lease.lost.is_set():
                log(f"task {tid[:8]}: the server no longer wants this clip; dropped")
                return False
            transport.upload(f"/api/worker/tasks/{tid}/uploads/clip.mp4?worker={name}", Path(mp4))
        except Exception as e:
            _report_failure(transport, name, tid, e, log)
            return False
        finally:
            for f in (jobdir / "out").glob(f"{clip_id}.{tid[:8]}.*") if (jobdir / "out").is_dir() else []:
                f.unlink(missing_ok=True)
            (jobdir / f"edl-{tid[:8]}.json").unlink(missing_ok=True)
            evict_cache(cache, int(cache_gb * 1e9), keep=jobdir)
    try:
        ok = transport.post(f"/api/worker/tasks/{tid}/result", {"worker": name, "warnings": warnings}, timeout=120).get("ok")
    except Exception as e:
        log(f"task {tid[:8]}: could not deliver the result ({type(e).__name__})")
        return False
    log(f"task {tid[:8]}: {clip_id} rendered in {time.monotonic() - started:.0f} s" + ("" if ok else " (the server had already moved on)"))
    return bool(ok)


def main(server: str, name: str, model: str, *, render: bool = True, cache_gb: float = 20.0) -> int:
    token = os.environ.get("CLIPCROW_WORKER_TOKEN", "")
    if len(token) < 16:
        print("set CLIPCROW_WORKER_TOKEN to the same secret the server has (at least 16 characters)", file=sys.stderr)
        return 2
    transport = HttpTransport(server, token)
    stop = threading.Event()
    can_render = False
    if render:
        can_render, why = render_ready()
        print("rendering: " + ("ready, this worker will also render clips" if can_render else f"off ({why}); transcription only"), file=sys.stderr)
    print(f"worker {name!r} serving {server} with model {model}; Ctrl-C to stop", file=sys.stderr)
    try:
        run_worker(transport, name, model=model, stop=stop.is_set, log=lambda m: print(m, file=sys.stderr, flush=True),
                   can_render=can_render, cache_gb=cache_gb)
    except KeyboardInterrupt:
        stop.set()
        print("stopped", file=sys.stderr)
    return 0
