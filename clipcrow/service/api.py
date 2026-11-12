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
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from ..assets.urlguard import UnsafeURL, assert_public_url
from ..costs import estimate_cost, estimate_total
from ..edl import load_edl
from ..planner.source import probe_source
from .accounts import AccountError, Accounts, User
from .editor import ClipEdit, clip_view
from .jobs import VIDEO_SUFFIXES, Job, JobStore, Params
from .plans import Catalog, load_catalog
from .retention import Sweeper, env_days, expires_at
from .runner import JobRunner, Pipeline

STATIC = Path(__file__).parent / "static"
COOKIE = "clipcrow_token"
SESSION_COOKIE = "clipcrow_session"
LANGUAGES = [{"code": "", "name": "Auto-detect"}, {"code": "mr", "name": "Marathi"}, {"code": "hi", "name": "Hindi"},
             {"code": "en", "name": "English"}]
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; "
       "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


class Login(BaseModel):
    token: str


class Credentials(BaseModel):
    email: str
    password: str


class Password(BaseModel):
    password: str


def _opt_int(v: Optional[str]) -> Optional[int]:
    return int(v) if v not in (None, "") else None


def _opt_float(v: Optional[str], default: float) -> float:
    return float(v) if v not in (None, "") else default


def create_app(data_dir: str | Path, pipeline: Pipeline, *, token: str | None = None, max_upload_mb: int = 4096,
               demo: bool = False, start_runner: bool = True, accounts: Accounts | None = None,
               catalog: Catalog | None = None, allow_signup: bool = True) -> FastAPI:
    """``accounts``: multi-user mode (sign-up, per-user jobs, minutes metered against the plan). Without it the app is
    single-user, optionally behind ``token``."""
    store = JobStore(data_dir)
    catalog = catalog or load_catalog()
    runner = JobRunner(store, pipeline)

    def finish(job: Job) -> None:
        """A job that failed or was cancelled gives its minutes back."""
        if accounts and job.owner and job.reserved_minutes and job.status in ("failed", "cancelled"):
            accounts.refund(job.owner, job.id)
    runner.on_finish = finish
    if start_runner:
        runner.recover()
    app = FastAPI(title="ClipCrow", docs_url=None, redoc_url=None)
    app.state.store, app.state.runner, app.state.accounts, app.state.catalog = store, runner, accounts, catalog
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

    def effective_plan(user: User):
        """A paid plan that has run out (and not been renewed by a webhook yet) counts as free."""
        plan = catalog.plan(user.plan)
        if plan.id != "free" and user.plan_until and user.plan_until < datetime.now(timezone.utc).isoformat(timespec="seconds"):
            return catalog.plan("free")
        return plan

    def authed(request: Request) -> Optional[User]:
        if accounts:
            user = accounts.user_for_session(request.cookies.get(SESSION_COOKIE, ""))
            if user is None:
                raise HTTPException(401, "Sign in first.")
            request.state.user = user
            return user
        request.state.user = None
        if not token:
            return None
        given = request.cookies.get(COOKIE, "")
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            given = given or header[7:]
        if not hmac.compare_digest(given.encode(), token.encode()):
            raise HTTPException(401, "Sign in first.")
        return None

    def job_or_404(job_id: str, request: Request) -> Job:
        try:
            job = store.get(job_id)
        except KeyError:
            raise HTTPException(404, "No such job.")
        user = getattr(request.state, "user", None)
        if accounts and (user is None or job.owner != user.id):      # someone else's job is simply not there
            raise HTTPException(404, "No such job.")
        return job

    def days_for(job: Job) -> tuple[int, int]:
        """(retention days, original-video days) for a job: its owner's plan, or the environment defaults."""
        if accounts and job.owner:
            try:
                p = effective_plan(accounts.get(job.owner))
                return p.retention_days, p.source_days
            except KeyError:
                return (0, 0)
        return env_days()

    if start_runner and (accounts or any(env_days())):
        app.state.sweeper = Sweeper(store, days_for)
        app.state.sweeper.start()

    def view(job: Job) -> dict:
        d = job.model_dump()
        d["queue_position"] = runner.queue_position(job.id) if job.status == "queued" else 0
        days = days_for(job)[0]
        d["expires_at"] = expires_at(job, days)
        return d

    # --- config and login ---------------------------------------------------------------
    @app.get("/api/config")
    def config(request: Request) -> dict:
        signed_in, email = True, None
        try:
            u = authed(request)
            email = u.email if u else None
        except HTTPException:
            signed_in = False
        return {"demo": demo, "languages": LANGUAGES, "max_upload_mb": max_upload_mb, "accounts": bool(accounts),
                "signup": bool(accounts) and allow_signup, "email": email,
                "needs_login": bool(token) or bool(accounts), "signed_in": signed_in, "ready": pipeline.check_ready(),
                "ai_images": bool(getattr(pipeline, "can_generate_images", False))}

    @app.post("/api/login")
    def login(body: Login, request: Request) -> JSONResponse:
        if not token or not hmac.compare_digest(body.token.encode(), token.encode()):
            raise HTTPException(401, "That token is not right.")
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=30 * 86400,
                        secure=request.url.scheme == "https")
        return resp

    # --- accounts -------------------------------------------------------------------------------
    def set_session(resp: JSONResponse, request: Request, user: User) -> JSONResponse:
        resp.set_cookie(SESSION_COOKIE, accounts.new_session(user.id), httponly=True, samesite="strict",
                        max_age=30 * 86400, secure=request.url.scheme == "https")
        return resp

    def need_accounts() -> Accounts:
        if not accounts:
            raise HTTPException(404, "Accounts are not turned on.")
        return accounts

    def me_view(user: User) -> dict:
        plan = effective_plan(user)
        used = accounts.used_this_month(user.id)
        return {"email": user.email, "plan": {"id": plan.id, "name": plan.name, "monthly_minutes": plan.monthly_minutes,
                                              "retention_days": plan.retention_days, "price_label": plan.price_label},
                "used_minutes": round(used, 1), "left_minutes": round(max(plan.monthly_minutes - used, 0) + user.credits, 1),
                "credit_minutes": round(user.credits, 1), "plan_until": user.plan_until, "subscribed": bool(user.subscription)}

    @app.post("/api/auth/register")
    def register(body: Credentials, request: Request) -> JSONResponse:
        need_accounts()
        if not allow_signup:
            raise HTTPException(403, "Sign-ups are closed on this server.")
        try:
            user = accounts.register(body.email, body.password)
        except AccountError as e:
            raise HTTPException(422, str(e))
        return set_session(JSONResponse({"ok": True}), request, user)

    @app.post("/api/auth/login")
    def account_login(body: Credentials, request: Request) -> JSONResponse:
        need_accounts()
        try:
            user = accounts.authenticate(body.email, body.password, who=request.client.host if request.client else "")
        except AccountError as e:
            raise HTTPException(429 if "Too many" in str(e) else 401, str(e))
        return set_session(JSONResponse({"ok": True}), request, user)

    @app.post("/api/auth/logout")
    def logout(request: Request) -> JSONResponse:
        need_accounts().end_session(request.cookies.get(SESSION_COOKIE, ""))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    @app.get("/api/me", dependencies=[Depends(authed)])
    def me(request: Request) -> dict:
        need_accounts()
        return me_view(request.state.user)

    @app.delete("/api/account", dependencies=[Depends(authed)])
    def delete_account(body: Password, request: Request) -> JSONResponse:
        user = request.state.user
        need_accounts()
        try:
            accounts.authenticate(user.email, body.password, who=request.client.host if request.client else "")
        except AccountError as e:
            raise HTTPException(401, "That password is not right.")
        if any(j.status in ("queued", "running") for j in store.list() if j.owner == user.id):
            raise HTTPException(409, "Cancel your running jobs first.")
        for j in store.list():
            if j.owner == user.id:
                store.delete(j.id)
        accounts.delete(user.id)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    # --- jobs -----------------------------------------------------------------------------
    @app.post("/api/jobs", dependencies=[Depends(authed)], status_code=201)
    async def create_job(request: Request, file: UploadFile = File(...), rights: str = Form(""),
                         language: Optional[str] = Form(None), clips: Optional[str] = Form(None),
                         min_clip: Optional[str] = Form(None), max_clip: Optional[str] = Form(None),
                         spread: Optional[str] = Form(None), speaker_name: Optional[str] = Form(None),
                         speaker_title: Optional[str] = Form(""), cutout: Optional[str] = Form(None),
                         multi: Optional[str] = Form(None), immersive: Optional[str] = Form(None),
                         explainer: Optional[str] = Form(None), links: Optional[str] = Form(None),
                         ai_images: Optional[str] = Form(None)) -> dict:
        if rights.lower() not in ("1", "true", "on", "yes"):
            raise HTTPException(400, "Confirm that you own this video or have the rights to process it.")
        declared = int(request.headers.get("content-length") or 0)
        if declared > limit + 1_000_000:
            raise HTTPException(413, f"That file is larger than the {max_upload_mb} MB limit.")
        link_list = [x.strip() for x in (links or "").splitlines() if x.strip()]
        want_images = str(ai_images).lower() in ("1", "true", "on", "yes")
        if len(link_list) > 6:
            raise HTTPException(422, "Use at most 6 links.")
        for u in link_list:
            try:
                assert_public_url(u)
            except UnsafeURL as e:
                raise HTTPException(422, f"Link {link_list.index(u) + 1}: {e}")
        if want_images and not getattr(pipeline, "can_generate_images", False):
            raise HTTPException(422, "AI image generation is not set up on this server.")
        try:
            params = Params(language=language or None, clips=_opt_int(clips), min_clip=_opt_int(min_clip) or 15,
                            max_clip=_opt_int(max_clip) or 60, spread=_opt_float(spread, 0.5),
                            speaker_name=(speaker_name or "").strip() or None,
                            speaker_title=(speaker_title or "").strip(),
                            cutout=str(cutout).lower() in ("1", "true", "on", "yes"),
                            immersive=str(immersive).lower() in ("1", "true", "on", "yes"),
                            explainer=str(explainer).lower() in ("1", "true", "on", "yes"), links=link_list,
                            ai_images=want_images,
                            multi=multi or "switch")
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

        user = request.state.user
        job = store.create(Path(file.filename or "video").name, params, demo=demo)
        if accounts:
            job.owner = user.id                              # from the first moment, so nobody else can see it
            store.save(job)
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
        if accounts:
            minutes = max(1, math.ceil(info.duration / 60))
            plan = effective_plan(user)
            left = accounts.reserve(user.id, job.id, minutes, plan.monthly_minutes)
            if left is None:
                store.delete(job.id)
                have = max(plan.monthly_minutes - accounts.used_this_month(user.id), 0) + accounts.get(user.id).credits
                raise HTTPException(402, f"This video is {minutes} min long and you have {have:.0f} min left on the {plan.name} plan this "
                                         f"month. Upgrade, buy minutes, or wait for the month to reset.")
            job.reserved_minutes = minutes
        store.save(job)
        runner.submit(job.id)
        return view(store.get(job.id))

    @app.get("/api/jobs", dependencies=[Depends(authed)])
    def list_jobs(request: Request) -> list[dict]:
        user = request.state.user
        return [view(j) for j in store.list() if not accounts or j.owner == user.id]

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(authed)])
    def get_job(job_id: str, request: Request) -> dict:
        return view(job_or_404(job_id, request))

    @app.post("/api/jobs/{job_id}/cancel", dependencies=[Depends(authed)])
    def cancel_job(job_id: str, request: Request) -> dict:
        job_or_404(job_id, request)
        runner.cancel(job_id)
        return view(store.get(job_id))

    @app.delete("/api/jobs/{job_id}", dependencies=[Depends(authed)], status_code=204)
    def delete_job(job_id: str, request: Request) -> None:
        job = job_or_404(job_id, request)
        if job.status in ("queued", "running"):
            raise HTTPException(409, "Cancel the job before deleting it.")
        store.delete(job_id)

    def clip_file(job_id: str, clip_id: str, request: Request) -> Path:
        job_or_404(job_id, request)
        try:
            path = store.clip_path(job_id, clip_id)
        except KeyError:
            raise HTTPException(404, "No such clip.")
        if not path.is_file():
            raise HTTPException(404, "This clip is not ready yet.")
        return path

    @app.get("/api/jobs/{job_id}/clips/{clip_id}/video", dependencies=[Depends(authed)])
    def clip_video(job_id: str, clip_id: str, request: Request) -> FileResponse:
        return FileResponse(clip_file(job_id, clip_id, request), media_type="video/mp4")

    @app.get("/api/jobs/{job_id}/clips/{clip_id}/download", dependencies=[Depends(authed)])
    def clip_download(job_id: str, clip_id: str, request: Request) -> FileResponse:
        job = job_or_404(job_id, request)
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(job.name).stem).strip("._") or "clip"
        return FileResponse(clip_file(job_id, clip_id, request), media_type="video/mp4", filename=f"{stem[:60]}-{clip_id}.mp4")

    # --- editing one clip ---------------------------------------------------------------------
    def editable(job_id: str, clip_id: str, request: Request) -> Job:
        job = job_or_404(job_id, request)
        if not any(c.id == clip_id for c in job.clips):
            raise HTTPException(404, "No such clip.")
        if job.status != "done":
            raise HTTPException(409, "Wait for the job to finish before editing its clips.")
        if job.source_removed or store.source_path(job_id) is None:
            raise HTTPException(409, "The original video was removed to save space, so this clip can no longer be edited. "
                                     "You can still download it.")
        return job

    @app.get("/api/jobs/{job_id}/clips/{clip_id}", dependencies=[Depends(authed)])
    def clip_get(job_id: str, clip_id: str, request: Request) -> dict:
        job = editable(job_id, clip_id, request)
        try:
            edl = load_edl(store.edl_path(job_id))
            view = clip_view(edl, clip_id, source_duration=float(job.source.get("duration", 0)))
        except (KeyError, OSError):
            raise HTTPException(404, "The plan for this clip is not on disk.")
        return {**view, "render": job.clip(clip_id).model_dump()}

    @app.put("/api/jobs/{job_id}/clips/{clip_id}", dependencies=[Depends(authed)])
    def clip_put(job_id: str, clip_id: str, edit: ClipEdit, request: Request) -> dict:
        editable(job_id, clip_id, request)
        try:
            changed = runner.edit_clip(job_id, clip_id, edit)
        except ValueError as e:
            raise HTTPException(422, str(e)[:300])
        return {"changed": changed, "render": store.get(job_id).clip(clip_id).model_dump()}

    @app.post("/api/jobs/{job_id}/clips/{clip_id}/reset", dependencies=[Depends(authed)])
    def clip_reset(job_id: str, clip_id: str, request: Request) -> dict:
        editable(job_id, clip_id, request)
        try:
            changed = runner.reset_clip(job_id, clip_id)
        except ValueError as e:
            raise HTTPException(422, str(e)[:300])
        return {"changed": changed, "render": store.get(job_id).clip(clip_id).model_dump()}

    @app.post("/api/jobs/{job_id}/clips/{clip_id}/render", dependencies=[Depends(authed)])
    def clip_render(job_id: str, clip_id: str, request: Request) -> dict:
        editable(job_id, clip_id, request)
        if store.get(job_id).clip(clip_id).render in ("queued", "rendering"):
            raise HTTPException(409, "This clip is already rendering.")
        runner.rerender(job_id, clip_id)
        return {"render": store.get(job_id).clip(clip_id).model_dump()}

    @app.get("/api/jobs/{job_id}/usage", dependencies=[Depends(authed)])
    def usage(job_id: str, request: Request) -> dict:
        job_or_404(job_id, request)
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
