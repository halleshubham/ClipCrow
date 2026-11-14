"""The HTTP API and the static web UI.

Security model (v1): built for one trusted user or a small team on a private network. Bind to 127.0.0.1
(the default) or put it behind a reverse proxy with TLS. Set CLIPCROW_TOKEN to require a login (cookie,
HttpOnly + SameSite=Strict). Every id in a URL is checked against a strict pattern and resolved inside the
job's own directory; uploads are size-capped, extension-checked and probed with ffprobe; error messages shown
to the browser never contain server internals; responses carry a strict Content-Security-Policy.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import hmac
import json
import logging
import math
import re
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional, Union

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from ..assets.urlguard import UnsafeURL, assert_public_url
from ..branding import BrandError
from ..costs import estimate_cost, estimate_total
from ..edl import load_edl
from ..planner.source import probe_source
from .accounts import AccountError, Accounts, User
from .billing import BillingError, Razorpay, apply_event
from .brand import MAX_LOGO_BYTES, MAX_OUTRO_BYTES, BrandStore
from .editor import ClipEdit, clip_view
from .jobs import VIDEO_SUFFIXES, Job, JobStore, Params
from .plans import Catalog, load_catalog
from .publish import LIMITS, PublishError, SocioBird, caption_html, settings_for, validate_when
from .retention import Sweeper, env_days, expires_at
from .storage import Mirror, S3Storage, StorageError, signature
from .uploads import OPEN_LIMIT, Upload, UploadStore
from .workers import HeartbeatIn, PollIn, ResultIn, TASK_ID, WorkerHub, _clean_name
from ..render import HYPERFRAMES_VERSION
from .runner import JobRunner, Pipeline
from .style_store import StyleStore
from ..styles import MAX_REFERENCE_BYTES, StyleError, StyleSettings, derive, measure

STATIC = Path(__file__).parent / "static"
COOKIE = "clipcrow_token"
SESSION_COOKIE = "clipcrow_session"
LANGUAGES = [{"code": "", "name": "Auto-detect"}, {"code": "mr", "name": "Marathi"}, {"code": "hi", "name": "Hindi"},
             {"code": "en", "name": "English"}]
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; "
       "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


# A failure of something we call (SocioBird, the payment service, the bucket) is 424, never 502: Cloudflare, which sits in front of a
# hosted server, replaces an origin's 502/504 with its own page, and the person would never see our message.
UPSTREAM = 424


class Login(BaseModel):
    token: str


class Credentials(BaseModel):
    email: str
    password: str


class Password(BaseModel):
    password: str


class PlanChoice(BaseModel):
    plan: str


class PackChoice(BaseModel):
    pack: str


def _part_size() -> int:
    """Every part but the last has this size (S3/R2: equal parts of at least 5 MiB, at most 10 000 parts). Smaller parts retry
    faster on a bad connection; ``CLIPCROW_UPLOAD_PART_MB`` changes it (default 64)."""
    try:
        return max(5, int(os.environ.get("CLIPCROW_UPLOAD_PART_MB") or 64)) * 1024 * 1024
    except ValueError:
        return 64 * 1024 * 1024


PART_SIZE = _part_size()
ETAG = re.compile(r'^"?[0-9a-fA-F]{32}"?\Z')


class StartUpload(BaseModel):
    filename: str = Field(max_length=300)
    size: int = Field(gt=0)
    rights: bool = False


class PartIn(BaseModel):
    n: int = Field(ge=1, le=10000)
    etag: str = Field(max_length=100)


class CompleteUpload(BaseModel):
    parts: list[PartIn] = Field(max_length=10000)
    options: dict[str, Union[str, int, float, bool, None]] = Field(default_factory=dict)


class StylePatch(BaseModel):
    name: Optional[str] = Field(default=None, max_length=60)
    settings: Optional[StyleSettings] = None


class PublishBody(BaseModel):
    channels: list[str]
    caption: str = ""
    title: str = ""
    privacy: Literal["public", "unlisted", "private"] = "public"
    when: Optional[str] = None
    made_for_kids: bool = False
    ai_confirmed: bool = False


def _opt_int(v) -> Optional[int]:
    return int(v) if v not in (None, "") else None


def _opt_float(v, default: float) -> float:
    return float(v) if v not in (None, "") else default


def create_app(data_dir: str | Path, pipeline: Pipeline, *, token: str | None = None, max_upload_mb: int = 4096,
               demo: bool = False, start_runner: bool = True, accounts: Accounts | None = None,
               catalog: Catalog | None = None, allow_signup: bool = True, razorpay: Razorpay | None = None,
               sociobird: SocioBird | None = None, sociobird_shared: bool = False, storage: S3Storage | None = None,
               storage_redirect: bool = False, backup_source: bool = False, local_clip_days: int = 0,
               worker_token: str | None = None, worker_pickup_seconds: float = 120.0, worker_lease_seconds: float = 90.0) -> FastAPI:
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
    mirror = Mirror(storage, store, backup_source=backup_source) if storage else None
    if mirror:
        runner.on_change = mirror.submit
    # presigned downloads and playback come from the bucket's address: allow exactly that origin for media, nothing else
    csp = CSP.replace("media-src 'self'", f"media-src 'self' {storage.origin}") if storage else CSP
    direct = bool(storage) and os.environ.get("CLIPCROW_DIRECT_UPLOAD", "1") != "0"
    if direct:                                              # the browser PUTs file parts straight to the bucket
        csp = csp.replace("connect-src 'self'", f"connect-src 'self' {storage.origin}")
    uploads = UploadStore(data_dir) if direct else None
    runner.storage = storage
    brands = BrandStore(data_dir)
    runner.brands = brands
    styles = StyleStore(data_dir)
    hub = WorkerHub(worker_token, pickup_seconds=worker_pickup_seconds, lease_seconds=worker_lease_seconds, render_pin=HYPERFRAMES_VERSION,
                    max_upload_bytes=int(float(os.environ.get("CLIPCROW_WORKER_MAX_UPLOAD_MB", "300") or 300) * 1024 * 1024)) if worker_token else None
    if hub is not None:
        pipeline.hub = hub                                  # RealPipeline sends speech recognition to a worker when one is online
    style_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="clipcrow-style")
    for _owner, _rec in styles.stale_analyzing():           # a restart interrupted these: nothing is measuring them now
        styles.fail(_owner, _rec.id, "The server restarted while this was being measured. Upload the reference again.")
    if start_runner:
        runner.recover()
    @asynccontextmanager
    async def lifespan(_app):
        yield
        if start_runner:                                    # SIGTERM from a deploy: let the running job finish, or requeue it
            seconds = float(os.environ.get("DRAIN_TIMEOUT_SECONDS", "45") or 0)
            await asyncio.to_thread(runner.drain, seconds)
        publish_pool.shutdown(wait=False, cancel_futures=True)
        if mirror:
            await asyncio.to_thread(mirror.close)

    app = FastAPI(title="ClipCrow", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.store, app.state.runner, app.state.accounts, app.state.catalog = store, runner, accounts, catalog
    app.state.mirror, app.state.mirror_source = mirror, backup_source
    limit = max_upload_mb * 1024 * 1024

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = csp
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api"):
            resp.headers["Cache-Control"] = "no-store"
        elif "Cache-Control" not in resp.headers:      # the UI files: always revalidate (ETag), or a deploy leaves old JS in browsers
            resp.headers["Cache-Control"] = "no-cache"
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

    if start_runner and (accounts or any(env_days()) or storage):
        app.state.sweeper = Sweeper(store, days_for, storage=storage, mirror=mirror, local_clip_days=local_clip_days,
                                    extra=[lambda: expire_uploads()] if direct else [])          # resolved when it runs: defined further down
        app.state.sweeper.start()

    def purge_remote(job_id: str, message: str = "The stored copies could not be removed just now, so nothing was deleted. Try again in a minute.") -> None:
        """Remove a job's copies from the bucket FIRST; if that fails nothing local is deleted either."""
        if storage:
            try:
                storage.delete_job(job_id)
            except StorageError:
                raise HTTPException(UPSTREAM, message)

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
                "signup": bool(accounts) and allow_signup, "email": email, "billing": bool(accounts and razorpay), "direct_upload": direct, "publishing": bool(sociobird) and (not accounts or sociobird_shared),
                "needs_login": bool(token) or bool(accounts), "signed_in": signed_in, "ready": pipeline.check_ready(),
                "ai_images": bool(getattr(pipeline, "can_generate_images", False)),
                "gpu_worker": {"enabled": hub is not None, "online": bool(hub and hub.online()),
                               "render": bool(hub and hub.online("render"))}}

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
            raise HTTPException(403, "That password is not right.")
        if any(j.status in ("queued", "running") for j in store.list() if j.owner == user.id):
            raise HTTPException(409, "Cancel your running jobs first.")
        if user.subscription and razorpay:                       # stop the charges FIRST; if that fails, delete nothing
            try:
                razorpay.cancel_subscription(user.subscription, at_cycle_end=False)
            except BillingError:
                raise HTTPException(UPSTREAM, "Your subscription could not be cancelled just now, so the account was not deleted. "
                                         "Try again in a minute.")
        for j in store.list():
            if j.owner == user.id:
                purge_remote(j.id, "Your files could not be removed from storage just now, so the account was not deleted. Try again in a minute.")
                store.delete(j.id)
        brands.delete_all(user.id)
        styles.delete_all(user.id)
        accounts.delete(user.id)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    # --- publishing (SocioBird) -------------------------------------------------------------------
    publish_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="clipcrow-publish")

    def need_publishing() -> SocioBird:
        if not sociobird or (accounts and not sociobird_shared):
            raise HTTPException(404, "Posting to social channels is not set up on this server.")
        return sociobird

    @app.get("/api/publish/channels", dependencies=[Depends(authed)])
    def publish_channels() -> list[dict]:
        sb = need_publishing()
        try:
            return [{"id": c.id, "name": c.name, "platform": c.platform, "brand": c.brand, "limit": LIMITS.get(c.platform, 2000)}
                    for c in sb.channels()]
        except PublishError as e:
            raise HTTPException(UPSTREAM, str(e))

    @app.get("/api/jobs/{job_id}/clips/{clip_id}/posts", dependencies=[Depends(authed)])
    def clip_posts(job_id: str, clip_id: str, request: Request) -> list[dict]:
        job_or_404(job_id, request)
        return store.posts(job_id, clip_id)

    @app.post("/api/jobs/{job_id}/clips/{clip_id}/publish", dependencies=[Depends(authed)], status_code=202)
    def clip_publish(job_id: str, clip_id: str, body: PublishBody, request: Request) -> list[dict]:
        sb = need_publishing()
        job = job_or_404(job_id, request)
        info = next((c for c in job.clips if c.id == clip_id), None)
        if info is None or job.status != "done":
            raise HTTPException(404, "No such clip.")
        path = store.clip_path(job_id, clip_id)
        if not path.is_file() and storage and job.remote.get(f"out/{clip_id}.mp4"):       # evicted from local disk: fetch it back
            try:
                storage.get_file(job_id, f"out/{clip_id}.mp4", path)
                sig = signature(path)
                store.update(job_id, lambda j: j.remote.__setitem__(f"out/{clip_id}.mp4", sig))
            except StorageError:
                raise HTTPException(UPSTREAM, "The clip could not be fetched from storage just now. Try again in a minute.")
        if not path.is_file() or info.render in ("queued", "rendering"):
            raise HTTPException(409, "This clip is not ready yet.")
        from ..provenance import provenance
        try:
            edl = load_edl(store.edl_path(job_id))
            ai_media = provenance(edl, next(c for c in edl.clips if c.id == clip_id))["disclosure_recommended"]
        except (OSError, StopIteration, ValueError):
            raise HTTPException(404, "The plan for this clip is not on disk.")
        if ai_media and not body.ai_confirmed:
            raise HTTPException(422, "This clip includes AI-generated pictures. Confirm that you will label it as AI content where the platform asks.")
        if not body.channels or len(body.channels) > 6 or len(set(body.channels)) != len(body.channels):
            raise HTTPException(422, "Choose between 1 and 6 different channels.")
        try:
            when = validate_when(body.when)
            known = {c.id: c for c in sb.channels()}
        except PublishError as e:
            raise HTTPException(422 if "date" in str(e) or "time" in str(e) or "year" in str(e) else UPSTREAM, str(e))
        chosen = []
        for cid in body.channels:
            if cid not in known:
                raise HTTPException(422, "One of those channels is not available.")
            chosen.append(known[cid])
        title = (body.title or info.title).strip()
        try:                                                      # check every channel's settings BEFORE anything is queued
            plans = [(ch, settings_for(ch.platform, title=title, privacy=body.privacy, made_for_kids=body.made_for_kids,
                                       ai_generated=ai_media), caption_html(body.caption or title, ch.platform)) for ch in chosen]
        except PublishError as e:
            raise HTTPException(422, str(e))
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows = [{"id": uuid.uuid4().hex[:12], "clip": clip_id, "channel": ch.id, "name": ch.name, "platform": ch.platform,
                 "status": "queued", "detail": "", "when": when.isoformat(timespec="seconds") if when else None, "at": now}
                for ch, _, _ in plans]
        store.add_posts(job_id, rows)

        def work() -> None:
            try:
                media = sb.upload(path)
            except PublishError as e:
                for r in rows:
                    store.update_post(job_id, r["id"], "failed", str(e))
                return
            except Exception:
                logging.getLogger("clipcrow.service").exception("upload to SocioBird failed")
                for r in rows:
                    store.update_post(job_id, r["id"], "failed", "The video could not be uploaded.")
                return
            for (ch, settings, content), r in zip(plans, rows):
                try:
                    sb.post(ch, content, media, settings, when=when)
                    store.update_post(job_id, r["id"], "scheduled" if when else "sent")
                except PublishError as e:
                    store.update_post(job_id, r["id"], "failed", str(e))
                except Exception:
                    logging.getLogger("clipcrow.service").exception("post to SocioBird failed")
                    store.update_post(job_id, r["id"], "failed", "The post could not be created.")
        publish_pool.submit(work)
        return rows

    # --- billing (Razorpay) ----------------------------------------------------------------------
    def need_billing() -> Razorpay:
        if not (accounts and razorpay):
            raise HTTPException(404, "Billing is not set up on this server.")
        return razorpay

    @app.get("/api/billing", dependencies=[Depends(authed)])
    def billing_info(request: Request) -> dict:
        user = request.state.user
        need_accounts()
        return {"enabled": bool(razorpay), "subscribed": bool(user.subscription), "current": effective_plan(user).id,
                "plans": [{"id": p.id, "name": p.name, "price_label": p.price_label, "monthly_minutes": p.monthly_minutes,
                           "can_subscribe": bool(p.razorpay_plan_id) and p.id != "free"} for p in catalog.plans.values()],
                "packs": [{"id": k.id, "minutes": k.minutes, "label": k.label} for k in catalog.packs.values()]}

    @app.post("/api/billing/subscribe", dependencies=[Depends(authed)])
    def subscribe(body: PlanChoice, request: Request) -> dict:
        user, rz = request.state.user, need_billing()
        plan = catalog.plans.get(body.plan)
        if plan is None or plan.id == "free" or not plan.razorpay_plan_id:
            raise HTTPException(422, "That plan cannot be bought.")
        if user.subscription:
            raise HTTPException(409, "You already have a subscription. Cancel it first to change plan.")
        try:
            return {"url": rz.create_subscription(plan.razorpay_plan_id, user.id, plan.id)["url"]}
        except BillingError as e:
            raise HTTPException(UPSTREAM, str(e))

    @app.post("/api/billing/credits", dependencies=[Depends(authed)])
    def buy_credits(body: PackChoice, request: Request) -> dict:
        user, rz = request.state.user, need_billing()
        pack = catalog.packs.get(body.pack)
        if pack is None:
            raise HTTPException(422, "That pack does not exist.")
        try:
            return {"url": rz.create_payment_link(pack.amount_paise, pack.label, user.id, pack.id)["url"]}
        except BillingError as e:
            raise HTTPException(UPSTREAM, str(e))

    @app.post("/api/billing/cancel", dependencies=[Depends(authed)])
    def cancel_subscription(request: Request) -> dict:
        user, rz = request.state.user, need_billing()
        if not user.subscription:
            raise HTTPException(409, "You do not have a subscription.")
        try:
            rz.cancel_subscription(user.subscription, at_cycle_end=True)
        except BillingError as e:
            raise HTTPException(UPSTREAM, str(e))
        return {"ok": True, "note": "Your plan stays active until the end of the period you paid for."}

    @app.post("/api/billing/razorpay/webhook")
    async def razorpay_webhook(request: Request) -> dict:
        rz = need_billing()
        if int(request.headers.get("content-length") or 0) > 1_000_000:
            raise HTTPException(413, "Too large.")
        body = await request.body()
        if len(body) > 1_000_000 or not rz.verify(body, request.headers.get("x-razorpay-signature", "")):
            raise HTTPException(400, "Bad signature.")                  # nothing is read from an unsigned body
        try:
            event = json.loads(body)
        except ValueError:
            raise HTTPException(400, "Not JSON.")
        event_id = request.headers.get("x-razorpay-event-id") or hashlib.sha256(body).hexdigest()
        return {"status": apply_event(event, event_id[:100], accounts, catalog)}

    # --- jobs -----------------------------------------------------------------------------
    def truthy(v) -> bool:
        return v is True or str(v).lower() in ("1", "true", "on", "yes")

    def brand_owner(request: Request) -> str:
        user = getattr(request.state, "user", None)
        return user.id if accounts and user is not None else "shared"

    def build_params(o: dict, owner: str = "shared") -> Params:
        """The job options, from a form or from JSON (strings or typed values), checked once for both upload paths."""
        link_list = [x.strip() for x in str(o.get("links") or "").splitlines() if x.strip()]
        want_images = truthy(o.get("ai_images"))
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
            params = Params(language=o.get("language") or None, clips=_opt_int(o.get("clips")),
                            min_clip=_opt_int(o.get("min_clip")) or 15, max_clip=_opt_int(o.get("max_clip")) or 60,
                            spread=_opt_float(o.get("spread"), 0.5), speaker_name=str(o.get("speaker_name") or "").strip() or None,
                            speaker_title=str(o.get("speaker_title") or "").strip(), cutout=truthy(o.get("cutout")),
                            immersive=truthy(o.get("immersive")), explainer=truthy(o.get("explainer")), links=link_list,
                            ai_images=want_images, multi=o.get("multi") or "switch", graphics=truthy(o.get("graphics")),
                            clean=truthy(o.get("clean")), clean_pace=o.get("clean_pace") or "natural",
                            cover=truthy(o.get("cover")), logo=truthy(o.get("logo")), outro=truthy(o.get("outro")))
        except (ValidationError, ValueError, TypeError) as e:
            raise HTTPException(422, f"Check the options: {str(e).splitlines()[0][:160]}")
        if params.min_clip >= params.max_clip:
            raise HTTPException(422, "The shortest clip must be shorter than the longest.")
        sid = str(o.get("style") or "").strip()
        if sid:
            try:
                rec = styles.get(owner, sid)
            except KeyError:
                raise HTTPException(422, "That style does not exist any more. Choose another or none.")
            if rec.status != "ready":
                raise HTTPException(422, "That style is not ready yet.")
            st = rec.settings.model_copy(deep=True)
            if not (_opt_int(o.get("min_clip")) or _opt_int(o.get("max_clip"))):      # an explicit clip length from the person wins
                params.min_clip, params.max_clip = st.min_clip, st.max_clip
            params.style, params.style_name = st, rec.name
        if params.logo and brands.logo_path(owner) is None:
            raise HTTPException(422, "Upload a logo on the Brand page first, or turn the logo off.")
        if params.outro and brands.outro_path(owner) is None:
            raise HTTPException(422, "Upload an ending video on the Brand page first, or turn it off.")
        return params

    # --- direct uploads: the browser sends the file to the bucket, not through this server --------------------------------
    def need_direct() -> S3Storage:
        if not direct:
            raise HTTPException(404, "Direct uploads are not turned on.")
        return storage

    def upload_or_404(upload_id: str, request: Request) -> Upload:
        try:
            rec = uploads.get(upload_id)
        except KeyError:
            raise HTTPException(404, "No such upload.")
        user = getattr(request.state, "user", None)
        if accounts and (user is None or rec.owner != user.id):
            raise HTTPException(404, "No such upload.")
        return rec

    def abandon(rec: Upload, *, finished: bool) -> None:
        """Throw an upload away: abort the multipart upload, or delete the object if it was already put together."""
        try:
            if finished:
                storage.delete(rec.id, rec.rel)
            else:
                storage.abort_multipart(rec.id, rec.rel, rec.s3_upload_id)
        except StorageError:
            log = logging.getLogger("clipcrow.service")
            log.warning("upload %s could not be removed from storage; the expiry sweep will retry", rec.id)
            return                                           # keep the record so the sweep tries again
        uploads.delete(rec.id)

    def expire_uploads() -> None:
        for rec in uploads.expired():
            try:
                storage.abort_multipart(rec.id, rec.rel, rec.s3_upload_id)
            except StorageError:
                continue
            try:
                storage.delete(rec.id, rec.rel)
            except StorageError:
                continue
            uploads.delete(rec.id)

    app.state.uploads, app.state.expire_uploads = uploads, expire_uploads

    @app.post("/api/uploads", dependencies=[Depends(authed)], status_code=201)
    def start_upload(body: StartUpload, request: Request) -> dict:
        sb = need_direct()
        if not body.rights:
            raise HTTPException(400, "Confirm that you own this video or have the rights to process it.")
        suffix = Path(body.filename).suffix.lower()
        if suffix not in VIDEO_SUFFIXES:
            raise HTTPException(400, "Upload an MP4, MOV, MKV, WebM or M4V video.")
        if body.size > limit:
            raise HTTPException(413, f"That file is larger than the {max_upload_mb} MB limit.")
        problem = pipeline.check_ready()
        if problem:
            raise HTTPException(503, problem)
        user = request.state.user
        owner = user.id if accounts else None
        if uploads.open_for(owner) >= OPEN_LIMIT:
            raise HTTPException(429, "You have unfinished uploads. Let them finish or cancel them first.")
        count = -(-body.size // PART_SIZE)
        part_size = PART_SIZE if count > 1 else body.size
        uid = uploads.new_id()
        rel = f"source{suffix}"
        try:
            s3id = sb.create_multipart(uid, rel)
        except StorageError as e:
            raise HTTPException(UPSTREAM, str(e))
        rec = Upload(id=uid, owner=owner, filename=Path(body.filename).name[:200], suffix=suffix, size=body.size,
                     part_size=part_size, parts=count, s3_upload_id=s3id, created_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
        uploads.save(rec)
        try:
            parts = []
            for n in range(1, count + 1):
                length = part_size if n < count else body.size - part_size * (count - 1)
                parts.append({"n": n, "length": length, "url": sb.presign_part(uid, rel, s3id, n, length)})
        except StorageError as e:
            abandon(rec, finished=False)
            raise HTTPException(UPSTREAM, str(e))
        return {"id": uid, "part_size": part_size, "parts": parts, "expires_in": 3600}

    @app.delete("/api/uploads/{upload_id}", dependencies=[Depends(authed)], status_code=204)
    def cancel_upload(upload_id: str, request: Request) -> None:
        need_direct()
        abandon(upload_or_404(upload_id, request), finished=False)

    @app.post("/api/uploads/{upload_id}/complete", dependencies=[Depends(authed)], status_code=201)
    async def complete_upload(upload_id: str, body: CompleteUpload, request: Request) -> dict:
        sb = need_direct()
        rec = upload_or_404(upload_id, request)
        user = request.state.user
        params = build_params(body.options, brand_owner(request))                    # a bad option must not cost the person their upload: check first
        if sorted(p.n for p in body.parts) != list(range(1, rec.parts + 1)):
            raise HTTPException(422, "Some parts of the upload are missing.")
        if any(not ETAG.match(p.etag) for p in body.parts):
            raise HTTPException(422, "The storage service did not confirm every part.")
        try:
            await asyncio.to_thread(sb.complete_multipart, rec.id, rec.rel, rec.s3_upload_id, [(p.n, p.etag) for p in body.parts])
        except StorageError:
            abandon(rec, finished=False)
            raise HTTPException(422, "The upload did not finish properly. Start it again.")
        try:
            size = await asyncio.to_thread(sb.size, rec.id, rec.rel)
        except StorageError as e:
            raise HTTPException(UPSTREAM, str(e))
        if size != rec.size:
            abandon(rec, finished=True)
            raise HTTPException(422, "The uploaded file is not the size that was announced. Start it again.")
        try:
            info = await asyncio.to_thread(probe_source, sb.presign_get(rec.id, rec.rel, expires=900), remote=True)
        except (ValueError, StorageError, OSError):
            abandon(rec, finished=True)
            raise HTTPException(400, "That file could not be read as a video.")
        if info.duration < params.min_clip + 5:
            abandon(rec, finished=True)
            raise HTTPException(400, f"This video is {info.duration:.0f} s long; clips of at least {params.min_clip} s need a longer video.")
        job = store.create(rec.filename, params, demo=demo, job_id=rec.id)
        job.source = {"duration": info.duration, "width": info.width, "height": info.height}
        job.source_pending = rec.rel
        job.remote[rec.rel] = f"remote:{rec.size}"              # in the bucket already; retention removes it with the job
        if accounts:
            job.owner = user.id
            minutes = max(1, math.ceil(info.duration / 60))
            plan = effective_plan(user)
            if accounts.reserve(user.id, job.id, minutes, plan.monthly_minutes) is None:
                store.delete(job.id)
                abandon(rec, finished=True)
                have = max(plan.monthly_minutes - accounts.used_this_month(user.id), 0) + accounts.get(user.id).credits
                raise HTTPException(402, f"This video is {minutes} min long and you have {have:.0f} min left on the {plan.name} plan this "
                                         f"month. Upgrade, buy minutes, or wait for the month to reset.")
            job.reserved_minutes = minutes
        store.save(job)
        uploads.delete(rec.id)
        runner.submit(job.id)
        return view(store.get(job.id))

    @app.post("/api/jobs", dependencies=[Depends(authed)], status_code=201)
    async def create_job(request: Request, file: UploadFile = File(...), rights: str = Form(""),
                         language: Optional[str] = Form(None), clips: Optional[str] = Form(None),
                         min_clip: Optional[str] = Form(None), max_clip: Optional[str] = Form(None),
                         spread: Optional[str] = Form(None), speaker_name: Optional[str] = Form(None),
                         speaker_title: Optional[str] = Form(""), cutout: Optional[str] = Form(None),
                         multi: Optional[str] = Form(None), graphics: Optional[str] = Form(None),
                         clean: Optional[str] = Form(None), clean_pace: Optional[str] = Form(None), immersive: Optional[str] = Form(None),
                         explainer: Optional[str] = Form(None), links: Optional[str] = Form(None),
                         ai_images: Optional[str] = Form(None), cover: Optional[str] = Form(None),
                         logo: Optional[str] = Form(None), outro: Optional[str] = Form(None),
                         style: Optional[str] = Form(None)) -> dict:
        if rights.lower() not in ("1", "true", "on", "yes"):
            raise HTTPException(400, "Confirm that you own this video or have the rights to process it.")
        declared = int(request.headers.get("content-length") or 0)
        if declared > limit + 1_000_000:
            raise HTTPException(413, f"That file is larger than the {max_upload_mb} MB limit.")
        params = build_params(dict(language=language, clips=clips, min_clip=min_clip, max_clip=max_clip, spread=spread,
                                   speaker_name=speaker_name, speaker_title=speaker_title, cutout=cutout, multi=multi, graphics=graphics, clean=clean, clean_pace=clean_pace,
                                   immersive=immersive, explainer=explainer, links=links, ai_images=ai_images, cover=cover, logo=logo, outro=outro,
                                   style=style),
                              brand_owner(request))
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
        purge_remote(job_id)
        store.delete(job_id)

    def clip_response(job_id: str, clip_id: str, request: Request, *, download: bool):
        job = job_or_404(job_id, request)
        try:
            path = store.clip_path(job_id, clip_id)
        except KeyError:
            raise HTTPException(404, "No such clip.")
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(job.name).stem).strip("._") or "clip"
        name = f"{stem[:60]}-{clip_id}.mp4"
        rel = f"out/{clip_id}.mp4"
        local = path.is_file()
        remote_sig = job.remote.get(rel)
        # From the bucket when the local file is gone, or (if asked) whenever the bucket copy is the same file.
        if storage and remote_sig and (not local or (storage_redirect and signature(path) == remote_sig)):
            try:
                url = storage.presign_get(job_id, rel, expires=600, filename=name if download else None)
                return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})
            except StorageError:
                if not local:
                    raise HTTPException(UPSTREAM, "The clip could not be fetched from storage just now. Try again in a minute.")
        if not local:
            raise HTTPException(404, "This clip is not ready yet.")
        return FileResponse(path, media_type="video/mp4", filename=name if download else None)

    @app.get("/api/jobs/{job_id}/clips/{clip_id}/video", dependencies=[Depends(authed)])
    def clip_video(job_id: str, clip_id: str, request: Request):
        return clip_response(job_id, clip_id, request, download=False)

    @app.get("/api/jobs/{job_id}/clips/{clip_id}/download", dependencies=[Depends(authed)])
    def clip_download(job_id: str, clip_id: str, request: Request):
        return clip_response(job_id, clip_id, request, download=True)

    @app.get("/api/jobs/{job_id}/clips/{clip_id}/cover", dependencies=[Depends(authed)])
    def clip_cover(job_id: str, clip_id: str, request: Request, download: int = 0):
        job = job_or_404(job_id, request)
        try:
            path = store.cover_path(job_id, clip_id)
        except KeyError:
            raise HTTPException(404, "No such clip.")
        if not path.is_file():
            raise HTTPException(404, "This clip has no cover image.")
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(job.name).stem).strip("._") or "clip"
        return FileResponse(path, media_type="image/png", filename=f"{stem[:60]}-{clip_id}-cover.png" if download else None)

    # --- styles: measure a reference video once, apply what it shows to your own clips ----------------------------------
    def style_view(rec) -> dict:
        return {"id": rec.id, "name": rec.name, "status": rec.status, "error": rec.error, "created_at": rec.created_at,
                "measured": rec.measured, "settings": rec.settings.model_dump()}

    def style_or_404(style_id: str, request: Request):
        try:
            return styles.get(brand_owner(request), style_id)
        except KeyError:
            raise HTTPException(404, "No such style.")

    def analyse_reference(owner: str, style_id: str, tmp: tempfile.TemporaryDirectory, path: Path) -> None:
        try:
            m = measure(path, count_words=getattr(pipeline, "reference_words", None),
                        read_look=getattr(pipeline, "reference_look", None))
            styles.finish(owner, style_id, m.view(), derive(m))
        except StyleError as e:
            styles.fail(owner, style_id, str(e))
        except Exception:                                 # unexpected: a generic message for the person, the details in the log
            logging.getLogger("clipcrow").exception("measuring a style reference failed")
            styles.fail(owner, style_id, "The reference could not be measured. Try another video.")
        finally:
            tmp.cleanup()                                 # the reference video is never kept

    @app.get("/api/styles", dependencies=[Depends(authed)])
    def styles_list(request: Request) -> list[dict]:
        return [style_view(r) for r in styles.list(brand_owner(request))]

    @app.post("/api/styles", dependencies=[Depends(authed)], status_code=202)
    async def style_create(request: Request, file: UploadFile = File(...), name: str = Form(""), rights: str = Form("")) -> dict:
        if not truthy(rights):
            raise HTTPException(400, "Confirm that you may use this video as a reference.")
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in VIDEO_SUFFIXES:
            raise HTTPException(400, "Upload an MP4, MOV, MKV, WebM or M4V video.")
        owner = brand_owner(request)
        try:
            rec = styles.create(owner, name or Path(file.filename or "Style").stem)
        except StyleError as e:
            raise HTTPException(409, str(e))
        tmp = tempfile.TemporaryDirectory(dir=data_dir)
        dest = Path(tmp.name) / f"reference{suffix}"
        try:
            size = 0
            with dest.open("wb") as f:
                while chunk := await file.read(1 << 20):
                    size += len(chunk)
                    if size > MAX_REFERENCE_BYTES:
                        raise HTTPException(413, f"A reference can be at most {MAX_REFERENCE_BYTES // (1024 * 1024)} MB.")
                    f.write(chunk)
        except BaseException:
            tmp.cleanup()
            styles.delete(owner, rec.id)
            raise
        style_pool.submit(analyse_reference, owner, rec.id, tmp, dest)
        return style_view(rec)

    @app.get("/api/styles/{style_id}", dependencies=[Depends(authed)])
    def style_get(style_id: str, request: Request) -> dict:
        return style_view(style_or_404(style_id, request))

    @app.put("/api/styles/{style_id}", dependencies=[Depends(authed)])
    def style_put(style_id: str, body: StylePatch, request: Request) -> dict:
        style_or_404(style_id, request)
        try:
            return style_view(styles.update(brand_owner(request), style_id, name=body.name, settings=body.settings))
        except StyleError as e:
            raise HTTPException(422, str(e))

    @app.delete("/api/styles/{style_id}", dependencies=[Depends(authed)], status_code=204)
    def style_delete(style_id: str, request: Request) -> None:
        style_or_404(style_id, request)
        styles.delete(brand_owner(request), style_id)

    # --- GPU workers: a machine the operator owns pulls speech-recognition tasks (see service/workers.py) ----------------------
    def worker_auth(request: Request) -> None:
        if hub is None:
            raise HTTPException(404, "Not found.")
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer ") or not hub.check_token(header[7:].strip()):
            raise HTTPException(401, "That worker token is not right.")

    def task_id_or_404(task_id: str) -> str:
        if not re.fullmatch(TASK_ID, task_id):
            raise HTTPException(404, "No such task.")
        return task_id

    @app.post("/api/worker/poll", dependencies=[Depends(worker_auth)])
    async def worker_poll(body: PollIn) -> dict:
        task = await asyncio.to_thread(hub.poll, body.worker, body.wait, body.device, body.model, tuple(body.can), body.pin)
        if task is None:
            return {"task": None}
        out = {"id": task.id, "kind": task.kind, "lease_seconds": hub.lease_seconds, "meta": task.meta}
        if task.kind == "transcribe":
            out["language"] = task.language                          # (an older worker reads it from here)
        else:
            out["files"] = {name: path.stat().st_size for name, path in task.files.items() if path.is_file()}
        return {"task": out}

    @app.get("/api/worker/tasks/{task_id}/audio", dependencies=[Depends(worker_auth)])
    def worker_audio(task_id: str, worker: str):
        path = hub.audio_for(task_id_or_404(task_id), worker)
        if path is None or not path.is_file():
            raise HTTPException(404, "No such task.")
        return FileResponse(path, media_type="audio/mp4")

    @app.get("/api/worker/tasks/{task_id}/files/{name:path}", dependencies=[Depends(worker_auth)])
    def worker_file(task_id: str, name: str, worker: str):
        """A file of the task's manifest. The name is only ever a dictionary key, never joined to a path."""
        path = hub.file_for(task_id_or_404(task_id), name, worker) if _clean_name(name) else None
        if path is None or not path.is_file():
            raise HTTPException(404, "No such file.")
        return FileResponse(path, media_type="application/octet-stream")

    @app.put("/api/worker/tasks/{task_id}/uploads/{name}", dependencies=[Depends(worker_auth)])
    async def worker_upload(task_id: str, name: str, request: Request, worker: str) -> dict:
        target = hub.upload_path(task_id_or_404(task_id), worker, name)
        if target is None:
            raise HTTPException(404, "No such upload.")
        declared = int(request.headers.get("content-length") or 0)
        if declared > hub.max_upload_bytes:
            raise HTTPException(413, "That upload is larger than this server accepts.")
        part = target.with_name(target.name + ".part")
        size = 0
        try:
            with part.open("wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > hub.max_upload_bytes:
                        raise HTTPException(413, "That upload is larger than this server accepts.")
                    await asyncio.to_thread(f.write, chunk)
            os.replace(part, target)
        finally:
            part.unlink(missing_ok=True)
        if not hub.commit_upload(task_id, worker, name, target):
            target.unlink(missing_ok=True)
            raise HTTPException(404, "That task is no longer yours.")
        return {"ok": True, "size": size}

    @app.post("/api/worker/tasks/{task_id}/heartbeat", dependencies=[Depends(worker_auth)])
    def worker_heartbeat(task_id: str, body: HeartbeatIn) -> dict:
        return {"ok": hub.heartbeat(task_id_or_404(task_id), body.worker)}

    @app.post("/api/worker/tasks/{task_id}/result", dependencies=[Depends(worker_auth)])
    def worker_result(task_id: str, body: ResultIn) -> dict:
        task_id_or_404(task_id)
        kind = hub.kind_of(task_id, body.worker)
        if kind is None:
            return {"ok": False}
        if body.error:
            return {"ok": hub.finish(task_id, body.worker, error=body.error)}
        if kind == "render":
            return {"ok": hub.finish(task_id, body.worker, result={"warnings": body.warnings})}
        if not body.words:
            return {"ok": hub.finish(task_id, body.worker, error="the worker found no speech")}
        return {"ok": hub.finish(task_id, body.worker, result={"language": body.language, "words": [w.model_dump() for w in body.words]})}

    # --- the brand kit: logo, ending video, how the cover and logo look ----------------------------------------
    @app.get("/api/brand", dependencies=[Depends(authed)])
    def brand_get(request: Request) -> dict:
        return brands.describe(brand_owner(request))

    @app.put("/api/brand", dependencies=[Depends(authed)])
    def brand_put(request: Request, body: dict = Body(...)) -> dict:
        try:
            brands.set_settings(brand_owner(request), body)
        except BrandError as e:
            raise HTTPException(422, str(e))
        return brands.describe(brand_owner(request))

    @app.post("/api/brand/logo", dependencies=[Depends(authed)])
    async def brand_logo(request: Request, file: UploadFile = File(...)) -> dict:
        data = await file.read(MAX_LOGO_BYTES + 1)
        try:
            return await asyncio.to_thread(brands.put_logo, brand_owner(request), data)
        except BrandError as e:
            raise HTTPException(422, str(e))

    @app.post("/api/brand/outro", dependencies=[Depends(authed)])
    async def brand_outro(request: Request, file: UploadFile = File(...)) -> dict:
        owner = brand_owner(request)
        with tempfile.TemporaryDirectory(dir=data_dir) as tmp:
            raw = Path(tmp) / "in"
            size = 0
            with raw.open("wb") as f:
                while chunk := await file.read(1 << 20):
                    size += len(chunk)
                    if size > MAX_OUTRO_BYTES:
                        raise HTTPException(413, f"The ending video can be at most {MAX_OUTRO_BYTES // (1024 * 1024)} MB.")
                    f.write(chunk)
            try:
                return await asyncio.to_thread(brands.put_outro, owner, raw)
            except BrandError as e:
                raise HTTPException(422, str(e))

    @app.delete("/api/brand/{what}", dependencies=[Depends(authed)])
    def brand_delete(what: str, request: Request) -> dict:
        try:
            return brands.delete(brand_owner(request), what)
        except BrandError as e:
            raise HTTPException(404, str(e))

    @app.get("/api/brand/logo/file", dependencies=[Depends(authed)])
    def brand_logo_file(request: Request):
        path = brands.logo_path(brand_owner(request))
        if path is None:
            raise HTTPException(404, "No logo yet.")
        return FileResponse(path, media_type={".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}[path.suffix],
                            headers={"Cache-Control": "no-cache"})

    @app.get("/api/brand/outro/file", dependencies=[Depends(authed)])
    def brand_outro_file(request: Request):
        path = brands.outro_path(brand_owner(request))
        if path is None:
            raise HTTPException(404, "No ending video yet.")
        return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "no-cache"})

    # --- editing one clip ---------------------------------------------------------------------
    def editable(job_id: str, clip_id: str, request: Request) -> Job:
        job = job_or_404(job_id, request)
        if not any(c.id == clip_id for c in job.clips):
            raise HTTPException(404, "No such clip.")
        if job.status != "done":
            raise HTTPException(409, "Wait for the job to finish before editing its clips.")
        if job.source_removed or store.working_video(job_id) is None:
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
