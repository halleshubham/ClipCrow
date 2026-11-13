"""``clipcrow worker``: run speech recognition for a ClipCrow server on THIS machine (typically one with an NVIDIA GPU).

The worker only ever makes outgoing HTTPS requests to the server: it asks for a task, downloads the task's audio, transcribes it
here and uploads the words. No port is opened on this machine. It authenticates with the shared secret in the environment variable
CLIPCROW_WORKER_TOKEN (never a command-line argument, which would end up in shell history and process lists).

The server treats everything this program sends as untrusted input and falls back to its own recognition when a worker is slow or
gone, so stopping this program at any time is safe.
"""
from __future__ import annotations

import json
import os
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

    def post(self, path: str, body: dict, *, timeout: float = 60.0) -> dict: ...

    def download(self, path: str, dest: Path, *, timeout: float = 600.0) -> None: ...


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


def run_worker(transport: Transport, name: str, *, model: str = "large-v3", transcribe: Optional[Callable] = None,
               label: str = "", stop: Callable[[], bool] = lambda: False, log: Callable[[str], None] = print,
               max_tasks: Optional[int] = None, poll_wait: float = POLL_WAIT) -> int:
    """Poll, transcribe, upload; until ``stop()`` or ``max_tasks`` tasks. Returns the number of tasks finished."""
    if transcribe is None:
        transcribe, label = default_transcribe(model)
    done, failures = 0, 0
    while not stop() and (max_tasks is None or done < max_tasks):
        try:
            reply = transport.post("/api/worker/poll", {"worker": name, "wait": poll_wait, "device": label[:60], "model": model[:40]},
                                   timeout=poll_wait + 20)
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
        if handle(transport, name, task, transcribe, log):
            done += 1
    return done


def handle(transport: Transport, name: str, task: dict, transcribe: Callable, log: Callable[[str], None],
           beat_seconds: Optional[float] = None) -> bool:
    """One task. Returns True when a result was delivered."""
    tid = task["id"]
    lease = float(task.get("lease_seconds") or 90)
    lost = threading.Event()
    finished = threading.Event()

    def beat() -> None:
        while not finished.wait(beat_seconds if beat_seconds is not None else max(5.0, lease / 3)):
            try:
                if not transport.post(f"/api/worker/tasks/{tid}/heartbeat", {"worker": name}, timeout=30).get("ok"):
                    lost.set()                                   # the server gave the task to someone else or gave up on it
                    return
            except Exception:
                pass                                              # a missed beat is fine; the lease is longer than three of them

    threading.Thread(target=beat, daemon=True).start()
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="clipcrow-worker-") as tmp:
            audio = Path(tmp) / "audio.m4a"
            transport.download(f"/api/worker/tasks/{tid}/audio?worker={name}", audio)
            log(f"task {tid[:8]}: transcribing {audio.stat().st_size / 1e6:.1f} MB of audio")
            tr = transcribe(str(audio), task.get("language"))
            body = {"worker": name, "language": tr.language,
                    "words": [{"w": w.w, "s": round(float(w.s), 3), "e": round(float(w.e), 3)} for w in tr.words]}
    except Exception as e:                                       # tell the server so the job falls back at once, not after the lease
        log(f"task {tid[:8]} failed: {type(e).__name__}: {str(e)[:150]}")
        finished.set()
        try:
            transport.post(f"/api/worker/tasks/{tid}/result", {"worker": name, "error": f"{type(e).__name__}: {str(e)[:200]}"}, timeout=30)
        except Exception:
            pass
        return False
    finished.set()
    if lost.is_set():
        log(f"task {tid[:8]}: the server no longer wants this result; dropped")
        return False
    try:
        ok = transport.post(f"/api/worker/tasks/{tid}/result", body, timeout=120).get("ok")
    except Exception as e:
        log(f"task {tid[:8]}: could not deliver the result ({type(e).__name__})")
        return False
    log(f"task {tid[:8]}: {len(body['words'])} words in {time.monotonic() - started:.0f} s" + ("" if ok else " (the server had already moved on)"))
    return bool(ok)


def main(server: str, name: str, model: str) -> int:
    token = os.environ.get("CLIPCROW_WORKER_TOKEN", "")
    if len(token) < 16:
        print("set CLIPCROW_WORKER_TOKEN to the same secret the server has (at least 16 characters)", file=sys.stderr)
        return 2
    transport = HttpTransport(server, token)
    stop = threading.Event()
    print(f"worker {name!r} serving {server} with model {model}; Ctrl-C to stop", file=sys.stderr)
    try:
        run_worker(transport, name, model=model, stop=stop.is_set, log=lambda m: print(m, file=sys.stderr, flush=True))
    except KeyboardInterrupt:
        stop.set()
        print("stopped", file=sys.stderr)
    return 0
