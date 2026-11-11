"""The HTTP API and the static web UI.

Security model (v1): built for one trusted user or a small team on a private network. Bind to 127.0.0.1
(the default) or put it behind a reverse proxy with TLS. Set CLIPCROW_TOKEN to require a login (cookie,
HttpOnly + SameSite=Strict). Every id in a URL is checked against a strict pattern and resolved inside the
job's own directory; uploads are size-capped, extension-checked and probed with ffprobe; error messages shown
to the browser never contain server internals; responses carry a strict Content-Security-Policy.
"""
from __future__ import annotations

import asyncio
import hmac
import re
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from ..costs import estimate_cost, estimate_total
from ..edl import load_edl
from ..planner.source import probe_source
from .jobs import VIDEO_SUFFIXES, Job, JobStore, Params
from .runner import JobRunner, Pipeline

STATIC = Path(__file__).parent / "static"
COOKIE = "clipcrow_token"
LANGUAGES = [{"code": "", "name": "Auto-detect"}, {"code": "mr", "name": "Marathi"}, {"code": "hi", "name": "Hindi"},
             {"code": "en", "name": "English"}]
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; "
       "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


class Login(BaseModel):
    token: str


def _opt_int(v: Optional[str]) -> Optional[int]:
    return int(v) if v not in (None, "") else None


def _opt_float(v: Optional[str], default: float) -> float:
    return float(v) if v not in (None, "") else default


def create_app(data_dir: str | Path, pipeline: Pipeline, *, token: str | None = None, max_upload_mb: int = 4096,
               demo: bool = False, start_runner: bool = True) -> FastAPI:
    store = JobStore(data_dir)
    runner = JobRunner(store, pipeline)
    if start_runner:
        runner.recover()
    app = FastAPI(title="ClipCrow", docs_url=None, redoc_url=None)
    app.state.store, app.state.runner = store, runner
    limit = max_upload_mb * 1024 * 1024

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    def authed(request: Request) -> None:
        if not token:
            return
        given = request.cookies.get(COOKIE, "")
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            given = given or header[7:]
        if not hmac.compare_digest(given.encode(), token.encode()):
            raise HTTPException(401, "Sign in first.")

    def job_or_404(job_id: str) -> Job:
        try:
            return store.get(job_id)
        except KeyError:
            raise HTTPException(404, "No such job.")

    def view(job: Job) -> dict:
        d = job.model_dump()
        d["queue_position"] = runner.queue_position(job.id) if job.status == "queued" else 0
        return d

    # --- config and login ---------------------------------------------------------------
    @app.get("/api/config")
    def config(request: Request) -> dict:
        signed_in = True
        try:
            authed(request)
        except HTTPException:
            signed_in = False
        return {"demo": demo, "languages": LANGUAGES, "max_upload_mb": max_upload_mb,
                "needs_login": bool(token), "signed_in": signed_in, "ready": pipeline.check_ready()}

    @app.post("/api/login")
    def login(body: Login, request: Request) -> JSONResponse:
        if not token or not hmac.compare_digest(body.token.encode(), token.encode()):
            raise HTTPException(401, "That token is not right.")
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=30 * 86400,
                        secure=request.url.scheme == "https")
        return resp

    # --- jobs -----------------------------------------------------------------------------
    @app.post("/api/jobs", dependencies=[Depends(authed)], status_code=201)
    async def create_job(request: Request, file: UploadFile = File(...), rights: str = Form(""),
                         language: Optional[str] = Form(None), clips: Optional[str] = Form(None),
                         min_clip: Optional[str] = Form(None), max_clip: Optional[str] = Form(None),
                         spread: Optional[str] = Form(None), speaker_name: Optional[str] = Form(None),
                         speaker_title: Optional[str] = Form(""), cutout: Optional[str] = Form(None)) -> dict:
        if rights.lower() not in ("1", "true", "on", "yes"):
            raise HTTPException(400, "Confirm that you own this video or have the rights to process it.")
        declared = int(request.headers.get("content-length") or 0)
        if declared > limit + 1_000_000:
            raise HTTPException(413, f"That file is larger than the {max_upload_mb} MB limit.")
        try:
            params = Params(language=language or None, clips=_opt_int(clips), min_clip=_opt_int(min_clip) or 15,
                            max_clip=_opt_int(max_clip) or 60, spread=_opt_float(spread, 0.5),
                            speaker_name=(speaker_name or "").strip() or None,
                            speaker_title=(speaker_title or "").strip(),
                            cutout=str(cutout).lower() in ("1", "true", "on", "yes"))
        except (ValidationError, ValueError) as e:
            raise HTTPException(422, f"Check the options: {str(e).splitlines()[0][:160]}")
        if params.min_clip >= params.max_clip:
            raise HTTPException(422, "The shortest clip must be shorter than the longest.")
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in VIDEO_SUFFIXES:
            raise HTTPException(400, "Upload an MP4, MOV, MKV, WebM or M4V video.")
        problem = pipeline.check_ready()
        if problem:
            raise HTTPException(503, problem)

        job = store.create(Path(file.filename or "video").name, params, demo=demo)
        dest = store.dir(job.id) / f"source{suffix}"
        try:
            size = 0
            with dest.open("wb") as f:
                while chunk := await file.read(1 << 20):
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, f"That file is larger than the {max_upload_mb} MB limit.")
                    f.write(chunk)
            try:
                info = await asyncio.to_thread(probe_source, dest)
            except ValueError:
                raise HTTPException(400, "That file could not be read as a video.")
            if info.duration < params.min_clip + 5:
                raise HTTPException(400, f"This video is {info.duration:.0f} s long; clips of at least "
                                         f"{params.min_clip} s need a longer video.")
        except HTTPException:
            store.delete(job.id)
            raise
        job.source = {"duration": info.duration, "width": info.width, "height": info.height}
        store.save(job)
        runner.submit(job.id)
        return view(store.get(job.id))

    @app.get("/api/jobs", dependencies=[Depends(authed)])
    def list_jobs() -> list[dict]:
        return [view(j) for j in store.list()]

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(authed)])
    def get_job(job_id: str) -> dict:
        return view(job_or_404(job_id))

    @app.post("/api/jobs/{job_id}/cancel", dependencies=[Depends(authed)])
    def cancel_job(job_id: str) -> dict:
        job_or_404(job_id)
        runner.cancel(job_id)
        return view(store.get(job_id))

    @app.delete("/api/jobs/{job_id}", dependencies=[Depends(authed)], status_code=204)
    def delete_job(job_id: str) -> None:
        job = job_or_404(job_id)
        if job.status in ("queued", "running"):
            raise HTTPException(409, "Cancel the job before deleting it.")
        store.delete(job_id)

    def clip_file(job_id: str, clip_id: str) -> Path:
        job_or_404(job_id)
        try:
            path = store.clip_path(job_id, clip_id)
        except KeyError:
            raise HTTPException(404, "No such clip.")
        if not path.is_file():
            raise HTTPException(404, "This clip is not ready yet.")
        return path

    @app.get("/api/jobs/{job_id}/clips/{clip_id}/video", dependencies=[Depends(authed)])
    def clip_video(job_id: str, clip_id: str) -> FileResponse:
        return FileResponse(clip_file(job_id, clip_id), media_type="video/mp4")

    @app.get("/api/jobs/{job_id}/clips/{clip_id}/download", dependencies=[Depends(authed)])
    def clip_download(job_id: str, clip_id: str) -> FileResponse:
        job = job_or_404(job_id)
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(job.name).stem).strip("._") or "clip"
        return FileResponse(clip_file(job_id, clip_id), media_type="video/mp4", filename=f"{stem[:60]}-{clip_id}.mp4")

    @app.get("/api/jobs/{job_id}/usage", dependencies=[Depends(authed)])
    def usage(job_id: str) -> dict:
        job_or_404(job_id)
        path = store.dir(job_id) / "out" / "edl.json"
        if not path.is_file():
            raise HTTPException(404, "The plan is not ready yet.")
        edl = load_edl(path)
        def order(c):          # score first, then the per-window calls in window order (threads finish in any order)
            kind, _, n = c.purpose.partition(":")
            return (0 if kind == "score" else 1, kind, int(n) if n.isdigit() else 0)
        calls = [{"purpose": c.purpose, "model": c.model, "input_tokens": c.input_tokens,
                  "output_tokens": c.output_tokens,
                  "est_cost_usd": estimate_cost(c.model, c.input_tokens, c.output_tokens)}
                 for c in sorted(edl.ai.calls if edl.ai else [], key=order)]
        return {
            "calls": calls,
            "input_tokens": sum(c["input_tokens"] for c in calls),
            "output_tokens": sum(c["output_tokens"] for c in calls),
            "est_cost_usd": estimate_total(edl.ai.calls) if edl.ai else None,
            "assets": [{"id": a.id, "origin": a.origin, "ai": a.ai, "tool": a.tool} for a in edl.assets],
            "disclosure_recommended": any(a.ai == "generated" for a in edl.assets),
            "clips": [{"id": c.id, "title": c.title, "decided": c.ai.decided if c.ai else []} for c in edl.clips],
        }

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="ui")
    return app
