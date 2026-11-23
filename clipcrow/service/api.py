"""The HTTP API and the static web UI.

Security model (v1): built for one trusted user or a small team on a private network. Bind to 127.0.0.1
(the default) or put it behind a reverse proxy with TLS. Public use needs accounts (CLIPCROW_ACCOUNTS=1): sign-in is by an
emailed code only, with no password and no shared token (session cookie: HttpOnly + SameSite=Strict). Every id in a URL is checked against a strict pattern and resolved inside the
job's own directory; uploads are size-capped, extension-checked and probed with ffprobe; error messages shown
to the browser never contain server internals; responses carry a strict Content-Security-Policy.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import os
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request
import subprocess
import threading
import time
import logging
import math
import re
import shutil
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal, Optional, Union

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator

from ..assets.urlguard import UnsafeURL, assert_public_url
from ..branding import BrandError
from ..costs import estimate_cost, estimate_total
from ..edl import load_edl
from ..planner.source import probe_source
from .accounts import AccountError, Accounts, CODE_MINUTES, CODE_WAIT_S, User
from .sample import import_sample
from .economics import economics as job_economics, suggest_min_minutes, suggest_price, target_markup
from . import settings as opsettings
from .ops import Limits, Snapshot, Watchdog, admin_emails, disk_snapshot, is_throwaway, job_refusal, jobs_since, RateLimiter
from .mailer import MailQueue, Mailer, Message, Outbox, billing_mail, job_done_mail, plain_mail, sign_in_code_mail, welcome_mail
from .billing import BillingError, Razorpay, apply_event
from .docstore import DocStore, store_mode
from .brand import MAX_LOGO_BYTES, MAX_OVERLAY_BYTES, MAX_MUSIC_BYTES, MAX_OUTRO_BYTES, TRACK_ID, BrandStore, track_path
from .editor import ClipEdit, clip_view, TranscriptEdit, transcript_view
from .jobs import VIDEO_SUFFIXES, Job, JobStore, Params
from .plans import Payg, Catalog, load_catalog
from .publish import LIMITS, PublishError, SocioBird, caption_html, settings_for, validate_when
from ..buildid import render_build
from .gpu_cost import estimate_numbers, load_rates, rate_lookup, summarise as gpu_summary
from .schedule import Chan, MAX_CHANNELS as BULK_CHANNELS, MAX_CLIPS as BULK_CLIPS, MAX_PER_DAY, SchedulePrefs, ScheduleError, clean_times, plan as plan_schedule, zone as schedule_zone
from .retention import Sweeper, env_days, expires_at, evict_local_clips
from .storage import Mirror, S3Storage, StorageError, signature
from .pictures import MAX_BYTES as MAX_PICTURE_BYTES, PICTURE_ID, PictureError, PictureStore, parse_ids
from .uploads import OPEN_LIMIT, Upload, UploadStore
from .workers import DirectDoneIn, DirectIn, HeartbeatIn, PollIn, ResultIn, TASK_ID, WorkerError, WorkerHub, _clean_name
from ..render import HYPERFRAMES_VERSION
from .runner import JobRunner, Pipeline
from .style_store import StyleStore
from ..styles import MAX_REFERENCE_BYTES, StyleError, StyleSettings, derive, measure

log = logging.getLogger("clipcrow.service")
STATIC = Path(__file__).parent / "static"
SESSION_COOKIE = "clipcrow_session"
SUPERADMIN_SESSION_DAYS = 7          # a superadmin signs in again after a week (everyone else: 30 days)
LANGUAGES = [{"code": "", "name": "Auto-detect"}, {"code": "mr", "name": "Marathi"}, {"code": "hi", "name": "Hindi"},
             {"code": "en", "name": "English"}]
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
       "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


# A failure of something we call (SocioBird, the payment service, the bucket) is 424, never 502: Cloudflare, which sits in front of a
# hosted server, replaces an origin's 502/504 with its own page, and the person would never see our message.
UPSTREAM = 424


class SpeakerChoice(BaseModel):
    voices: dict[str, Optional[int]] = Field(max_length=30)     # voice number -> person (1 = first from the left), null = off screen


class LogoPreview(BaseModel):
    position: Literal["tl", "tr", "bl", "br"] = "tr"
    size_pct: int = Field(default=18, ge=5, le=40)
    margin_px: int = Field(default=60, ge=0, le=300)
    zone: Optional[tuple[float, float, float, float]] = None
    caption_top: Optional[int] = Field(default=None, ge=0, le=1900)
    banner_top: Optional[int] = Field(default=None, ge=0, le=1800)

    @field_validator("zone")
    @classmethod
    def _inside(cls, v):
        if v is not None and not (all(0.0 <= x <= 1.0 for x in v) and v[0] < v[2] and v[1] < v[3]):
            raise ValueError("zone must be (x0, y0, x1, y1) inside the frame")
        return v


class RetryBody(BaseModel):
    multi: Optional[Literal["switch", "split", "off"]] = None
    resume_from: Optional[Literal["auto", "plan", "frame", "render"]] = None      # continue from a step this job already finished (the job's "resumable" lists them); None = start over


class BrandName(BaseModel):
    name: str = Field(min_length=1, max_length=40)


class BrandPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=40)
    default: bool = False


class CleanupReview(BaseModel):
    applied: list[int] = Field(max_length=2000)             # the numbers of the cuts that must be cut (all the others stay in)


class CodeRequest(BaseModel):
    email: str


class CodeVerify(BaseModel):
    email: str
    code: str


class ConfirmEmail(BaseModel):
    email: str                      # typed again to confirm deleting the account


class SocioBirdKey(BaseModel):
    key: str = Field(min_length=8, max_length=300)


class SettingsBody(BaseModel):
    values: dict


class AdminCredits(BaseModel):
    minutes: float = Field(ge=-10000, le=10000)                  # added (positive) or removed (negative), at most 10,000 at a time
    note: str = Field(min_length=3, max_length=200)             # WHY (required): "paid by UPI ref 123", "goodwill after a failed job" ... kept in the ledger


class ClaimJobs(BaseModel):
    email: str = Field(max_length=254)                           # the account that receives every job that has no owner


class ViewAs(BaseModel):
    user_id: str = Field(pattern=r"^[a-f0-9]{32}$")        # the Rust regex engine: $ does not match before a trailing newline


class AdminPlan(BaseModel):
    plan: str


class AdminDisable(BaseModel):
    disabled: bool


class NotifyBody(BaseModel):
    job_done: bool


class PlanChoice(BaseModel):
    plan: str


class PackChoice(BaseModel):
    pack: str


class Recharge(BaseModel):
    rupees: int = Field(ge=1, le=10_000_000)


def _clean_reading(r) -> dict:
    """What the editing pass said it understood (shown to a person): its intent, the passages it saw, and the result of its final read-through."""
    if not isinstance(r, dict):
        return {}
    out = {"intent": str(r.get("intent") or "")[:600], "passages": [], "verify": None}
    for p in (r.get("passages") or [])[:60]:
        if isinstance(p, dict) and isinstance(p.get("start"), (int, float)) and isinstance(p.get("end"), (int, float)):
            out["passages"].append({"start": float(p["start"]), "end": float(p["end"]), "role": str(p.get("role") or "")[:80]})
    v = r.get("verify")
    if isinstance(v, dict):
        out["verify"] = {"reads_cleanly": bool(v.get("reads_cleanly")), "problems": [str(x)[:200] for x in (v.get("problems") or [])[:8]]}
    return out


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


class PublishBulk(BaseModel):
    clips: list[str] = Field(min_length=1, max_length=BULK_CLIPS)
    channels: list[str] = Field(min_length=1, max_length=BULK_CHANNELS)
    per_day: int = Field(default=2, ge=1, le=MAX_PER_DAY)
    start: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")          # the first day, in the person's time zone (default: today)
    timezone: Optional[str] = Field(default=None, max_length=64)
    times: Optional[dict[str, list[str]]] = None                                       # best times to use now (platform -> HH:MM list), not saved
    suffix: str = Field(default="", max_length=300)                                    # added under every caption: hashtags, a link
    privacy: Literal["public", "unlisted", "private"] = "public"
    made_for_kids: bool = False
    ai_confirmed: bool = False
    dry_run: bool = False


class SchedulePrefsBody(BaseModel):
    timezone: str = Field(max_length=64)
    times: dict[str, list[str]] = Field(default_factory=dict)


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


def derive_lengths(lo: Optional[int], hi: Optional[int]) -> tuple[int, int]:
    """The reel length range from what a person gave: both, or one end (the other follows from it), or neither (the usual 15-60 s, which a caller that
    wants the whole video overrides with ``whole``)."""
    if lo and hi:
        return lo, hi
    if lo:
        return lo, min(900, max(lo * 2, lo + 30))
    if hi:
        return max(5, min(hi - 5, round(hi / 3))), hi
    return 15, 60


def _opt_float(v, default: float) -> float:
    return float(v) if v not in (None, "") else default


def create_app(data_dir: str | Path, pipeline: Pipeline, *, max_upload_mb: int = 4096,
               demo: bool = False, start_runner: bool = True, accounts: Accounts | None = None,
               catalog: Catalog | None = None, allow_signup: bool = True, razorpay: Razorpay | None = None,
               sociobird: SocioBird | None = None, storage: S3Storage | None = None,
               storage_redirect: bool = False, backup_source: bool = False, local_clip_days: Optional[float] = None,
               worker_token: str | None = None, worker_pickup_seconds: float = 120.0, worker_lease_seconds: float = 90.0,
               mailer: Mailer | None = None, public_url: str = "", limits: Limits | None = None, alert_email: str = "", sample_dir: str = "", sociobird_factory: Callable[[str], SocioBird] | None = None,
               sociobird_oauth_exchange: Callable[[str], str] | None = None) -> FastAPI:
    """``accounts``: multi-user mode (sign-up by emailed code, per-user jobs, minutes metered against the plan). Without it the app is open and single-user: only for
    this machine (the CLI refuses a public address without accounts). There is no password and no shared token: the emailed code is the only way in."""
    docs = DocStore(accounts.db) if accounts else None                      # records that can live in the database (CLIPCROW_STORE_<NAME>); only a server with accounts has one
    modes = {n: store_mode(n, has_db=docs is not None) for n in ('jobs', 'uploads', 'brand', 'style', 'schedule', 'settings')}
    if any(m != 'files' for m in modes.values()):
        log.info('record stores: %s', ', '.join(f'{k}={v}' for k, v in modes.items()))
    store = JobStore(data_dir, docs, modes['jobs'])
    catalog = catalog or load_catalog()
    runner = JobRunner(store, pipeline)

    mailq = MailQueue(mailer, sync=isinstance(mailer, Outbox)) if mailer else None
    limits = limits or Limits.from_env()
    admins = admin_emails()
    base_url = (public_url or os.environ.get("CLIPCROW_PUBLIC_URL", "")).strip().rstrip("/")      # links in mail come from here, never from a request header

    def finish(job: Job) -> None:
        """A job that failed or was cancelled gives its minutes back; a person who asked for it gets one mail when the job ends."""
        if accounts and job.owner and job.reserved_minutes and job.status in ("failed", "cancelled"):
            accounts.refund(job.owner, job.id)
        if accounts and mailq and job.owner and job.status in ("done", "failed"):
            try:
                owner = accounts.get(job.owner)
                if owner.notify_job_done:
                    mailq.send(job_done_mail(owner.email, job.status == "done", f"{base_url}/dashboard#/jobs/{job.id}" if base_url else ""))
            except (KeyError, ValueError):
                pass
    runner.on_finish = finish
    mirror = Mirror(storage, store, backup_source=backup_source) if storage else None
    if mirror:
        runner.on_change = mirror.submit
    # presigned downloads and playback come from the bucket's address: allow exactly that origin for media, nothing else
    csp = CSP.replace("media-src 'self' blob:", f"media-src 'self' blob: {storage.origin}") if storage else CSP
    direct = bool(storage) and os.environ.get("CLIPCROW_DIRECT_UPLOAD", "1") != "0"
    if direct:                                              # the browser PUTs file parts straight to the bucket
        csp = csp.replace("connect-src 'self'", f"connect-src 'self' {storage.origin}")
    uploads = UploadStore(data_dir, docs, modes['uploads']) if direct else None
    runner.storage = storage
    brands = BrandStore(data_dir, docs, modes['brand'])
    pictures = PictureStore(data_dir)
    runner.brands = brands
    styles = StyleStore(data_dir, docs, modes['style'])
    hub = WorkerHub(worker_token, pool_tokens={k: v for k, v in (('rented', os.environ.get('CLIPCROW_WORKER_TOKEN_RENTED', '').strip()),) if v}, pickup_seconds=worker_pickup_seconds, lease_seconds=worker_lease_seconds, render_pin=HYPERFRAMES_VERSION, render_build=render_build(),
                    disabled=frozenset(x.strip() for x in (os.environ.get("CLIPCROW_WORKER_DISABLED") or "").split(",") if x.strip()),
                    widen=(os.environ.get("CLIPCROW_GPU_FALLBACK") or "any").strip().lower() != "strict",
                    max_upload_bytes=int(float(os.environ.get("CLIPCROW_WORKER_MAX_UPLOAD_MB", "300") or 300) * 1024 * 1024),
                    max_direct_bytes=int(float(os.environ.get("CLIPCROW_WORKER_MAX_DIRECT_MB", "2048") or 2048) * 1024 * 1024),
                    storage=storage if os.environ.get("CLIPCROW_WORKER_DIRECT", "1") != "0" else None,
                    rate_for=lambda name: rate_lookup(load_rates())(name),                    # read again each time, so a rate changed on the Operator page applies at once
                    on_task_end=lambda r: store.add_gpu_usage(r["job"], {k: r[k] for k in ("kind", "worker", "seconds", "ok", "rate")} | {"at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
                    if store.exists(r["job"]) else None) if worker_token else None
    pod = None
    if hub is not None:
        from .gpu_pod import from_env as pod_from_env
        pod = pod_from_env(hub)                             # a rented pod that sleeps when idle and wakes for a waiting job (RACE_GPU_API_KEY + RACE_POD_ID)
        if pod is not None:
            pod.start()
    if hub is not None:
        pipeline.hub = hub                                  # RealPipeline sends speech recognition to a worker when one is online
    pipeline.store = store                                  # ... and reuses the transcript of a file this account already made a job from
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
    app.state.pod, app.state.hub = pod, hub
    app.state.outbox = mailer if isinstance(mailer, Outbox) else None            # tests read the sign-in code from here
    limit = max_upload_mb * 1024 * 1024

    def secure_cookie(request: Request) -> bool:
        return request.url.scheme == "https"          # correct behind the proxy because uvicorn trusts its X-Forwarded-Proto (cli.py)

    from ..httpsafe import install as no_redirects
    no_redirects()                                      # a server we send a key to cannot bounce the key elsewhere with a redirect
    from .bodyguard import BodyGuard
    from .compress import TextGzip
    app.add_middleware(TextGzip)
    app.add_middleware(BodyGuard, upload_mb=max_upload_mb, session_cookies=(SESSION_COOKIE,), require_sign_in=bool(accounts))

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = csp
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api"):
            resp.headers.setdefault("Cache-Control", "no-store")      # an endpoint that sets its own (fonts, tracks) keeps it
        elif "Cache-Control" not in resp.headers:      # the UI files: always revalidate (ETag), or a deploy leaves old JS in browsers
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    def effective_plan(user: User):
        """A paid plan that has run out (and not been renewed by a webhook yet) counts as free."""
        plan = catalog.plan(user.plan)
        if plan.id != "free" and user.plan_until and user.plan_until < datetime.now(timezone.utc).isoformat(timespec="seconds"):
            return catalog.plan("free")
        return plan

    VIEW_ONLY_BLOCKED = ("/api/sociobird", "/api/publish")          # a viewed account's social connection is never touched, not even to read

    def authed(request: Request) -> Optional[User]:
        if accounts:
            tok = request.cookies.get(SESSION_COOKIE, "")
            user = accounts.user_for_session(tok)
            if user is None:
                raise HTTPException(401, "Sign in first.")
            request.state.real_user, request.state.viewing = user, None
            if user.email in env_admins:                               # only a superadmin can be viewing another account
                target = accounts.impersonation_target(tok)
                if target is not None:
                    request.state.viewing = target
                    if request.method not in ("GET", "HEAD", "OPTIONS"):
                        raise HTTPException(403, "You are viewing this account read-only. Stop viewing to make changes.")
                    if request.url.path.startswith(VIEW_ONLY_BLOCKED):
                        raise HTTPException(403, "Posting and the social connection are not available while viewing another account.")
                    user = target
            request.state.user = user
            return user
        request.state.user = None                                    # no accounts: the app is open (this machine only)
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

    days_memo: dict[str, tuple[float, tuple[int, int]]] = {}

    def days_for(job: Job) -> tuple[int, int]:
        """(retention days, original-video days) for a job: its owner's plan, or the environment defaults."""
        if accounts and job.owner:
            hit = days_memo.get(job.owner)
            if hit and time.monotonic() - hit[0] < 5.0:             # a job list asks once per job; the plan of one person does not change within seconds
                return hit[1]
            try:
                p = effective_plan(accounts.get(job.owner))
                val = (p.retention_days, p.source_days)
            except KeyError:
                val = (0, 0)
            days_memo[job.owner] = (time.monotonic(), val)
            if len(days_memo) > 2000:
                days_memo.clear()
            return val
        return env_days()

    if start_runner and (accounts or any(env_days()) or storage):
        app.state.sweeper = Sweeper(store, days_for, storage=storage, mirror=mirror, local_clip_days=local_clip_days,
                                    extra=([lambda: getattr(app.state, "expire_uploads", lambda: None)()] if direct else []) + [pictures.expire] + ([lambda: accounts.backup(Path(data_dir) / "backups")] if accounts else []))          # resolved when it runs (the first run can come before create_app has defined it, then it is skipped)
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
        d["expires_at"] = None if (job.sample or job.keep) else expires_at(job, days)        # a sample is never expired by retention
        if job.status in ("failed", "cancelled"):                         # the steps a person can continue from (resume.py): a few file checks, only for a job that stopped
            from .resume import available
            d["resumable"] = available(job, store.dir(job.id) / "out")
        return d

    def charge_minutes(duration: float) -> int:
        """Minutes of balance a job of this length is charged: one a minute, at least the minimum charge (pay as you go)."""
        return max(catalog.payg.min_job_minutes if catalog.payg else 1, math.ceil(duration / 60), 1)

    def no_minutes(minutes: float, have: float, plan) -> str:
        if catalog.payg and plan.monthly_minutes == 0:
            return f"This video is {minutes} min long and your balance is {have:.0f} min. Recharge on your Account page, then start it again."
        return (f"This video is {minutes} min long and you have {have:.0f} min left on the {plan.name} plan this month. "
                f"Upgrade, buy minutes, or wait for the month to reset.")

    def enforce_limits(user: User, minutes: float, job_id: str) -> None:
        """A new job from this person must stay inside the cost limits (an operator is exempt). Raises the refusal, after which the caller discards the job."""
        if user.email in admins:
            return
        mine = [j for j in store.briefs() if j.owner == user.id and j.id != job_id]
        why = job_refusal(limits, active=sum(1 for j in mine if j.status in ("queued", "running")), today=jobs_since(mine, 24), minutes=minutes)
        if why:
            raise HTTPException(why[0], why[1])

    rates = RateLimiter()
    zip_slots = threading.BoundedSemaphore(2)

    def throttle(request: Request, what: str, limit: int, window_s: float, message: str) -> None:
        """Costly routes (a style analysis, a re-cut, a render) are limited per person: a free account must not be able to spend the operator's LLM and GPU budget in a loop."""
        user = getattr(request.state, "user", None)
        who = user.id if user else (request.client.host if request.client else "-")
        if not rates.allow(f"{what}:{who}", limit, window_s):
            raise HTTPException(429, message)

    # --- config and login ---------------------------------------------------------------
    @app.get("/api/config")
    def config(request: Request) -> dict:
        signed_in, email = True, None
        try:
            u = authed(request)
            email = u.email if u else None
        except HTTPException:
            signed_in = False
        is_admin = bool(email and email in admins)
        viewing = getattr(request.state, "viewing", None)
        return {"demo": demo, "languages": LANGUAGES, "max_upload_mb": max_upload_mb, "accounts": bool(accounts),
                "pricing": catalog.payg.view() if catalog.payg else None, "signup": bool(accounts) and allow_signup, "signup_paused": bool(accounts) and allow_signup and accounts.signups_today() >= limits.signups_per_day,
                "email": email, "admin": bool(email and email in admins), "mail": bool(mailer), "billing": bool(accounts and razorpay), "direct_upload": direct, "publishing": bool(accounts) or bool(sociobird),
                "needs_login": bool(accounts), "signed_in": signed_in, "viewing_as": viewing.email if viewing else None, "ready": pipeline.check_ready() if (signed_in or not accounts) else None,
                "ai_images": bool(getattr(pipeline, "can_generate_images", False)),
                "gpu_worker": {"enabled": hub is not None, "online": bool(hub and hub.online()),
                               "render": bool(hub and hub.online("render")),
                               # the numbers behind the cost estimate and the pod's spend are for people who are signed in / the operator, not for the open internet
                               **({"stale": hub.stale_workers()} if (is_admin or not accounts) and hub and hub.stale_workers() else {}),
                               **({"estimate": estimate_numbers(load_rates(), pod.hourly if pod is not None else None)} if signed_in else {}),
                               **({"pod": pod.status() if is_admin else {"state": pod.state}} if pod is not None and signed_in else {})}}

    # --- accounts -------------------------------------------------------------------------------
    def set_session(resp: JSONResponse, request: Request, user: User) -> JSONResponse:
        resp.set_cookie(SESSION_COOKIE, accounts.new_session(user.id, days=SUPERADMIN_SESSION_DAYS if user.email in env_admins else None), httponly=True, samesite="strict",
                        max_age=int((SUPERADMIN_SESSION_DAYS if user.email in env_admins else 30) * 86400), secure=secure_cookie(request))
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
                "credit_minutes": round(user.credits, 1), "plan_until": user.plan_until, "subscribed": bool(user.subscription),
                "notify_job_done": user.notify_job_done, "mail": bool(mailer), "admin": user.email in admins}

    sample_path = (sample_dir or os.environ.get("CLIPCROW_SAMPLE_DIR", "")).strip()

    def identity_hash(email: str) -> str:
        """The same mailbox under its usual spellings: lowercase, no +tag, and no dots in a Gmail address."""
        local, _, domain = email.strip().lower().partition("@")
        local = local.split("+", 1)[0]
        if domain in ("gmail.com", "googlemail.com"):
            local, domain = local.replace(".", ""), "gmail.com"
        return hashlib.sha256(f"{local}@{domain}".encode()).hexdigest()

    def welcome(user: User) -> None:
        """A new account gets the sample job (when the operator made one) and one welcome mail. Neither may ever fail the sign-in."""
        link, bonus = "", (catalog.payg.signup_bonus_minutes if catalog.payg else 0)
        if bonus and accounts.first_time("bonus:" + identity_hash(user.email)):
            accounts.add_credits(user.id, bonus)                       # once per PERSON: deleting the account and signing up again, or a+1@gmail.com, does not earn it again
        try:
            if sample_path:
                job = import_sample(store, sample_path, user.id)
                if job is not None and base_url:
                    link = f"{base_url}/dashboard#/jobs/{job.id}"
        except Exception:                                              # noqa: BLE001
            log.exception("sample job for a new account failed")
        if mailq:
            try:
                mailq.send(welcome_mail(user.email, effective_plan(user).monthly_minutes, base_url, link, bonus))
            except ValueError:
                pass

    @app.post("/api/auth/code")
    def sign_in_code(body: CodeRequest, request: Request) -> JSONResponse:
        """Step one of signing in (and of signing up): mail a 6-digit code. The answer is the same for every well-formed address."""
        need_accounts()
        if not mailq:
            raise HTTPException(503, "Email sign-in is not set up on this server. Ask the operator to turn on email.")
        known = accounts.by_email(body.email)
        if known is None and is_throwaway(body.email):
            raise HTTPException(422, "Please use a permanent email address, not a temporary one.")
        open_now = allow_signup and accounts.signups_today() < limits.signups_per_day          # new accounts pause for the day at the cap; existing ones still sign in
        try:
            code = accounts.request_code(body.email, who=request.client.host if request.client else "", allow_signup=open_now)
        except AccountError as e:
            raise HTTPException(429 if "Too many" in str(e) else 422, str(e))
        if code:
            try:
                mailq.send(sign_in_code_mail(body.email.strip().lower(), code, CODE_MINUTES))
            except ValueError:
                raise HTTPException(422, "Enter a valid email address.")
        return JSONResponse({"ok": True, "wait": CODE_WAIT_S, "minutes": CODE_MINUTES})

    @app.post("/api/auth/verify")
    def sign_in_verify(body: CodeVerify, request: Request) -> JSONResponse:
        need_accounts()
        first_time = accounts.by_email(body.email) is None
        try:
            user = accounts.verify_code(body.email, body.code, who=request.client.host if request.client else "", allow_signup=allow_signup)
        except AccountError as e:
            raise HTTPException(429 if "Too many" in str(e) else 401, str(e))
        if first_time:
            welcome(user)
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

    @app.put("/api/me/notifications", dependencies=[Depends(authed)])
    def set_notifications(body: NotifyBody, request: Request) -> dict:
        need_accounts().set_notify(request.state.user.id, body.job_done)
        return {"job_done": body.job_done}

    @app.delete("/api/account", dependencies=[Depends(authed)])
    def delete_account(body: ConfirmEmail, request: Request) -> JSONResponse:
        user = request.state.user
        need_accounts()
        if not hmac.compare_digest((body.email or "").strip().lower().encode(), user.email.encode()):
            raise HTTPException(403, "That is not the email of this account.")
        if any(j.status in ("queued", "running") for j in store.briefs() if j.owner == user.id):
            raise HTTPException(409, "Cancel your running jobs first.")
        if user.subscription and razorpay:                       # stop the charges FIRST; if that fails, delete nothing
            try:
                razorpay.cancel_subscription(user.subscription, at_cycle_end=False)
            except BillingError:
                raise HTTPException(UPSTREAM, "Your subscription could not be cancelled just now, so the account was not deleted. "
                                         "Try again in a minute.")
        for j in store.briefs():
            if j.owner == user.id:
                purge_remote(j.id, "Your files could not be removed from storage just now, so the account was not deleted. Try again in a minute.")
                store.delete(j.id)
        brands.delete_all(user.id)
        pictures.delete_all(user.id)
        styles.delete_all(user.id)
        schedule_prefs.delete_all(user.id)
        accounts.delete(user.id)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    # --- the operator: health, alerts and the admin view --------------------------------------------------
    @app.get("/api/health")
    def health() -> dict:
        """A plain "is it up" answer for an uptime monitor and for a deploy check: ``busy`` is only whether a job is running or waiting (no counts, no names)."""
        return {"ok": True, "busy": any(b.status in ("queued", "running") for b in store.briefs())}

    def snapshot() -> Snapshot:
        free_gb, free_pct = disk_snapshot(str(Path(data_dir)))
        jobs = store.list()
        waiting = [j for j in jobs if j.status == "queued"]
        oldest = 0.0
        for j in waiting:
            try:
                made = datetime.fromisoformat(j.created_at)
                oldest = max(oldest, (datetime.now(timezone.utc) - (made if made.tzinfo else made.replace(tzinfo=timezone.utc))).total_seconds())
            except ValueError:
                pass
        stuck = [t for t in (hub.unserved() if hub is not None else []) if t.get("age", 0) > 300]
        return Snapshot(disk_free_gb=free_gb, disk_free_pct=free_pct, queued=len(waiting), running=sum(1 for j in jobs if j.status == "running"),
                        oldest_queued_s=oldest, failed_last_hour=jobs_since(jobs, 1, statuses=("failed",)), unserved_tasks=len(stuck),
                        mail_failures_hour=mailq.failures_last_hour() if mailq else 0)

    alert_to = (alert_email or os.environ.get("CLIPCROW_ALERT_EMAIL", "")).strip()
    if start_runner and mailq:                                         # runs always; it sends only while an alert address is set (it can be set on the Operator page)
        app.state.watchdog = Watchdog(snapshot, lambda subject, body: mailq.send(plain_mail(alert_to, subject, [body], "alert")) if alert_to else None)
        app.state.watchdog.start()

    cost_memo: dict[str, tuple[int, float]] = {}

    def job_cost(job_id: str) -> Optional[dict]:
        """What making this job cost, in rupees: the AI at the model's list price plus the metered GPU time. None when the plan is not on disk."""
        path = store.dir(job_id) / "out" / "edl.json"
        if not path.is_file():
            return None
        stamp = path.stat().st_mtime_ns
        hit = cost_memo.get(job_id)
        if hit and hit[0] == stamp:                                    # the plan holds the whole transcript: parse it once per change, not on every admin page
            usd = hit[1]
        else:
            try:
                edl = load_edl(path)
            except Exception:                                          # noqa: BLE001 - one unreadable plan must not break the page
                return None
            usd = estimate_total(edl.ai.calls) if edl.ai else 0
            cost_memo[job_id] = (stamp, usd)
            if len(cost_memo) > 3000:
                cost_memo.clear()
        rate = max(1.0, float(os.environ.get("CLIPCROW_USD_INR") or 88.0))
        gpu = gpu_summary(store.gpu_usage(job_id)).get("est_cost", 0.0)
        return {"ai": round((usd or 0) * rate, 2), "gpu": round(gpu, 2), "total": round((usd or 0) * rate + gpu, 2)}

    def price_per_minute() -> float:
        return catalog.payg.price_paise_per_minute / 100 if catalog.payg else 0.0

    # --- operator settings: change them without a redeploy ------------------------------------------------------
    env_admins = frozenset(admins)
    ENV_BACKED = {"target_markup_percent": "CLIPCROW_MARGIN_PERCENT", "usd_inr": "CLIPCROW_USD_INR", "gpu_rates": "CLIPCROW_GPU_RATES",
                  "blocked_email_domains": "CLIPCROW_BLOCKED_EMAIL_DOMAINS", "model": "CLIPCROW_MODEL", "framing_threads": "CLIPCROW_FRAMING_THREADS",
                  "retention_days": "CLIPCROW_RETENTION_DAYS", "source_days": "CLIPCROW_SOURCE_DAYS"}
    env_start = {name: os.environ.get(name) for name in ENV_BACKED.values()}                 # what the environment said at start, to go back to
    g0, free0 = catalog.payg, catalog.plan("free")
    tiers0 = sorted(g0.bonus_tiers) if g0 else []
    from ..planner.llm import DEFAULT_MODEL

    def envnum(name: str, fallback: float) -> float:
        try:
            return float(env_start.get(name) or fallback)
        except ValueError:
            return fallback
    base_values = {
        "price_per_minute": g0.price_paise_per_minute / 100 if g0 else 8.0, "min_job_minutes": g0.min_job_minutes if g0 else 3,
        "signup_bonus_minutes": g0.signup_bonus_minutes if g0 else 0.0, "min_recharge": (g0.min_recharge_paise // 100) if g0 else 100,
        "max_recharge": (g0.max_recharge_paise // 100) if g0 else 20000,
        "tier1_from": tiers0[0][0] // 100 if len(tiers0) > 0 else 0, "tier1_percent": tiers0[0][1] if len(tiers0) > 0 else 0,
        "tier2_from": tiers0[1][0] // 100 if len(tiers0) > 1 else 0, "tier2_percent": tiers0[1][1] if len(tiers0) > 1 else 0,
        "plan_name": free0.name, "target_markup_percent": int(target_markup()), "usd_inr": envnum("CLIPCROW_USD_INR", 88.0),
        "gpu_rates": env_start.get("CLIPCROW_GPU_RATES") or "", "signup_open": allow_signup, "signups_per_day": limits.signups_per_day,
        "blocked_email_domains": env_start.get("CLIPCROW_BLOCKED_EMAIL_DOMAINS") or "", "extra_admins": "",
        "max_active_jobs": limits.active_jobs, "max_jobs_per_day": limits.jobs_per_day, "max_job_minutes": limits.job_minutes, "max_upload_mb": max_upload_mb,
        "retention_days": free0.retention_days if accounts else int(envnum("CLIPCROW_RETENTION_DAYS", 0)),
        "source_days": free0.source_days if accounts else int(envnum("CLIPCROW_SOURCE_DAYS", 0)),
        "gpu_fallback": "any" if (hub is None or hub.widen) else "strict", "disabled_workers": ", ".join(sorted(hub.disabled)) if hub is not None else "",
        "worker_pickup_seconds": int(hub.pickup_seconds) if hub is not None else 120, "pod_idle_minutes": int(pod.idle_seconds / 60) if pod is not None else 10,
        "pod_max_daily_inr": float(pod.max_daily) if pod is not None else 0.0, "alert_email": alert_to,
        "model": env_start.get("CLIPCROW_MODEL") or DEFAULT_MODEL, "framing_threads": int(envnum("CLIPCROW_FRAMING_THREADS", min(4, os.cpu_count() or 2))),
    }
    opsettings.cross_check(base_values)
    opset = opsettings.SettingsStore(Path(data_dir) / "settings.json", base_values, docs, modes['settings'])

    def apply_settings() -> None:
        """Put the current settings to work: the live objects change, so no restart is needed."""
        nonlocal limits, allow_signup, admins, max_upload_mb, limit, alert_to
        v, over = opset.values(), opset.overridden()
        if catalog.payg is not None:
            tiers = tuple(sorted((int(v[f"tier{n}_from"]) * 100, int(v[f"tier{n}_percent"])) for n in (1, 2) if v[f"tier{n}_from"] and v[f"tier{n}_percent"]))
            catalog.payg = Payg(price_paise_per_minute=max(1, round(v["price_per_minute"] * 100)), min_job_minutes=int(v["min_job_minutes"]),
                                min_recharge_paise=int(v["min_recharge"]) * 100, max_recharge_paise=int(v["max_recharge"]) * 100,
                                signup_bonus_minutes=float(v["signup_bonus_minutes"]), bonus_tiers=tiers)
        free = catalog.plan("free")
        catalog.plans["free"] = dataclasses.replace(free, name=v["plan_name"] or free.name, retention_days=int(v["retention_days"]), source_days=int(v["source_days"]))
        limits = Limits(active_jobs=int(v["max_active_jobs"]), jobs_per_day=int(v["max_jobs_per_day"]), job_minutes=int(v["max_job_minutes"]),
                        signups_per_day=int(v["signups_per_day"]))
        allow_signup = bool(v["signup_open"])
        admins = env_admins | frozenset(x.strip().lower() for x in v["extra_admins"].split(",") if x.strip())
        max_upload_mb = int(v["max_upload_mb"])
        limit = max_upload_mb * 1024 * 1024
        alert_to = v["alert_email"]
        if hub is not None:
            hub.widen = v["gpu_fallback"] != "strict"
            hub.disabled = frozenset(x.strip() for x in v["disabled_workers"].split(",") if x.strip())
            hub.pickup_seconds = float(v["worker_pickup_seconds"])
        if pod is not None:
            pod.idle_seconds = float(v["pod_idle_minutes"]) * 60
            pod.max_daily = float(v["pod_max_daily_inr"])
        for key, name in ENV_BACKED.items():                           # settings that other code reads from the environment each time it needs them
            if key in over:
                os.environ[name] = str(v[key])
            elif env_start[name] is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = env_start[name]
    apply_settings()

    def services() -> list[dict]:
        """Which outside services are connected: yes or no, never a value."""
        env = os.environ
        flag = lambda *names: all(bool(env.get(n)) for n in names)
        return [{"name": "Email", "set": bool(mailer), "detail": mailer.name if mailer else "off"},
                {"name": "AI planner (Anthropic)", "set": flag("ANTHROPIC_API_KEY")}, {"name": "Object storage (R2)", "set": storage is not None},
                {"name": "Payments (Razorpay)", "set": bool(razorpay)}, {"name": "Posting (SocioBird)", "set": bool(sociobird)},
                {"name": "GPU worker token", "set": bool(worker_token)}, {"name": "On-demand GPU (pod controller)", "set": pod is not None},
                {"name": "Public address for links in mail", "set": bool(base_url), "detail": base_url},
                {"name": "Plans file", "set": flag("CLIPCROW_PLANS")}, {"name": "Operators from the environment", "set": bool(env_admins), "detail": f"{len(env_admins)}"}]

    def settings_view(viewer: Optional[User] = None) -> dict:
        root = bool(viewer and viewer.email in env_admins)
        view_rows = [{**r, "locked": not root} for r in opset.view()]          # every setting is shown to operators, only a superadmin can change one
        return {"groups": [{"id": g, "title": t, "about": a} for g, t, a in opsettings.GROUPS], "settings": view_rows, "services": services(),
                "history": opset.history(), "accounts": bool(accounts), "billing": bool(razorpay), "superadmin": root}

    def admin_only(request: Request) -> User:
        user = authed(request)
        if user is None or user.email not in admins:
            raise HTTPException(404, "Not found.")                  # the page does not exist for anyone else
        return user

    def superadmin_only(admin: User = Depends(admin_only)) -> User:
        """A root operator: an address in CLIPCROW_ADMINS in the server's environment. Other operators (added on the settings page) can look, not change money."""
        if admin.email not in env_admins:
            raise HTTPException(403, "Only a superadmin can do this.")
        return admin

    def audit(request: Request, admin: User, action: str, target: str = "", note: str = "") -> None:
        """Every privileged action goes into ONE append-only trail (the audit log on the Operator page): who, what, which account, from which address. Written BEFORE the action,
        so if the trail cannot be written the action does not happen (fail closed)."""
        ip = request.client.host if request.client else ""
        accounts.audit(admin.email, action, target, f"{note} · ip {ip}" if ip else note)

    def ops_alert(subject: str, line: str) -> None:
        """Tell the alert address about a sensitive change (best effort: a mail problem never blocks or undoes the action)."""
        try:
            if mailq and alert_to:
                mailq.send(plain_mail(alert_to, subject, [line], "alert"))
        except Exception:                                                  # noqa: BLE001
            log.exception("could not send the operator alert")

    def real_superadmin(request: Request) -> User:
        """The signed-in person themselves (never the account being viewed), who must be a superadmin."""
        user = need_accounts().user_for_session(request.cookies.get(SESSION_COOKIE, ""))
        if user is None or user.email not in env_admins:
            raise HTTPException(404, "Not found.")
        return user

    @app.post("/api/admin/impersonate")
    def admin_impersonate(body: ViewAs, request: Request, admin: User = Depends(real_superadmin)) -> dict:
        """Look at another account exactly as they see it, to judge the quality of their jobs. Silent for them (nothing is sent or shown), read-only for you, limited to an hour,
        and every start and stop is written to the audit log below."""
        target = admin_target(body.user_id)
        if target.id == admin.id or target.email in env_admins:
            raise HTTPException(422, "Choose someone who is not a superadmin.")
        audit(request, admin, "view-as-start", target.email)
        accounts.start_impersonation(request.cookies.get(SESSION_COOKIE, ""), target.id)
        ops_alert("ClipCrow: an account is being viewed", "A superadmin started viewing an account read-only. It is in the audit log.")
        return {"email": target.email, "minutes": 60}

    @app.delete("/api/admin/impersonate")
    def admin_stop_impersonating(request: Request, admin: User = Depends(real_superadmin)) -> dict:
        tok = request.cookies.get(SESSION_COOKIE, "")
        target = accounts.impersonation_target(tok)
        if target is not None:
            audit(request, admin, "view-as-stop", target.email)
        accounts.stop_impersonation(tok)
        return {"ok": True}

    @app.get("/api/admin/audit")
    def admin_audit(admin: User = Depends(real_superadmin)) -> dict:
        return {"entries": accounts.audit_list()}

    @app.get("/api/admin/overview")
    def admin_overview(admin: User = Depends(admin_only)) -> dict:
        snap = snapshot()
        jobs = store.list()
        by_owner: dict[str, int] = {}
        for j in jobs:
            if j.owner:
                by_owner[j.owner] = by_owner.get(j.owner, 0) + 1
        users = accounts.list_users()
        for u in users:
            u["jobs_now"] = by_owner.get(u["id"], 0)
            u["is_superadmin"] = u["email"] in env_admins
            u["left_minutes"] = round(max(catalog.plan(u["plan"]).monthly_minutes - u["used_minutes"], 0) + u["credits"], 1)      # what they can still use: this month's allowance left plus extra minutes
        recent_failed = [{"id": j.id, "owner": next((u["email"] for u in users if u["id"] == j.owner), ""), "error": (j.error or "")[:160], "when": j.created_at}
                         for j in sorted((j for j in jobs if j.status == "failed"), key=lambda j: j.created_at, reverse=True)[:10]]
        owners = {u["id"]: u["email"] for u in users}
        rows, markup = [], target_markup()
        for j in [j for j in jobs if j.status == "done" and j.reserved_minutes and not j.sample][:30]:        # newest first
            c = job_cost(j.id)
            if c is None:
                continue
            e = job_economics(j.reserved_minutes * price_per_minute(), c["total"], markup)
            rows.append({"id": j.id, "owner": owners.get(j.owner or "", ""), "minutes": j.reserved_minutes, "video_minutes": round((j.source or {}).get("duration", 0) / 60, 1),
                         "cost_ai": c["ai"], "cost_gpu": c["gpu"], **e})
        per_min = [(r["cost_ai"] + r["cost_gpu"]) / r["video_minutes"] for r in rows if r["video_minutes"] >= 15]
        shorts = [r["cost_ai"] + r["cost_gpu"] for r in rows if r["video_minutes"] < 5]
        total_rev, total_cost = sum(r["revenue"] for r in rows), sum(r["cost"] for r in rows)
        money = {"jobs": rows, "price_per_minute": price_per_minute(), "min_job_minutes": catalog.payg.min_job_minutes if catalog.payg else 1,
                 "target_markup_percent": round(markup), "totals": job_economics(total_rev, total_cost, markup),
                 "suggested_price_per_minute": suggest_price(per_min, markup), "suggested_min_job_minutes": suggest_min_minutes(shorts, price_per_minute(), markup) if shorts else None,
                 "note": "Revenue is the list price of the minutes charged (free-trial minutes included); cost is the AI at list price plus metered GPU time. Server, storage and the payment fee are not counted."}
        return {"superadmin": admin.email in env_admins, "money": money, "users": users, "plans": list(catalog.plans), "signups_today": accounts.signups_today(), "signup_cap": limits.signups_per_day,
                "jobs": {s: sum(1 for j in jobs if j.status == s) for s in ("queued", "running", "done", "failed", "cancelled")},
                "disk_free_gb": round(snap.disk_free_gb, 1), "disk_free_pct": round(snap.disk_free_pct), "failed_last_hour": snap.failed_last_hour,
                "unserved_tasks": snap.unserved_tasks, "mail": mailer.name if mailer else "off", "mail_failures_hour": snap.mail_failures_hour,
                "gpu_worker": {"online": bool(hub and hub.online()), "render": bool(hub and hub.online("render"))},
                "pod": pod.status() if pod is not None else None,
                "limits": {"active_jobs": limits.active_jobs, "jobs_per_day": limits.jobs_per_day, "job_minutes": limits.job_minutes},
                "recent_failed": recent_failed}

    def admin_target(user_id: str) -> User:
        if not re.fullmatch(r"[0-9a-f]{32}\Z", user_id or ""):
            raise HTTPException(404, "No such account.")
        try:
            return accounts.get(user_id)
        except KeyError:
            raise HTTPException(404, "No such account.")

    @app.get("/api/admin/settings")
    def admin_settings(admin: User = Depends(admin_only)) -> dict:
        return settings_view(admin)

    @app.put("/api/admin/settings")
    def admin_settings_put(body: SettingsBody, request: Request, admin: User = Depends(superadmin_only)) -> dict:
        """Settings move money and who may do what (prices, limits, retention, the operator list), so only a superadmin changes them; other operators can read."""
        audit(request, admin, "settings", "", ", ".join(sorted(body.values))[:150])
        try:
            opset.update(body.values, admin.id)
        except ValueError as e:
            raise HTTPException(422, str(e))
        apply_settings()
        log.info("admin %s changed settings: %s", admin.id[:8], ", ".join(sorted(body.values))[:200])
        if "extra_admins" in body.values:
            ops_alert("ClipCrow: the list of operators changed", "A superadmin changed who else can open the Operator page. If that was not you, act now.")
        return settings_view(admin)

    @app.delete("/api/admin/settings/{key}")
    def admin_settings_reset(key: str, request: Request, admin: User = Depends(superadmin_only)) -> dict:
        audit(request, admin, "settings-reset", "", key[:60])
        try:
            opset.reset(key, admin.id)
        except ValueError as e:
            raise HTTPException(404, str(e))
        apply_settings()
        log.info("admin %s reset setting %s", admin.id[:8], key[:40])
        return settings_view(admin)

    @app.post("/api/admin/users/{user_id}/credits")
    def admin_credits(user_id: str, body: AdminCredits, request: Request, admin: User = Depends(superadmin_only)) -> dict:
        """Add (positive) or remove (negative) extra minutes by hand. Never below zero; every change is written to the person's ledger with who made it and why."""
        user = admin_target(user_id)
        if body.minutes == 0:
            raise HTTPException(422, "Enter a number of minutes to add or remove.")
        audit(request, admin, "credits", user.email, f"{body.minutes:+g} min: {body.note.strip()[:100]}")
        try:
            balance = accounts.adjust_credits(user.id, body.minutes, admin.email, body.note.strip())
        except AccountError as e:
            raise HTTPException(422, str(e))
        if abs(body.minutes) >= 1000:
            ops_alert("ClipCrow: a large credit change", f"{body.minutes:+g} minutes were added or removed on an account ({body.note.strip()[:100]}). It is in the audit log.")
        log.info("superadmin %s changed the credit minutes of %s by %s", admin.id[:8], user.id[:8], body.minutes)
        return {"credit_minutes": round(balance, 1)}

    @app.get("/api/admin/users/{user_id}/credits")
    def admin_credit_history(user_id: str, admin: User = Depends(admin_only)) -> dict:
        user = admin_target(user_id)
        return {"balance": round(accounts.get(user.id).credits, 1), "history": accounts.credit_history(user.id)}

    @app.post("/api/admin/users/{user_id}/plan")
    def admin_plan(user_id: str, body: AdminPlan, request: Request, admin: User = Depends(superadmin_only)) -> dict:
        user = admin_target(user_id)
        if body.plan not in catalog.plans:
            raise HTTPException(422, "No such plan.")
        audit(request, admin, "plan", user.email, f"{user.plan} -> {body.plan}")
        accounts.set_plan(user.id, body.plan, until=None)
        log.info("admin %s set the plan of %s to %s", admin.id[:8], user.id[:8], body.plan)
        return {"plan": body.plan}

    @app.post("/api/admin/users/{user_id}/disabled")
    def admin_disable(user_id: str, body: AdminDisable, request: Request, admin: User = Depends(admin_only)) -> dict:
        user = admin_target(user_id)
        if user.id == admin.id:
            raise HTTPException(422, "You cannot disable your own account.")
        if user.email in env_admins and body.disabled:
            raise HTTPException(422, "A root operator (set in the server's environment) cannot be disabled here.")
        audit(request, admin, "disable" if body.disabled else "enable", user.email)
        accounts.set_disabled(user.id, body.disabled)
        log.info("admin %s %s %s", admin.id[:8], "disabled" if body.disabled else "enabled", user.id[:8])
        return {"disabled": body.disabled}

    # --- storage: what is on the server's disk, what the bucket holds, old jobs ---------------------------------------
    cleanup_lock = threading.Lock()

    def jobs_disk_usage() -> dict:
        """Bytes under the jobs folder by kind: the uploaded originals (and the cleaned cut made from them), the finished clips, and everything else."""
        out = {"originals": 0, "clips": 0, "other": 0}
        for job_dir in store.root.iterdir():
            for dirpath, _dirs, files in os.walk(job_dir):
                for name in files:
                    try:
                        n = os.lstat(os.path.join(dirpath, name)).st_size
                    except OSError:
                        continue
                    if name.startswith("source.") or name == "clean.mp4":
                        out["originals"] += n
                    elif re.fullmatch(r"c\d{1,3}\.mp4", name):
                        out["clips"] += n
                    else:
                        out["other"] += n
        return out

    def storage_report() -> dict:
        free_gb, free_pct = disk_snapshot(str(Path(data_dir)))
        jobs = [j for j in store.list() if j.status not in ("queued", "running")]
        removable = {"removed": 0, "bytes": 0, "kept": 0, "kept_bytes": 0}
        if storage:
            for j in jobs:
                r = evict_local_clips(store, j, storage, dry_run=True)
                for k in removable:
                    removable[k] += r[k]
        usage = jobs_disk_usage()
        unowned = sum(1 for j in store.list() if not j.owner and not j.sample) if accounts else 0
        gb = lambda n: round(n / 2 ** 30, 2)
        return {"disk_free_gb": round(free_gb, 1), "disk_free_pct": round(free_pct), "r2": storage is not None, "mirror": mirror is not None,
                "local_clip_days": local_clip_days, "jobs_gb": {k: gb(v) for k, v in usage.items()},
                "clips_safe_to_remove": removable["removed"], "clips_safe_gb": gb(removable["bytes"]),
                "clips_not_in_bucket": removable["kept"], "clips_not_in_bucket_gb": gb(removable["kept_bytes"]),
                "unowned_jobs": unowned, "superadmin": False}

    @app.get("/api/admin/storage")
    def admin_storage(admin: User = Depends(admin_only)) -> dict:
        return {**storage_report(), "superadmin": admin.email in env_admins}

    @app.post("/api/admin/storage/cleanup")
    def admin_storage_cleanup(request: Request, admin: User = Depends(superadmin_only)) -> dict:
        """Remove from the server the clip files that have an identical copy in the bucket. Each one is checked with the bucket first (the object must be there, same size)."""
        if not storage:
            raise HTTPException(409, "Object storage is not set up, so there is nothing to rely on: no file was removed.")
        audit(request, admin, "storage-cleanup")
        if not cleanup_lock.acquire(blocking=False):
            raise HTTPException(409, "A cleanup is already running.")
        try:
            total = {"removed": 0, "bytes": 0, "kept": 0, "kept_bytes": 0}
            for j in store.list():
                if j.status in ("queued", "running"):
                    continue
                r = evict_local_clips(store, j, storage, verify=True)
                for k in total:
                    total[k] += r[k]
        finally:
            cleanup_lock.release()
        log.info("superadmin %s removed %s local clip files (%s bytes) that are safe in the bucket; %s kept", admin.id[:8], total["removed"], total["bytes"], total["kept"])
        return {"removed": total["removed"], "freed_gb": round(total["bytes"] / 2 ** 30, 2), "kept": total["kept"], "kept_gb": round(total["kept_bytes"] / 2 ** 30, 2)}

    @app.post("/api/admin/storage/upload-missing")
    def admin_storage_upload_missing(request: Request, admin: User = Depends(superadmin_only)) -> dict:
        if mirror is None:
            raise HTTPException(409, "Object storage is not set up.")
        audit(request, admin, "storage-upload-missing")
        return {"queued": mirror.reconcile()}

    @app.post("/api/admin/jobs/claim")
    def admin_claim_jobs(body: ClaimJobs, request: Request, admin: User = Depends(superadmin_only)) -> dict:
        """Give every job that has no owner (made before accounts) to one account, and keep them for ever: a job that belongs to someone is otherwise expired by that
        plan's retention, which would delete jobs that are older than it."""
        need_accounts()
        try:
            who = accounts.by_email(body.email.strip().lower())
        except Exception:                                                  # noqa: BLE001
            who = None
        if who is None:
            raise HTTPException(404, "No account has that address yet. Sign in once with it first.")
        audit(request, admin, "claim-jobs", who.email)
        n = 0
        for j in store.list():
            if j.owner or j.sample or j.status in ("queued", "running"):
                continue
            def give(job, uid=who.id):
                job.owner, job.keep = uid, True
            store.update(j.id, give)
            n += 1
        log.info("superadmin %s gave %s old jobs to account %s", admin.id[:8], n, who.id[:8])
        return {"assigned": n}

    # --- publishing (SocioBird) -------------------------------------------------------------------
    publish_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="clipcrow-publish")

    SB_BASE = (os.environ.get("SOCIOBIRD_API_URL") or "https://go.sociobird.app/api").strip()
    make_sociobird = sociobird_factory or (lambda key: SocioBird(SB_BASE, key))
    NOT_CONNECTED = "Connect your SocioBird account first: open your Account page and add it under Posting."

    def need_publishing(request: Request) -> SocioBird:
        """The SocioBird client for THIS person: built from their own connection (OAuth, or a key they pasted), never from the server's key. With accounts on there is no
        shared fallback of any kind, so nobody ever sees or posts to anyone else's channels. Without accounts (this machine only) it is the server's one client."""
        user = getattr(request.state, "user", None)
        if accounts:
            key = accounts.link_token(user.id) if user is not None else None
            if key:
                return make_sociobird(key)
            raise HTTPException(409, NOT_CONNECTED)
        if not sociobird:
            raise HTTPException(404, "Posting to social channels is not set up on this server.")
        return sociobird

    def refused(user, e: PublishError) -> None:
        """SocioBird turned our request down with 401 or 403: the key was revoked or the plan has no API. Remember it so the Account page says reconnect."""
        if accounts and user is not None and re.search(r"\((401|403)\)", str(e)):
            accounts.mark_link(user.id, False, "SocioBird refused the connection")
            raise HTTPException(409, "SocioBird refused your connection (the key was removed, or your SocioBird plan has no API access). Reconnect it on your Account page.")

    def sociobird_status(user: User) -> dict:
        link = accounts.get_link(user.id) if accounts else None
        return {"connected": bool(link), "kind": link["kind"] if link else "", "label": link["label"] if link else "", "since": link["connected_at"] if link else "",
                "last_error": link["last_error"] if link else "", "oauth": bool(oauth_id)}

    @app.get("/api/sociobird", dependencies=[Depends(authed)])
    def sociobird_get(request: Request) -> dict:
        need_accounts()
        return sociobird_status(request.state.user)

    @app.post("/api/sociobird/key", dependencies=[Depends(authed)])
    def sociobird_connect(body: SocioBirdKey, request: Request) -> dict:
        """Connect with a person's own SocioBird API key. It is tried once (listing their channels) before anything is stored, then stored encrypted."""
        user = request.state.user
        need_accounts()
        key = body.key.strip()
        if re.search(r"[\s\x00-\x1f]", key):
            raise HTTPException(422, "That does not look like a SocioBird API key: it has spaces or line breaks.")
        try:
            channels = make_sociobird(key).channels()
        except PublishError as e:
            if re.search(r"\((401|403)\)", str(e)):
                raise HTTPException(422, "SocioBird did not accept that key. Copy it again from SocioBird (Settings, Public API); your SocioBird plan must include API access.")
            raise HTTPException(UPSTREAM, str(e))
        accounts.set_link(user.id, "key", key, f"{len(channels)} channel{'s' if len(channels) != 1 else ''}")
        return sociobird_status(user)

    # --- connecting with SocioBird's OAuth (authorisation code) ------------------------------------------------------
    # The operator registers ClipCrow once in SocioBird (Developer settings) with the redirect URL <public address>/api/sociobird/callback and sets
    # SOCIOBIRD_OAUTH_CLIENT_ID and SOCIOBIRD_OAUTH_CLIENT_SECRET. Not yet tried against the live SocioBird (docs/sociobird-per-user-task.md, step 0).
    oauth_id = os.environ.get("SOCIOBIRD_OAUTH_CLIENT_ID", "").strip()
    oauth_web = (os.environ.get("SOCIOBIRD_OAUTH_URL") or "https://go.sociobird.app").rstrip("/")
    oauth_token_url = os.environ.get("SOCIOBIRD_OAUTH_TOKEN_URL") or f"{SB_BASE}/oauth/token"

    def exchange_code(code: str) -> str:
        """Swap the authorisation code for SocioBird's access token (server to server; the client secret never leaves the server). Raises PublishError."""
        secret = os.environ.get("SOCIOBIRD_OAUTH_CLIENT_SECRET", "")
        if not (oauth_id and secret):
            raise PublishError("SocioBird sign-in is not set up on this server.")
        from ..httpsafe import https_or_loopback
        if not https_or_loopback(oauth_token_url):
            raise PublishError("SocioBird sign-in is set up with an address that is not https, so the secret was not sent.")
        data = json.dumps({"grant_type": "authorization_code", "code": code, "client_id": oauth_id, "client_secret": secret}).encode()
        req = urllib.request.Request(oauth_token_url, data=data, method="POST", headers={"Content-Type": "application/json", "User-Agent": "ClipCrow/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:                        # noqa: S310 (the operator's configured https address)
                token = json.loads(r.read(100_000)).get("access_token")
        except (urllib.error.URLError, TimeoutError, ValueError):
            raise PublishError("SocioBird did not accept the sign-in.") from None
        if not isinstance(token, str) or not 8 <= len(token) <= 400:
            raise PublishError("SocioBird did not return an access token.")
        return token
    swap_code = sociobird_oauth_exchange or exchange_code

    @app.post("/api/sociobird/oauth/start", dependencies=[Depends(authed)])
    def sociobird_oauth_start(request: Request) -> dict:
        user = request.state.user
        need_accounts()
        if not oauth_id:
            raise HTTPException(404, "Signing in with SocioBird is not turned on here. Paste your API key instead.")
        state = accounts.new_oauth_state(user.id)
        return {"url": f"{oauth_web}/oauth/authorize?" + urllib.parse.urlencode({"client_id": oauth_id, "response_type": "code", "state": state})}

    @app.get("/api/sociobird/callback", include_in_schema=False)
    def sociobird_oauth_callback(code: str = "", state: str = "", error: str = "") -> RedirectResponse:
        """Where SocioBird sends the browser back. It always ends in a redirect to the Account page with ``?sociobird=connected`` or ``failed``."""
        back = lambda outcome: RedirectResponse(f"/dashboard?sociobird={outcome}#/account", status_code=303)
        uid = accounts.take_oauth_state(state[:200]) if accounts else None
        if uid is None or error or not code or len(code) > 500:
            return back("failed")
        try:
            token = swap_code(code)
            channels = make_sociobird(token).channels()
        except PublishError:
            log.warning("SocioBird sign-in failed for an account")
            return back("failed")
        accounts.set_link(uid, "oauth", token, f"{len(channels)} channel{'s' if len(channels) != 1 else ''}")
        return back("connected")

    @app.delete("/api/sociobird", dependencies=[Depends(authed)])
    def sociobird_disconnect(request: Request) -> dict:
        user = request.state.user
        need_accounts().delete_link(user.id)
        return sociobird_status(user)

    @app.get("/api/publish/channels", dependencies=[Depends(authed)])
    def publish_channels(request: Request) -> list[dict]:
        sb = need_publishing(request)
        try:
            out = [{"id": c.id, "name": c.name, "platform": c.platform, "brand": c.brand, "limit": LIMITS.get(c.platform, 2000)} for c in sb.channels()]
            if accounts and request.state.user is not None:
                accounts.mark_link(request.state.user.id, True)
            return out
        except PublishError as e:
            refused(getattr(request.state, "user", None), e)
            raise HTTPException(UPSTREAM, str(e))

    @app.get("/api/jobs/{job_id}/clips/{clip_id}/posts", dependencies=[Depends(authed)])
    def clip_posts(job_id: str, clip_id: str, request: Request) -> list[dict]:
        job_or_404(job_id, request)
        return store.posts(job_id, clip_id)

    def postable_clip(job_id: str, job: Job, clip_id: str):
        """``(info, path, ai_media)`` of a finished clip, fetching its file back from storage when it was evicted from the disk."""
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
        return info, path, ai_media

    schedule_prefs = SchedulePrefs(data_dir, docs, modes['schedule'])

    @app.get("/api/publish/schedule/prefs", dependencies=[Depends(authed)])
    def schedule_prefs_get(request: Request) -> dict:
        need_publishing(request)
        return schedule_prefs.get(brand_owner(request))

    @app.put("/api/publish/schedule/prefs", dependencies=[Depends(authed)])
    def schedule_prefs_put(body: SchedulePrefsBody, request: Request) -> dict:
        need_publishing(request)
        try:
            return schedule_prefs.put(brand_owner(request), body.timezone, body.times)
        except ScheduleError as e:
            raise HTTPException(422, str(e))

    @app.get("/api/jobs/{job_id}/posts", dependencies=[Depends(authed)])
    def job_posts(job_id: str, request: Request) -> list[dict]:
        job_or_404(job_id, request)
        return store.posts(job_id)

    @app.post("/api/jobs/{job_id}/publish/bulk", dependencies=[Depends(authed)], status_code=202)
    def publish_bulk(job_id: str, body: PublishBulk, request: Request) -> dict:
        """Schedule the chosen reels on the chosen channels, ``per_day`` reels a day on each channel, at its platform's best times. With ``dry_run`` only the
        plan is returned. Everything is checked before anything is queued."""
        sb = need_publishing(request)
        job = job_or_404(job_id, request)
        if len(set(body.clips)) != len(body.clips) or len(set(body.channels)) != len(body.channels):
            raise HTTPException(422, "Each reel and each channel can be chosen once.")
        if not all(re.fullmatch(r"c\d{1,3}", c) for c in body.clips):
            raise HTTPException(422, "That is not a reel of this job.")
        prefs = schedule_prefs.get(brand_owner(request))
        try:
            tz = schedule_zone(body.timezone or prefs["timezone"])
            times = dict(prefs["times"])
            for k, v in (body.times or {}).items():
                if k not in times:
                    raise ScheduleError("That platform is not supported.")
                times[k] = clean_times(v)
            now = datetime.now(timezone.utc)
            start = date.fromisoformat(body.start) if body.start else now.astimezone(tz).date()
            known = {c.id: c for c in sb.channels()}
        except PublishError as e:
            raise HTTPException(UPSTREAM, str(e))
        except (ScheduleError, ValueError) as e:
            raise HTTPException(422, str(e) if isinstance(e, ScheduleError) else "That date is not valid.")
        if start < now.astimezone(tz).date() - timedelta(days=1) or start > now.astimezone(tz).date() + timedelta(days=365):
            raise HTTPException(422, "Pick a first day from today to a year ahead.")
        chans = []
        for cid in body.channels:
            if cid not in known:
                raise HTTPException(422, "One of those channels is not available.")
            chans.append(Chan(cid, known[cid].name, known[cid].platform))
        infos, ai_clips = {}, []
        for cid in body.clips:
            info, path, ai_media = postable_clip(job_id, job, cid)
            infos[cid] = (info, path)
            if ai_media:
                ai_clips.append(cid)
        if ai_clips and not (body.ai_confirmed or body.dry_run):
            raise HTTPException(422, f"These reels include AI-generated pictures ({', '.join(ai_clips)}). Confirm that you will label them as AI content where the platform asks.")
        try:
            rows_plan = plan_schedule(body.clips, chans, body.per_day, start, tz, times, now)
        except ScheduleError as e:
            raise HTTPException(422, str(e))
        shown = [{**r, "title": infos[r["clip"]][0].title} for r in rows_plan]
        if body.dry_run:
            return {"dry_run": True, "timezone": str(tz), "posts": shown, "count": len(shown),
                    "days": len({r["local"][:10] for r in rows_plan}), "ai_confirm_needed": ai_clips}
        suffix = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", body.suffix or "").strip()
        work_items = []
        for r in rows_plan:
            info = infos[r["clip"]][0]
            ch = known[r["channel"]]
            caption = "\n".join(x for x in (info.title, getattr(info, "hook", "")) if x)
            if suffix:
                caption += "\n\n" + suffix
            try:
                st = settings_for(ch.platform, title=info.title, privacy=body.privacy, made_for_kids=body.made_for_kids, ai_generated=r["clip"] in ai_clips)
            except PublishError as e:
                raise HTTPException(422, str(e))
            work_items.append((ch, r, st, caption_html(caption, ch.platform)))
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows = [{"id": uuid.uuid4().hex[:12], "clip": r["clip"], "channel": ch.id, "name": ch.name, "platform": ch.platform,
                 "status": "queued", "detail": "", "when": r["when"], "at": stamp} for ch, r, _, _ in work_items]
        store.add_posts(job_id, rows)

        def bulk_work() -> None:
            log = logging.getLogger("clipcrow.service")
            media: dict[str, dict] = {}
            failed: dict[str, str] = {}
            for (ch, r, st, content), row in zip(work_items, rows):
                cid = r["clip"]
                if cid in failed:
                    store.update_post(job_id, row["id"], "failed", failed[cid])
                    continue
                if cid not in media:                                           # one upload per reel, however many channels
                    try:
                        media[cid] = sb.upload(infos[cid][1])
                    except PublishError as e:
                        failed[cid] = str(e)
                    except Exception:
                        log.exception("upload to SocioBird failed")
                        failed[cid] = "The video could not be uploaded."
                    if cid in failed:
                        store.update_post(job_id, row["id"], "failed", failed[cid])
                        continue
                try:
                    sb.post(ch, content, media[cid], st, when=datetime.fromisoformat(r["when"]))
                    store.update_post(job_id, row["id"], "scheduled")
                except PublishError as e:
                    store.update_post(job_id, row["id"], "failed", str(e))
                except Exception:
                    log.exception("post to SocioBird failed")
                    store.update_post(job_id, row["id"], "failed", "The post could not be created.")
        publish_pool.submit(bulk_work)
        return {"dry_run": False, "timezone": str(tz), "posts": [{**r, "title": infos[r["clip"]][0].title} for r in rows_plan], "count": len(rows), "rows": rows}

    @app.post("/api/jobs/{job_id}/clips/{clip_id}/publish", dependencies=[Depends(authed)], status_code=202)
    def clip_publish(job_id: str, clip_id: str, body: PublishBody, request: Request) -> list[dict]:
        sb = need_publishing(request)
        job = job_or_404(job_id, request)
        info, path, ai_media = postable_clip(job_id, job, clip_id)
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
                "packs": [{"id": k.id, "minutes": k.minutes, "label": k.label} for k in catalog.packs.values()],
                "payg": catalog.payg.view() if catalog.payg else None}

    @app.get("/api/pricing")
    def pricing() -> dict:
        """Public: the price and the recharge options, for the landing page and the New job page. Nothing about any person."""
        return {"payg": catalog.payg.view() if catalog.payg else None, "plan": {"name": catalog.plan("free").name, "retention_days": catalog.plan("free").retention_days}}

    @app.post("/api/billing/recharge", dependencies=[Depends(authed)])
    def recharge(body: Recharge, request: Request) -> dict:
        user, rz = request.state.user, need_billing()
        g = catalog.payg
        if g is None:
            raise HTTPException(404, "Recharging is not turned on.")
        paise = body.rupees * 100
        if not g.min_recharge_paise <= paise <= g.max_recharge_paise:
            raise HTTPException(422, f"Recharge between Rs {g.min_recharge_paise // 100} and Rs {g.max_recharge_paise // 100}.")
        try:
            link = rz.create_payment_link(paise, f"ClipCrow recharge Rs {body.rupees}", user.id, "custom")
        except BillingError as e:
            raise HTTPException(UPSTREAM, str(e))
        return {"url": link["url"], "minutes": g.minutes_for(paise)}

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
        event_id = hashlib.sha256(body).hexdigest()          # from the SIGNED body: the event-id header is not covered by the signature, so a replay could change it
        def tell(user, kind: str, detail: str) -> None:
            if mailq:
                mailq.send(billing_mail(user.email, kind, detail))
        return {"status": apply_event(event, event_id[:100], accounts, catalog, tell)}

    # --- jobs -----------------------------------------------------------------------------
    def truthy(v) -> bool:
        return v is True or str(v).lower() in ("1", "true", "on", "yes")

    def brand_owner(request: Request) -> str:
        user = getattr(request.state, "user", None)
        return user.id if accounts and user is not None else "shared"

    def build_params(o: dict, owner: str = "shared") -> Params:
        """The job options, from a form or from JSON (strings or typed values), checked once for both upload paths."""
        if str(o.get("links") or "").strip():
            raise HTTPException(422, "This server no longer opens web pages for you. Take a screenshot yourself and add it as a picture.")
        try:
            picture_ids = parse_ids(o.get("pictures"))
        except PictureError as e:
            raise HTTPException(422, str(e))
        if picture_ids and not truthy(o.get("explainer")):
            raise HTTPException(422, "Turn on the explainer option to use pictures.")
        if any(pictures.path(owner, pid) is None for pid in picture_ids):
            raise HTTPException(422, "A picture you added is no longer available. Add it again.")
        want_images = truthy(o.get("ai_images"))
        if want_images and not getattr(pipeline, "can_generate_images", False):
            raise HTTPException(422, "AI image generation is not set up on this server.")
        no_length = o.get("min_clip") in (None, "") and o.get("max_clip") in (None, "") and not str(o.get("style") or "").strip()
        try:
            lo, hi = derive_lengths(_opt_int(o.get("min_clip")), _opt_int(o.get("max_clip")))
            params = Params(language=o.get("language") or None, clips=_opt_int(o.get("clips")),
                            min_clip=lo, max_clip=hi,
                            spread=_opt_float(o.get("spread"), 0.5), speaker_name=str(o.get("speaker_name") or "").strip() or None,
                            speaker_title=str(o.get("speaker_title") or "").strip(), cutout=truthy(o.get("cutout")),
                            immersive=truthy(o.get("immersive")), explainer=truthy(o.get("explainer")), pictures=picture_ids,
                            ai_images=want_images, multi=o.get("multi") or "switch", graphics=truthy(o.get("graphics")),
                            clean=truthy(o.get("clean")), clean_pace=o.get("clean_pace") or "natural", gpu=o.get("gpu") if o.get("gpu") in ("auto", "home", "pod") else "auto", clean_take=o.get("clean_take") or "last", fresh_transcript=truthy(o.get("fresh_transcript")), whole=truthy(o.get("whole")) or no_length,           # no length asked for: the whole video is one reel
                           
                            cover=truthy(o.get("cover")), logo=truthy(o.get("logo")), overlay=truthy(o.get("overlay")), banner=truthy(o.get("banner")), outro=truthy(o.get("outro")), sounds=truthy(o.get("sounds")), music=truthy(o.get("music")), transition=o.get("transition") or "none")
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
        want = str(o.get("brand") or "").strip()
        if want and not brands.has(owner, want):
            raise HTTPException(422, "That brand does not exist any more. Choose another.")
        bid = want or brands.default_id(owner)
        params.brand = bid                                      # fixed now: changing the default brand later never moves this job
        if params.logo and brands.logo_path(owner, bid) is None:
            raise HTTPException(422, "Upload a logo for this brand on the Brand page first, or turn the logo off.")
        if params.overlay and brands.overlay_path(owner, bid) is None:
            raise HTTPException(422, "Upload an overlay PNG for this brand on the Brand page first, or turn the overlay off.")
        if params.banner and not brands.settings(owner, bid).get("banner_handle"):
            raise HTTPException(422, "Write the channel name for the banner on the Brand page first, or turn the banner off.")
        if params.music and brands.music_path(owner, bid) is None:
            raise HTTPException(422, "Upload a music track for this brand on the Brand page first, or turn the music off.")
        if params.outro and brands.outro_path(owner, bid) is None:
            raise HTTPException(422, "Upload an ending video for this brand on the Brand page first, or turn it off.")
        bs = brands.settings(owner, bid)
        if not params.speaker_name and bs.get("speaker_name"):  # the brand's own speaker unless the job names someone
            params.speaker_name = bs["speaker_name"]
            if not params.speaker_title:
                params.speaker_title = bs.get("speaker_title", "")
        return params

    # --- the person's own pictures for an explainer (the model looks at them and places them) -----------------------
    @app.post("/api/pictures", dependencies=[Depends(authed)], status_code=201)
    async def picture_add(request: Request, file: UploadFile = File(...)) -> dict:
        data = await file.read(MAX_PICTURE_BYTES + 1)
        try:
            out = await asyncio.to_thread(pictures.add, brand_owner(request), data)
        except PictureError as e:
            raise HTTPException(422, str(e))
        return {**out, "url": f"/api/pictures/{out['id']}"}

    @app.get("/api/pictures/{picture_id}", dependencies=[Depends(authed)])
    def picture_get(picture_id: str, request: Request):
        path = pictures.path(brand_owner(request), picture_id)
        if path is None:
            raise HTTPException(404, "No such picture.")
        return FileResponse(path, media_type="image/png")

    @app.delete("/api/pictures/{picture_id}", dependencies=[Depends(authed)], status_code=204)
    def picture_delete(picture_id: str, request: Request) -> None:
        if not pictures.delete(brand_owner(request), picture_id):
            raise HTTPException(404, "No such picture.")

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
            minutes = charge_minutes(info.duration)
            plan = effective_plan(user)
            try:
                enforce_limits(user, minutes, job.id)
            except HTTPException:
                store.delete(job.id)
                abandon(rec, finished=True)
                raise
            if accounts.reserve(user.id, job.id, minutes, plan.monthly_minutes) is None:
                store.delete(job.id)
                abandon(rec, finished=True)
                have = max(plan.monthly_minutes - accounts.used_this_month(user.id), 0) + accounts.get(user.id).credits
                raise HTTPException(402, no_minutes(minutes, have, plan))
            job.reserved_minutes = minutes
        store.save(job)
        pictures.adopt(brand_owner(request), params.pictures, store.dir(job.id) / "pictures")      # only now: a refused job keeps the person's pictures
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
                         clean: Optional[str] = Form(None), clean_pace: Optional[str] = Form(None), gpu: Optional[str] = Form(None), clean_take: Optional[str] = Form(None), fresh_transcript: Optional[str] = Form(None), whole: Optional[str] = Form(None), immersive: Optional[str] = Form(None),
                         explainer: Optional[str] = Form(None), links: Optional[str] = Form(None), pictures_ids: Optional[str] = Form(None, alias="pictures"),
                         ai_images: Optional[str] = Form(None), cover: Optional[str] = Form(None),
                         logo: Optional[str] = Form(None), overlay: Optional[str] = Form(None), banner: Optional[str] = Form(None), outro: Optional[str] = Form(None), sounds: Optional[str] = Form(None), music: Optional[str] = Form(None), transition: Optional[str] = Form(None),
                         style: Optional[str] = Form(None), brand: Optional[str] = Form(None)) -> dict:
        if rights.lower() not in ("1", "true", "on", "yes"):
            raise HTTPException(400, "Confirm that you own this video or have the rights to process it.")
        declared = int(request.headers.get("content-length") or 0)
        if declared > limit + 1_000_000:
            raise HTTPException(413, f"That file is larger than the {max_upload_mb} MB limit.")
        params = build_params(dict(language=language, clips=clips, min_clip=min_clip, max_clip=max_clip, spread=spread,
                                   speaker_name=speaker_name, speaker_title=speaker_title, cutout=cutout, multi=multi, graphics=graphics, clean=clean, clean_pace=clean_pace, gpu=gpu, clean_take=clean_take, fresh_transcript=fresh_transcript, whole=whole,
                                   immersive=immersive, explainer=explainer, links=links, pictures=pictures_ids, ai_images=ai_images, cover=cover, logo=logo, overlay=overlay, banner=banner, outro=outro, sounds=sounds, music=music, transition=transition,
                                   style=style, brand=brand),
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
            minutes = charge_minutes(info.duration)
            plan = effective_plan(user)
            try:
                enforce_limits(user, minutes, job.id)
            except HTTPException:
                store.delete(job.id)
                raise
            left = accounts.reserve(user.id, job.id, minutes, plan.monthly_minutes)
            if left is None:
                store.delete(job.id)
                have = max(plan.monthly_minutes - accounts.used_this_month(user.id), 0) + accounts.get(user.id).credits
                raise HTTPException(402, no_minutes(minutes, have, plan))
            job.reserved_minutes = minutes
        store.save(job)
        pictures.adopt(brand_owner(request), params.pictures, store.dir(job.id) / "pictures")
        runner.submit(job.id)
        return view(store.get(job.id))

    @app.get("/api/jobs", dependencies=[Depends(authed)])
    def list_jobs(request: Request) -> list[dict]:
        user = request.state.user
        if not accounts:
            return [view(j) for j in store.list()]
        out = []
        for b in store.briefs():                                  # the person's own jobs only are read in full; everyone else's cost one stat
            if b.owner == user.id:
                try:
                    out.append(view(store.get(b.id)))
                except KeyError:
                    continue
        return out

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(authed)])
    def get_job(job_id: str, request: Request) -> dict:
        return view(job_or_404(job_id, request))

    @app.get("/api/jobs/{job_id}/cleanup", dependencies=[Depends(authed)])
    def get_cleanup(job_id: str, request: Request) -> dict:
        """What the raw-recording cleanup cut, in the ORIGINAL recording's seconds, so a person can check each removal."""
        job = job_or_404(job_id, request)
        path = store.dir(job_id) / "out" / "cleanup.json"
        try:
            rep = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise HTTPException(404, "This job has no cleanup report.")
        cuts = [{"index": n, "kind": str(c.get("kind", ""))[:20], "reason": str(c.get("reason", ""))[:200], "text": str(c.get("text", ""))[:300],
                 "start": c.get("start"), "end": c.get("end"), "applied": bool(c.get("applied"))}
                for n, c in enumerate(rep.get("cuts", [])) if isinstance(c.get("start"), (int, float)) and isinstance(c.get("end"), (int, float))
                and (c.get("applied") or c.get("kind") == "suggested")]
        out = store.dir(job_id) / "out"
        can_review = (job.status == "done" and not job.source_removed and store.source_path(job_id) is not None
                      and ((out / "transcript.original.json").is_file() or (out / "transcript.json").is_file()))
        return {"pace": rep.get("pace"), "before_seconds": rep.get("before_seconds"), "after_seconds": rep.get("after_seconds"),
                "by_kind": rep.get("by_kind", {}), "cuts": sorted(cuts, key=lambda c: c["start"]), "missed_speech": rep.get("missed_speech", []),
                "timemap": rep.get("timemap", []), "reading": _clean_reading(rep.get("reading")), "can_review": can_review,
                "take": rep.get("take", "last"), "reviewed": bool(rep.get("reviewed")),
                "applied": rep.get("applied", True) is not False, "removed_share": rep.get("removed_share")}

    @app.post("/api/jobs/{job_id}/cleanup/review", dependencies=[Depends(authed)])
    def review_cleanup(job_id: str, body: CleanupReview, request: Request) -> dict:
        """Cut again with the person's choices: ``applied`` is the complete list of cut numbers (as ``GET .../cleanup`` shows them) that must be cut; every
        other cut stays in. The recording is cut again from the ORIGINAL upload and the clips are planned and rendered again (edits to clips are lost)."""
        job = job_or_404(job_id, request)
        if job.status != "done":
            raise HTTPException(409, "The cuts can be changed once the job has finished.")
        if accounts and request.state.user:
            enforce_limits(request.state.user, 0, job.id)
        counter = store.dir(job_id) / "out" / "recuts.count"
        try:
            done_recuts = int(counter.read_text() or 0)
        except (OSError, ValueError):
            done_recuts = 0
        if done_recuts >= 5:
            raise HTTPException(429, "This recording has been cut again 5 times; start a new job to cut it differently.")
        counter.parent.mkdir(parents=True, exist_ok=True)
        counter.write_text(str(done_recuts + 1))
        if job.source_removed or store.source_path(job_id) is None:
            raise HTTPException(409, "The original recording has been removed, so it cannot be cut again.")
        if any(c.render in ("queued", "rendering") for c in job.clips):
            raise HTTPException(409, "A clip is rendering. Try again when it has finished.")
        out = store.dir(job_id) / "out"
        try:
            rep = json.loads((out / "cleanup.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise HTTPException(404, "This job has no cleanup report.")
        cuts = rep.get("cuts", [])
        if any(n < 0 or n >= len(cuts) for n in body.applied):
            raise HTTPException(422, "Unknown cut number.")
        chosen = set(body.applied)
        for n, c in enumerate(cuts):
            shown = isinstance(c.get("start"), (int, float)) and isinstance(c.get("end"), (int, float)) and (c.get("applied") or c.get("kind") == "suggested")
            if shown:                                            # only what the person was shown can be switched; any other cut stays as it was
                c["applied"] = n in chosen
        if not (out / "transcript.original.json").is_file() and not (out / "transcript.json").is_file():
            raise HTTPException(409, "The saved words of this job are missing, so it cannot be cut again.")
        (out / "cleanup.review.json").write_text(json.dumps({"cuts": cuts, "reading": rep.get("reading")}, ensure_ascii=False), encoding="utf-8")

        def reset(j: Job) -> None:
            j.status, j.error, j.cancel_requested = "queued", None, False
            j.reserved_minutes = 0                              # the minutes were taken for the first run; a failed re-cut must not give them back
            for st in j.stages:
                st.state, st.detail = "pending", ""
            j.add_log("cutting again with your choices; the clips are rebuilt")
        store.update(job_id, reset)
        runner.submit(job_id)
        return view(store.get(job_id))

    @app.post("/api/jobs/{job_id}/retry", dependencies=[Depends(authed)])
    def retry_job(job_id: str, request: Request, body: Optional[RetryBody] = None) -> dict:
        """Run a job again from its saved video, with the options it had (``multi`` may be changed). Minutes are taken again. A failed or cancelled job gave
        its minutes back; a finished one is run again only when the request says what to change, and its clips are replaced."""
        job = job_or_404(job_id, request)
        if job.status in ("queued", "running"):
            raise HTTPException(409, "This job is running.")
        if job.status == "done" and not (body and body.multi):
            raise HTTPException(409, "A finished job is run again only with a changed option.")
        if store.source_path(job_id) is None:
            raise HTTPException(409, "The video of this job is no longer on the server, so it cannot be run again. Upload it again.")
        from .resume import reset_from, resolve
        step = resolve(job, store.dir(job_id) / "out", body.resume_from) if body and body.resume_from else None
        if body and body.resume_from and step is None:
            raise HTTPException(409, "That step cannot be continued from: its saved result is missing. Choose an earlier step, or start over.")
        problem = pipeline.check_ready()
        if problem:
            raise HTTPException(503, problem)
        minutes = 0
        if accounts and job.owner:
            enforce_limits(request.state.user, float(job.source.get("duration") or 0) / 60, job.id)
            throttle(request, "retry", 10, 3600, "Too many re-runs in the last hour. Try again later.")
            minutes = max(1, math.ceil(float(job.source.get("duration") or 0) / 60))
            plan = effective_plan(request.state.user)
            if accounts.reserve(job.owner, job.id, minutes, plan.monthly_minutes) is None:
                raise HTTPException(402, f"This video is {minutes} min long and you do not have that many minutes left this month.")

        def reset(j: Job) -> None:
            j.status, j.error, j.cancel_requested, j.attempts = "queued", None, False, 0
            j.reserved_minutes = minutes
            if body and body.multi:
                j.params = j.params.model_copy(update={"multi": body.multi})
            reset_from(j, step)                                           # keeps the steps before ``step`` as finished; None starts everything over
            j.resume_from = body.resume_from if step else None
            j.add_log("run again by request" + (f", continuing from the {step} step" if step else ""))
        store.update(job_id, reset)
        runner.submit(job_id)
        return view(store.get(job_id))

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

    @app.get("/api/jobs/{job_id}/clips.zip", dependencies=[Depends(authed)])
    def clips_zip(job_id: str, request: Request):
        """Every finished reel of a job in one download (stored, not compressed: an MP4 does not shrink)."""
        import zipfile
        from starlette.background import BackgroundTask
        job = job_or_404(job_id, request)
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(job.name).stem).strip("._") or "clips"
        ready = [c.id for c in job.clips if c.ready]
        if not ready:
            raise HTTPException(404, "No reel is ready yet.")
        if not zip_slots.acquire(blocking=False):                      # an archive is built on disk before the first byte is sent: only a few at a time
            raise HTTPException(429, "Several downloads are being prepared. Try again in a moment.")
        try:
            tmpdir = Path(tempfile.mkdtemp(prefix="clipcrow-zip-", dir=data_dir))        # the data disk, not a small /tmp
        except BaseException:
            zip_slots.release()
            raise
        added = 0
        try:
            archive = tmpdir / "reels.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
                for cid in ready:
                    try:
                        path = store.clip_path(job_id, cid)
                    except KeyError:
                        continue
                    rel = f"out/{cid}.mp4"
                    if not path.is_file() and storage and job.remote.get(rel):          # only the bucket has it: fetch it for this download
                        path = tmpdir / f"{cid}.mp4"
                        try:
                            storage.get_file(job_id, rel, path)
                        except StorageError:
                            continue
                    if path.is_file():
                        z.write(path, f"{stem[:60]}-{cid}.mp4")
                        added += 1
            if not added:
                raise HTTPException(404, "No reel is ready yet.")
        except BaseException:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise
        finally:
            zip_slots.release()
        return FileResponse(archive, media_type="application/zip", filename=f"{stem[:60]}-reels.zip",
                            background=BackgroundTask(shutil.rmtree, tmpdir, ignore_errors=True))

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
        throttle(request, "style", 6, 3600, "You have analysed several reference videos in the last hour. Try again later.")
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
        pool = hub.pool_of(header[7:].strip()) if header.lower().startswith("bearer ") else None
        if pool is None:
            raise HTTPException(401, "That worker token is not right.")
        request.state.worker_pool = pool

    def task_id_or_404(task_id: str) -> str:
        if not re.fullmatch(TASK_ID, task_id):
            raise HTTPException(404, "No such task.")
        return task_id

    @app.post("/api/worker/poll", dependencies=[Depends(worker_auth)])
    async def worker_poll(body: PollIn, request: Request) -> dict:
        pool = request.state.worker_pool or body.pool                    # a pool token decides the pool, not the worker
        task = await asyncio.to_thread(hub.poll, body.worker, body.wait, body.device, body.model, tuple(body.can), body.pin, body.direct, body.power, pool, body.build)
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

    @app.post("/api/worker/tasks/{task_id}/direct-upload", dependencies=[Depends(worker_auth)])
    async def worker_direct_upload(task_id: str, body: DirectIn) -> dict:
        """Presigned part URLs for the finished clip: the bytes go to the bucket's own host, not through this domain's proxy."""
        task_id_or_404(task_id)
        try:
            plan = await asyncio.to_thread(hub.direct_plan, task_id, body.worker, body.size)
        except WorkerError as e:
            raise HTTPException(409, str(e))
        if plan is None:
            raise HTTPException(404, "Direct upload is not available.")         # the worker then sends the clip through this server as before
        return plan

    @app.post("/api/worker/tasks/{task_id}/direct-done", dependencies=[Depends(worker_auth)])
    async def worker_direct_done(task_id: str, body: DirectDoneIn) -> dict:
        task_id_or_404(task_id)
        ok = await asyncio.to_thread(hub.direct_finish, task_id, body.worker, [(p.n, p.etag) for p in body.parts])
        if not ok:
            raise HTTPException(422, "The clip did not arrive in full.")
        return {"ok": True}

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
        if kind == "speakers":
            if body.speakers is None:
                return {"ok": hub.finish(task_id, body.worker, error="the worker sent no answer")}
            return {"ok": hub.finish(task_id, body.worker, result={"speakers": body.speakers.model_dump()})}
        if not body.words:
            return {"ok": hub.finish(task_id, body.worker, error="the worker found no speech")}
        return {"ok": hub.finish(task_id, body.worker, result={"language": body.language, "engine": body.engine, "words": [w.model_dump() for w in body.words]})}

    # --- the brand kit: logo, ending video, how the cover and logo look ----------------------------------------
    def brand_q(request: Request) -> Optional[str]:
        """The brand a request is about (``?brand=<id>``), else None for the default one. Someone else's brand is simply not there."""
        bid = request.query_params.get("brand")
        if not bid:
            return None
        if not brands.has(brand_owner(request), bid):
            raise HTTPException(404, "No such brand.")
        return bid

    @app.get("/api/brands", dependencies=[Depends(authed)])
    def brands_list(request: Request) -> list[dict]:
        return brands.list(brand_owner(request))

    @app.post("/api/brands", dependencies=[Depends(authed)], status_code=201)
    def brands_create(request: Request, body: BrandName) -> dict:
        try:
            return brands.create(brand_owner(request), body.name)
        except BrandError as e:
            raise HTTPException(422, str(e))

    @app.put("/api/brands/{brand_id}", dependencies=[Depends(authed)])
    def brands_update(brand_id: str, request: Request, body: BrandPatch) -> list[dict]:
        owner = brand_owner(request)
        if not brands.has(owner, brand_id):
            raise HTTPException(404, "No such brand.")
        try:
            if body.name is not None:
                brands.rename(owner, brand_id, body.name)
            if body.default:
                brands.set_default(owner, brand_id)
        except BrandError as e:
            raise HTTPException(422, str(e))
        return brands.list(owner)

    @app.delete("/api/brands/{brand_id}", dependencies=[Depends(authed)])
    def brands_delete(brand_id: str, request: Request) -> list[dict]:
        owner = brand_owner(request)
        if not brands.has(owner, brand_id):
            raise HTTPException(404, "No such brand.")
        try:
            brands.remove(owner, brand_id)
        except BrandError as e:
            raise HTTPException(422, str(e))
        return brands.list(owner)

    @app.get("/api/brand", dependencies=[Depends(authed)])
    def brand_get(request: Request) -> dict:
        return brands.describe(brand_owner(request), brand_q(request))

    @app.post("/api/brand/preview", dependencies=[Depends(authed)])
    def brand_preview(request: Request, body: LogoPreview) -> dict:
        """Where the logo would sit for a face at ``zone`` with these settings: the same placement a real clip gets (``branding.fit_logo``), for the preview."""
        from ..branding import fit_logo, logo_aspect
        from ..edl import Logo
        owner, bid = brand_owner(request), brand_q(request)
        path = brands.logo_path(owner, bid)
        lg = fit_logo(Logo(asset="brand-logo", position=body.position, size_pct=body.size_pct, opacity=0.9, margin_px=body.margin_px), body.zone, body.caption_top,
                      logo_aspect(path) if path else 1.0, banner_top=body.banner_top)
        return {"position": lg.position, "top_px": lg.top_px, "shrink": lg.shrink, "side_px": lg.side_px, "faded_opacity": lg.faded_opacity, "aspect": logo_aspect(path) if path else 1.0}

    @app.put("/api/brand", dependencies=[Depends(authed)])
    def brand_put(request: Request, body: dict = Body(...)) -> dict:
        try:
            brands.set_settings(brand_owner(request), body, brand_q(request))
        except BrandError as e:
            raise HTTPException(422, str(e))
        return brands.describe(brand_owner(request), brand_q(request))

    @app.post("/api/brand/logo", dependencies=[Depends(authed)])
    async def brand_logo(request: Request, file: UploadFile = File(...)) -> dict:
        data = await file.read(MAX_LOGO_BYTES + 1)
        try:
            return await asyncio.to_thread(brands.put_logo, brand_owner(request), data, brand_q(request))
        except BrandError as e:
            raise HTTPException(422, str(e))

    @app.post("/api/brand/overlay", dependencies=[Depends(authed)])
    async def brand_overlay(request: Request, file: UploadFile = File(...)) -> dict:
        data = await file.read(MAX_OVERLAY_BYTES + 1)
        try:
            return await asyncio.to_thread(brands.put_overlay, brand_owner(request), data, brand_q(request))
        except BrandError as e:
            raise HTTPException(422, str(e))

    @app.post("/api/brand/outro", dependencies=[Depends(authed)])
    async def brand_outro(request: Request, file: UploadFile = File(...)) -> dict:
        owner = brand_owner(request)
        bq = brand_q(request)
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
                return await asyncio.to_thread(brands.put_outro, owner, raw, bq)
            except BrandError as e:
                raise HTTPException(422, str(e))

    @app.post("/api/brand/music", dependencies=[Depends(authed)])
    async def brand_music(request: Request, file: UploadFile = File(...)) -> dict:
        owner = brand_owner(request)
        bq = brand_q(request)
        with tempfile.TemporaryDirectory(dir=data_dir) as tmp:
            raw = Path(tmp) / "in"
            size = 0
            with raw.open("wb") as f:
                while chunk := await file.read(1 << 20):
                    size += len(chunk)
                    if size > MAX_MUSIC_BYTES:
                        raise HTTPException(413, f"The music can be at most {MAX_MUSIC_BYTES // (1024 * 1024)} MB.")
                    f.write(chunk)
            try:
                return await asyncio.to_thread(brands.put_music, owner, raw, bq)
            except BrandError as e:
                raise HTTPException(422, str(e))

    @app.get("/api/brand/music/file", dependencies=[Depends(authed)])
    def brand_music_file(request: Request):
        path = brands.music_path(brand_owner(request), brand_q(request))
        if path is None:
            raise HTTPException(404, "No music yet.")
        return FileResponse(path, media_type="audio/mpeg" if path.suffix == ".mp3" else "audio/mp4", headers={"Cache-Control": "no-cache"})

    @app.get("/api/brand/tracks/{track_id}/file", dependencies=[Depends(authed)])
    def brand_track_file(track_id: str):
        path = track_path(track_id) if TRACK_ID.match(track_id) else None
        if path is None:
            raise HTTPException(404, "No such track.")
        return FileResponse(path, media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/api/fonts/file", dependencies=[Depends(authed)])
    def font_file(name: str):
        """One catalogue font's Regular file, for the Brand page preview. Only names in the catalogue are served (never a path)."""
        from .. import fonts
        if name not in fonts.KNOWN:
            raise HTTPException(404, "No such font.")
        try:
            path = fonts.resolve_font(name, 400)
        except fonts.FontNotFound:
            raise HTTPException(404, "That font is not installed here.")
        return FileResponse(path, media_type="font/ttf", headers={"Cache-Control": "private, max-age=86400"})

    @app.delete("/api/brand/{what}", dependencies=[Depends(authed)])
    def brand_delete(what: str, request: Request) -> dict:
        try:
            return brands.delete(brand_owner(request), what, brand_q(request))
        except BrandError as e:
            raise HTTPException(404, str(e))

    @app.get("/api/brand/logo/file", dependencies=[Depends(authed)])
    def brand_logo_file(request: Request):
        path = brands.logo_path(brand_owner(request), brand_q(request))
        if path is None:
            raise HTTPException(404, "No logo yet.")
        return FileResponse(path, media_type={".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}[path.suffix],
                            headers={"Cache-Control": "no-cache"})

    @app.get("/api/brand/overlay/file", dependencies=[Depends(authed)])
    def brand_overlay_file(request: Request):
        path = brands.overlay_path(brand_owner(request), brand_q(request))
        if path is None:
            raise HTTPException(404, "No overlay yet.")
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-cache"})

    @app.get("/api/brand/outro/file", dependencies=[Depends(authed)])
    def brand_outro_file(request: Request):
        path = brands.outro_path(brand_owner(request), brand_q(request))
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
        throttle(request, "edit", 40, 60, "You are editing faster than the clips can be rendered. Wait a moment.")
        try:
            changed = runner.edit_clip(job_id, clip_id, edit)
        except ValueError as e:
            raise HTTPException(422, str(e)[:300])
        return {"changed": changed, "render": store.get(job_id).clip(clip_id).model_dump()}

    @app.get("/api/jobs/{job_id}/speakers", dependencies=[Depends(authed)])
    def speakers_get(job_id: str, request: Request) -> dict:
        """Who is on screen and which voice is whose, for the "who is who" card. 404 when the video has one person (or a single camera shot is not fixed)."""
        from ..seats import load_map
        job = job_or_404(job_id, request)
        out = store.dir(job_id) / "out"
        sm = load_map(out)
        flag = out / "speakers.building"
        building = flag.exists() and time.time() - flag.stat().st_mtime < 3600
        if sm is None:
            wide = (job.source.get("width") or 0) > 1.2 * (job.source.get("height") or 1)
            return {"available": False, "building": building, "can_build": bool(job.status == "done" and wide and not job.source_removed and store.working_video(job_id) is not None)}
        spoken: dict[int, float] = {}
        for t in sm.turns:
            spoken[t.voice] = spoken.get(t.voice, 0.0) + (t.end - t.start)
        return {"available": True, "building": building, "seats": [{"n": i + 1, "cx": s.cx, "cy": s.cy, "w": round(s.w, 3), "h": round(s.h, 3), "seen": round(s.seen, 2), "live": round(s.live, 2)} for i, s in enumerate(sm.seats)],     # size, share of frames seen and the still-face value: what a "who is who" that looks wrong needs to be explained
                "voices": [{"voice": v, "seconds": round(spoken[v]), "seat": (None if sm.seat_of(v) is None else sm.seat_of(v) + 1),
                            "changed": v in sm.overrides} for v in sorted(spoken)],
                "picture": (store.dir(job_id) / "out" / "speakers.jpg").is_file()}

    @app.post("/api/jobs/{job_id}/speakers/build", dependencies=[Depends(authed)])
    def speakers_build(job_id: str, request: Request) -> dict:
        """Find who is on screen and whose voice is whose, for a job made without it. Takes a few minutes; the clips are framed again when it is done."""
        job_or_404(job_id, request)
        throttle(request, "speakers", 6, 3600, "You have started several speaker checks in the last hour. Try again later.")
        try:
            started = runner.build_speakers(job_id)
        except ValueError as e:
            raise HTTPException(409, str(e))
        return {"started": started}

    @app.get("/api/jobs/{job_id}/speakers/picture", dependencies=[Depends(authed)])
    def speakers_picture(job_id: str, request: Request):
        job_or_404(job_id, request)
        path = store.dir(job_id) / "out" / "speakers.jpg"
        if not path.is_file():
            raise HTTPException(404, "No picture.")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/jobs/{job_id}/speakers/{voice}/sample", dependencies=[Depends(authed)])
    def speakers_sample(job_id: str, voice: int, request: Request):
        """A few seconds of one voice (its longest turn), so a person can hear who it is."""
        from ..seats import load_map
        job_or_404(job_id, request)
        sm = load_map(store.dir(job_id) / "out")
        video = store.working_video(job_id)
        turns = [t for t in (sm.turns if sm else []) if t.voice == voice]
        if not turns or video is None or not 0 <= voice <= 20:
            raise HTTPException(404, "No such voice.")
        t = max(turns, key=lambda x: x.end - x.start)
        start, length = t.start + max(0.0, (t.end - t.start - 6.0) / 2), min(6.0, t.end - t.start)
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{length:.2f}", "-i", str(video), "-vn", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "64k", "-f", "mp3", "-"],
                           capture_output=True, timeout=60)
        if r.returncode != 0 or not r.stdout:
            raise HTTPException(404, "No sample.")
        return Response(r.stdout, media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=3600"})

    @app.put("/api/jobs/{job_id}/speakers", dependencies=[Depends(authed)])
    def speakers_put(job_id: str, body: SpeakerChoice, request: Request) -> dict:
        """Say which person each voice is (1 = the first from the left, null = off screen); the clips that followed the voices are planned again."""
        job_or_404(job_id, request)
        try:
            overrides = {int(k): (None if v is None else int(v) - 1) for k, v in body.voices.items()}
            clips = runner.set_speakers(job_id, overrides)
        except (ValueError, TypeError) as e:
            raise HTTPException(409, str(e))
        return {"clips": clips}

    @app.get("/api/jobs/{job_id}/transcript", dependencies=[Depends(authed)])
    def transcript_get(job_id: str, request: Request) -> dict:
        """The words of the plan, sentence by sentence, for a person to read and correct. ``original`` is what the recogniser first wrote
        where the AI corrected it (only when the timeline is still the original one, i.e. no cleanup cut the recording)."""
        job = job_or_404(job_id, request)
        if job.status != "done":
            raise HTTPException(409, "The transcript can be checked once the job has finished.")
        try:
            edl = load_edl(store.edl_path(job_id))
            raw = None
            rawp = store.dir(job_id) / "out" / "transcript.raw.json"
            if rawp.is_file() and not (store.dir(job_id) / "out" / "clean.mp4").is_file():
                try:
                    raw = json.loads(rawp.read_text(encoding="utf-8")).get("words")
                except (OSError, ValueError):
                    raw = None
            return transcript_view(edl, raw)
        except (KeyError, OSError):
            raise HTTPException(404, "The plan for this job is not on disk.")
        except ValueError as e:
            raise HTTPException(409, str(e)[:200])

    @app.put("/api/jobs/{job_id}/transcript", dependencies=[Depends(authed)])
    def transcript_put(job_id: str, body: TranscriptEdit, request: Request) -> dict:
        job = job_or_404(job_id, request)
        if job.status == "done" and (job.source_removed or store.working_video(job_id) is None):
            raise HTTPException(409, "The original video was removed to save space, so the clips can no longer be rendered again. "
                                     "You can still download them.")
        throttle(request, "edit", 40, 60, "You are editing faster than the clips can be rendered. Wait a moment.")
        try:
            clips = runner.edit_transcript(job_id, body)
        except ValueError as e:
            raise HTTPException(422, str(e)[:300])
        except (KeyError, OSError):
            raise HTTPException(404, "The plan for this job is not on disk.")
        return {"changed": bool(clips), "clips": clips}

    @app.post("/api/jobs/{job_id}/clips/{clip_id}/reset", dependencies=[Depends(authed)])
    def clip_reset(job_id: str, clip_id: str, request: Request) -> dict:
        editable(job_id, clip_id, request)
        throttle(request, "edit", 40, 60, "You are editing faster than the clips can be rendered. Wait a moment.")
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
        throttle(request, "edit", 40, 60, "You are editing faster than the clips can be rendered. Wait a moment.")
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
            "gpu": gpu_summary(store.gpu_usage(job_id)),
            **({"economics": (lambda c, j: {**job_economics(j.reserved_minutes * price_per_minute(), c["total"], target_markup()), "cost_ai": c["ai"], "cost_gpu": c["gpu"],
                                            "minutes": j.reserved_minutes})(job_cost(job_id), store.get(job_id))}
               if request.state.user is not None and request.state.user.email in admins and job_cost(job_id) else {}),
        }

    # The landing page is the front door (static/index.html, served at /); the app itself is at /dashboard (hash routes: /dashboard#/jobs/...).
    @app.get("/dashboard", include_in_schema=False)
    @app.get("/dashboard/", include_in_schema=False)
    def dashboard_page() -> FileResponse:
        return FileResponse(STATIC / "dashboard.html", media_type="text/html")

    @app.get("/landing", include_in_schema=False)
    @app.get("/landing/", include_in_schema=False)
    def old_landing_address() -> RedirectResponse:
        return RedirectResponse("/", status_code=301)

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="ui")
    return app
