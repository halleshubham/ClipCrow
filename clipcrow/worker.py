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
import re
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

    def put_part(self, url: str, src: Path, offset: int, length: int, *, timeout: float = 60.0) -> str:
        """PUT ``length`` bytes of ``src`` from ``offset`` to a presigned URL of the bucket; returns the part's ETag."""
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


    def put_part(self, url, src, offset, length, *, timeout=60.0):
        """A presigned URL carries its own signature: the worker token must NOT be sent there (it is another host)."""
        if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise ValueError("the storage address is not https")
        try:
            with open(src, "rb") as f:
                f.seek(offset)
                req = urllib.request.Request(url, data=_Slice(f, length), method="PUT")
                req.add_header("Content-Type", "application/octet-stream")
                req.add_header("Content-Length", str(length))
                req.add_header("User-Agent", "clipcrow-worker")
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    r.read()
                    etag = r.headers.get("ETag", "")
        except urllib.error.HTTPError as e:
            raise WorkerHttpError(e.code, f"{e.code} from the file storage") from None
        if not etag:
            raise WorkerHttpError(0, "the file storage did not confirm the part")
        return etag


class _Slice:
    """A read-only view of the next ``length`` bytes of an open file, so a part streams from disk and is never held in memory."""

    def __init__(self, f, length: int):
        self._f, self._left = f, length

    def read(self, n: int = -1) -> bytes:
        n = self._left if n is None or n < 0 else min(n, self._left)
        data = self._f.read(n)
        self._left -= len(data)
        return data


class WorkerHttpError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


PART_TRIES = 3
THROUGH_SERVER_MAX = 90 * 1024 * 1024       # the proxy in front of the server refuses a request of about 100 MB


def send_clip(transport: Transport, name: str, tid: str, mp4: Path, log: Callable[[str], None] = lambda _m: None) -> None:
    """Hand the finished clip to the server. Preferred: straight to the bucket in parts (no proxy size limit); the server answers 404 when
    it has no bucket, and then (or when an older server does not know the route) the clip goes through the server as before."""
    size = mp4.stat().st_size
    try:
        plan = transport.post(f"/api/worker/tasks/{tid}/direct-upload", {"worker": name, "size": size}, timeout=60)
    except WorkerHttpError as e:
        if e.status in (404, 405, 422):
            plan = None
        else:
            raise
    if not plan:
        transport.upload(f"/api/worker/tasks/{tid}/uploads/clip.mp4?worker={name}", mp4)
        return
    log(f"task {tid[:8]}: sending {size / 1e6:.0f} MB straight to the file storage ({len(plan['parts'])} part(s))")
    done, offset = [], 0
    try:
        for part in plan["parts"]:
            length = int(part["length"])
            for attempt in range(PART_TRIES):
                try:
                    started = time.monotonic()
                    done.append({"n": int(part["n"]), "etag": transport.put_part(part["url"], mp4, offset, length)})
                    log(f"task {tid[:8]}:   part {part['n']}/{len(plan['parts'])} sent ({length / 1e6:.0f} MB in {time.monotonic() - started:.0f} s)")
                    break
                except (WorkerHttpError, urllib.error.URLError, OSError, TimeoutError) as e:
                    log(f"task {tid[:8]}:   part {part['n']} attempt {attempt + 1} failed ({type(e).__name__})")
                    if attempt == PART_TRIES - 1:
                        raise
                    time.sleep(2 * (attempt + 1))
            offset += length
        transport.post(f"/api/worker/tasks/{tid}/direct-done", {"worker": name, "parts": done}, timeout=300)
        return
    except (WorkerHttpError, urllib.error.URLError, OSError, TimeoutError) as e:
        if size > THROUGH_SERVER_MAX:
            raise                                                # too big for the proxy in front of the server: report the failure, the server renders it
        log(f"task {tid[:8]}: the file storage did not take it ({type(e).__name__}); sending through the server instead")
    transport.upload(f"/api/worker/tasks/{tid}/uploads/clip.mp4?worker={name}", mp4)


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


def capacity(label: str, slots: int = 1) -> int:
    """A rough number for "how much can this machine do", sent to the server only to decide who is offered a task first: a CUDA GPU outranks a CPU,
    and each extra render slot adds a little."""
    base = 100 if "cuda" in (label or "").lower() else 10
    return min(1000, base + 5 * max(0, slots - 1))


def run_worker(transport: Transport, name: str, *, model: str = "large-v3", transcribe: Optional[Callable] = None,
               label: str = "", stop: Callable[[], bool] = lambda: False, log: Callable[[str], None] = print,
               max_tasks: Optional[int] = None, poll_wait: float = POLL_WAIT, can_render: bool = False,
               render_fn: Optional[Callable] = None, cache: Optional[Path] = None, cache_gb: float = 20.0,
               pin: Optional[str] = None, can_speakers: bool = False, speakers_fn: Optional[Callable] = None, render_only: bool = False,
               can_framing: bool = False, framing_fn: Optional[Callable] = None,
               power: Optional[int] = None, pool: str = "home") -> int:
    """Poll, do the task, upload; until ``stop()`` or ``max_tasks`` tasks. Returns the number of tasks finished."""
    if transcribe is None and not render_only:
        transcribe, label = default_transcribe(model)
    if pin is None:
        from .render import HYPERFRAMES_VERSION as pin
    can = (["render"] if render_only else ["transcribe"] + (["render"] if can_render else []) + (["speakers"] if can_speakers else []) + (["framing"] if can_framing else []))
    if power is None:
        power = capacity(label)
    done, failures = 0, 0
    while not stop() and (max_tasks is None or done < max_tasks):
        try:
            reply = transport.post("/api/worker/poll", {"worker": name, "wait": poll_wait, "device": label[:60], "model": model[:40],
                                                         "can": can, "pin": pin, "direct": True, "power": power, "pool": pool}, timeout=poll_wait + 20)
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
        elif task.get("kind") == "framing":
            ok = handle_framing(transport, name, task, log, cache=cache or cache_root(), framing_fn=framing_fn, cache_gb=cache_gb)
        elif task.get("kind") == "speakers":
            ok = handle_speakers(transport, name, task, log, cache=cache or cache_root(), speakers_fn=speakers_fn, cache_gb=cache_gb)
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
                body = {"worker": name, "language": tr.language, "engine": getattr(tr, "engine", "") or "",
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
    log(f"task {tid[:8]}: took a render of {clip_id} for job {job_id}: {sum(int(v) for v in files.values()) / 1e6:.0f} MB of files (fetching what is not cached)")
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
                    if size > 5_000_000:
                        log(f"task {tid[:8]}:   fetched {fname} ({size / 1e6:.0f} MB)")
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
            send_clip(transport, name, tid, Path(mp4), log)
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


def speakers_ready() -> tuple[bool, str]:
    """Can this machine find out who is who in a video? It needs the face model, OpenCV's YuNet and torch with transformers (the voice model downloads on first use)."""
    try:
        import cv2
        import torch  # noqa: F401
        import transformers  # noqa: F401
        from .seats import FACE_MODEL
    except ImportError as e:
        return False, f"{e.name or 'a package'} is missing"
    if not hasattr(cv2, "FaceDetectorYN") or not FACE_MODEL.is_file():
        return False, "OpenCV has no YuNet face detector"
    import torch
    return True, "voices on the GPU" if torch.cuda.is_available() else "voices on the CPU: this image's torch cannot see a GPU"


def framing_ready() -> tuple[bool, str]:
    """Can this machine frame clips (find the faces and plan the crop)? It needs OpenCV and its bundled face cascade; no GPU."""
    try:
        import cv2
    except ImportError as e:
        return False, f"{e.name or 'a package'} is missing"
    if not Path(cv2.data.haarcascades).is_dir():
        return False, "OpenCV has no face cascade"
    return True, f"faces on {os.cpu_count() or 1} CPU core(s)"


def default_framing(video: Path, clips: list[dict], multi: str, graphics: bool, say: Callable[[str], None], check: Callable[[], None]) -> dict:
    """The answer to a framing task: ``{"clips": [...]}`` with the framing of each clip (``reframe.plan_framing``, several clips at once)."""
    from .framing import plan_clips
    spans = [(str(c["id"]), float(c["start"]), float(c["end"])) for c in clips]

    def done(cid: str, took: float) -> None:
        say(f"{cid} framed in {took:.0f} s")
        check()
    got = plan_clips(str(video), spans, multi=multi, graphics=graphics, done=done)
    out = []
    for cid, (f, took) in got.items():
        out.append({"id": cid, "framing": f.framing, "keyframes": [[round(t, 3), round(cx, 4)] for t, cx in f.keyframes], "coverage": round(float(f.coverage), 4),
                    "split": [[x, y] for x, y in f.split], "people": int(f.people), "fit_spans": [[a, b] for a, b in f.fit_spans],
                    "face_zone": list(f.face_zone) if f.face_zone else None, "seconds": round(took, 1)})
    return {"clips": out}


def handle_framing(transport: Transport, name: str, task: dict, log: Callable[[str], None], *, cache: Path,
                   framing_fn: Optional[Callable] = None, cache_gb: float = 20.0, beat_seconds: Optional[float] = None) -> bool:
    """A framing task: fetch the video (cached per job, under the name a render of this job uses), frame each clip, send back JSON only."""
    tid, meta, files = task["id"], task.get("meta") or {}, task.get("files") or {}
    started = time.monotonic()
    job_id = str(meta.get("job_id", ""))
    clips = meta.get("clips")
    if (not job_id.replace("-", "").isalnum() or len(files) != 1 or not isinstance(clips, list) or not 0 < len(clips) <= 60
            or not all(isinstance(c, dict) and re.fullmatch(r"c\d{1,3}", str(c.get("id", ""))) and isinstance(c.get("start"), (int, float)) and isinstance(c.get("end"), (int, float))
                       and 0 <= c["start"] < c["end"] <= 86400 for c in clips)):
        _report_failure(transport, name, tid, ValueError("bad job id, manifest or clip list"), log)
        return False
    jobdir = cache / job_id
    with Lease(transport, name, tid, float(task.get("lease_seconds") or 90), beat_seconds) as lease:
        try:
            (fname, size), = files.items()
            if not fname.startswith("media/"):
                raise ValueError(f"unexpected file {fname!r}")
            jobdir.mkdir(parents=True, exist_ok=True)
            os.utime(jobdir)
            dest = safe_join(jobdir / "media", fname[len("media/"):])
            if not (dest.is_file() and dest.stat().st_size == size):
                dest.parent.mkdir(parents=True, exist_ok=True)
                log(f"task {tid[:8]}: took framing of {len(clips)} clip(s) for job {job_id}: fetching the video ({size / 1e6:.0f} MB)")
                transport.download(f"/api/worker/tasks/{tid}/files/{fname}?worker={name}", dest)
            else:
                log(f"task {tid[:8]}: took framing of {len(clips)} clip(s) for job {job_id}: the video is cached")

            def check() -> None:
                if lease.lost.is_set():
                    raise RuntimeError("the server gave this task up")
            answer = (framing_fn or default_framing)(dest, clips, str(meta.get("multi") or "switch"), bool(meta.get("graphics")),
                                                       lambda m: log(f"task {tid[:8]}: {m}"), check)
        except Exception as e:
            _report_failure(transport, name, tid, e, log)
            return False
        finally:
            evict_cache(cache, int(cache_gb * 1e9), keep=jobdir)
    if lease.lost.is_set():
        log(f"task {tid[:8]}: the server no longer wants this result; dropped")
        return False
    try:
        ok = transport.post(f"/api/worker/tasks/{tid}/result", {"worker": name, "framing": answer}, timeout=120).get("ok")
    except Exception as e:
        log(f"task {tid[:8]}: could not deliver the result ({type(e).__name__})")
        return False
    log(f"task {tid[:8]}: {len(clips)} clip(s) framed in {time.monotonic() - started:.0f} s" + ("" if ok else " (the server had already moved on)"))
    return bool(ok)


def default_speakers(video: Path, say: Callable[[str], None], check: Callable[[], None]) -> dict:
    """The answer to a speakers task: ``SpeakerMap`` as JSON (no cache, no picture: the server makes its own)."""
    from .seats import analyse
    sm, _ = analyse(video, say=say, check=check)
    if sm is None:
        raise ValueError("this video is not one the people search applies to")
    data = sm.to_json()
    return {k: data[k] for k in ("duration", "seats", "turns", "voice_seat", "lift", "picture_t")}


def handle_speakers(transport: Transport, name: str, task: dict, log: Callable[[str], None], *, cache: Path,
                    speakers_fn: Optional[Callable] = None, cache_gb: float = 20.0, beat_seconds: Optional[float] = None) -> bool:
    """A speakers task: fetch the video (cached per job, under the name a render of this job uses), find the people and the voices, send back JSON only."""
    tid, meta, files = task["id"], task.get("meta") or {}, task.get("files") or {}
    started = time.monotonic()
    job_id = str(meta.get("job_id", ""))
    if not job_id.replace("-", "").isalnum() or len(files) != 1:
        _report_failure(transport, name, tid, ValueError("bad job id or manifest"), log)
        return False
    jobdir = cache / job_id
    with Lease(transport, name, tid, float(task.get("lease_seconds") or 90), beat_seconds) as lease:
        try:
            (fname, size), = files.items()
            if not fname.startswith("media/"):
                raise ValueError(f"unexpected file {fname!r}")
            jobdir.mkdir(parents=True, exist_ok=True)
            os.utime(jobdir)
            dest = safe_join(jobdir / "media", fname[len("media/"):])
            if not (dest.is_file() and dest.stat().st_size == size):
                dest.parent.mkdir(parents=True, exist_ok=True)
                log(f"task {tid[:8]}: took a who-is-who search for job {job_id}: fetching the video ({size / 1e6:.0f} MB)")
                transport.download(f"/api/worker/tasks/{tid}/files/{fname}?worker={name}", dest)
            else:
                log(f"task {tid[:8]}: took a who-is-who search for job {job_id}: the video is cached")

            def check() -> None:
                if lease.lost.is_set():
                    raise RuntimeError("the server gave this task up")
            answer = (speakers_fn or default_speakers)(dest, lambda m: log(f"task {tid[:8]}: {m}"), check)
        except Exception as e:
            _report_failure(transport, name, tid, e, log)
            return False
        finally:
            evict_cache(cache, int(cache_gb * 1e9), keep=jobdir)
    if lease.lost.is_set():
        log(f"task {tid[:8]}: the server no longer wants this result; dropped")
        return False
    try:
        ok = transport.post(f"/api/worker/tasks/{tid}/result", {"worker": name, "speakers": answer}, timeout=120).get("ok")
    except Exception as e:
        log(f"task {tid[:8]}: could not deliver the result ({type(e).__name__})")
        return False
    log(f"task {tid[:8]}: who is who found in {time.monotonic() - started:.0f} s" + ("" if ok else " (the server had already moved on)"))
    return bool(ok)


def main(server: str, name: str, model: str, *, render: bool = True, cache_gb: float = 20.0, parallel: int = 1, pool: str = "home") -> int:
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
    can_framing, why = framing_ready()
    print("framing: " + (f"ready, this worker will also frame clips ({why})" if can_framing else f"off ({why})"), file=sys.stderr)
    can_speakers, why = speakers_ready()
    print("who is who: " + (f"ready, this worker will also tell the people in a video apart ({why})" if can_speakers else f"off ({why})"), file=sys.stderr)
    print(f"worker {name!r} serving {server} with model {model}; Ctrl-C to stop", file=sys.stderr)
    parallel = max(1, min(8, int(parallel)))
    log = lambda m: print(m, file=sys.stderr, flush=True)
    if can_render and parallel > 1:
        print(f"parallel: {parallel - 1} extra render slot(s); speech recognition stays one at a time", file=sys.stderr)
        for i in range(2, parallel + 1):                         # extra slots only render; the GPU model is loaded once, by the first
            threading.Thread(target=run_worker, args=(transport, f"{name}.r{i}"), daemon=True,
                             kwargs=dict(model=model, stop=stop.is_set, log=log, can_render=True, cache_gb=cache_gb, render_only=True, pool=pool,
                                         label="render slot", power=capacity("cuda", parallel))).start()
    try:
        run_worker(transport, name, model=model, stop=stop.is_set, log=log, can_render=can_render, cache_gb=cache_gb,
                   can_speakers=can_speakers, can_framing=can_framing, pool=pool)
    except KeyboardInterrupt:
        stop.set()
        print("stopped", file=sys.stderr)
    return 0
