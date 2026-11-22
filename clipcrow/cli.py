from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from .edl import EDLError, json_schema, load_edl


DEFAULT_LOCAL_CLIP_DAYS = 0.05           # about 72 minutes: long enough to watch or edit the clip you just made, then the bucket copy is the only one


def local_clip_days_from_env(value: str, has_storage: bool):
    """How long a finished clip's file stays on the server once an identical copy is in the bucket. Not set: about an hour. A number of days (0 = as soon as it is
    uploaded). ``off`` keeps them for ever. Without a bucket there is no copy to rely on, so nothing is ever removed."""
    if not has_storage:
        return None
    v = (value or "").strip().lower()
    if v in ("off", "keep", "never", "no", "false"):
        return None
    if not v:
        return DEFAULT_LOCAL_CLIP_DAYS
    try:
        return max(0.0, float(v))
    except ValueError:
        return DEFAULT_LOCAL_CLIP_DAYS


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="clipcrow")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("schema", help="print the EDL JSON schema")
    v = sub.add_parser("validate", help="validate an EDL file")
    v.add_argument("edl")
    c = sub.add_parser("compile", help="write the HyperFrames project for a clip (no render)")
    c.add_argument("edl"); c.add_argument("--clip", required=True); c.add_argument("--out", required=True)
    c.add_argument("--media-root", default="."); c.add_argument("--gsap")
    c.add_argument("--no-media", action="store_true", help="HTML only: skip ffmpeg and the matte")
    t = sub.add_parser("transcribe", help="speech to a word-timed transcript JSON (faster-whisper, Indic-aware)")
    t.add_argument("media"); t.add_argument("--out", required=True); t.add_argument("--language")
    ca = sub.add_parser("compare-asr", help="run the old and the new speech settings on one stretch of a video and print them side by side")
    ca.add_argument("media"); ca.add_argument("--from", dest="start", type=float, default=0.0); ca.add_argument("--to", type=float, required=True)
    ca.add_argument("--language", default="mr"); ca.add_argument("--model", help="Whisper model (default $CLIPCROW_WHISPER_MODEL)")
    ca.add_argument("--refine", action="store_true", help="also correct the new text with the model (needs ANTHROPIC_API_KEY)")
    ca.add_argument("--out", help="also write the result as JSON")
    mk = sub.add_parser("make", help="video -> clips: transcribe, plan, frame, render")
    mk.add_argument("video"); mk.add_argument("--out", required=True); mk.add_argument("--language")
    mk.add_argument("--transcript", help="skip speech recognition: use this transcript JSON")
    mk.add_argument("--edl", help="skip transcription and planning: use this EDL")
    mk.add_argument("--clips", type=int); mk.add_argument("--min-clip", type=int, default=15)
    mk.add_argument("--max-clip", type=int, default=60); mk.add_argument("--cutout", action="store_true")
    mk.add_argument("--immersive", action="store_true", help="slow push-ins at sentence starts (face-to-camera reels)")
    mk.add_argument("--explainer", action="store_true", help="show pictures over the footage where the speaker names something")
    mk.add_argument("--picture", action="append", default=[], help="a picture of yours (PNG/JPEG/WebP) the explainer may show: the model looks at it and picks the moment (repeat up to 6 times)")
    mk.add_argument("--link", action="append", default=[], help="a public page the explainer may show (repeat up to 6 times)")
    mk.add_argument("--ai-images", action="store_true", help="let the explainer ask for AI-generated images (GEMINI_API_KEY; "
                    "marks the clip as AI-generated media)")
    mk.add_argument("--spread", type=float, default=0.5, help="spread clips across the talk (0 = off)")
    mk.add_argument("--cover", action="store_true", help="a bold title (the hook, else the title) for the first ~250 ms, and a cN.cover.png")
    mk.add_argument("--logo", metavar="IMAGE", help="a logo (png/jpg/webp) in a corner of every clip")
    mk.add_argument("--outro", metavar="VIDEO", help="a branding video appended after every clip (first 6 s)")
    mk.add_argument("--whole", action="store_true", help="make the whole (cleaned) recording ONE clip: no moment picking, up to 15 minutes")
    mk.add_argument("--clean", action="store_true", help="first cut fumbles, fillers, retakes and dead air out of the raw recording")
    mk.add_argument("--clean-pace", choices=["natural", "fast", "tight"], default="natural", help="how short long pauses become")
    mk.add_argument("--clean-take", choices=["last", "best"], default="last", help="which take of a repeated passage stays: the last, or the one the AI judges best")
    mk.add_argument("--graphics", action="store_true", help="the source cuts to slides, cards or b-roll: show those whole instead of cropping them")
    mk.add_argument("--multi", choices=["switch", "split", "off"], default="switch",
                    help="two or more people on screen: cut to whoever talks, stack two, or follow one face")
    mk.add_argument("--speaker-name"); mk.add_argument("--speaker-title", default="")
    mk.add_argument("--model"); mk.add_argument("--score-model"); mk.add_argument("--gsap")
    mk.add_argument("--workers", type=int); mk.add_argument("--no-render", action="store_true")
    sv = sub.add_parser("serve", help="run the web service (sign-in is by emailed code: set CLIPCROW_ACCOUNTS=1)")
    sv.add_argument("--data", default="data", help="where jobs and clips are stored")
    sv.add_argument("--host", default="127.0.0.1"); sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--max-upload-mb", type=int, default=4096)
    sv.add_argument("--demo", action="store_true", help="built-in Marathi transcript and recorded answers: no Whisper, no API key")
    sv.add_argument("--insecure", action="store_true", help="allow a non-local address without accounts (no login at all: only on a network you trust)")
    sv.add_argument("--accounts", action="store_true", default=os.environ.get("CLIPCROW_ACCOUNTS") == "1", help="multi-user: sign-up, per-user jobs, minutes metered against a plan, retention")
    sv.add_argument("--no-signup", action="store_true", default=os.environ.get("CLIPCROW_SIGNUP") == "0", help="with --accounts: do not let new people register")
    es = sub.add_parser("export-sample", help="save a finished job as the sample every new account starts with (set CLIPCROW_SAMPLE_DIR to the folder)")
    es.add_argument("job_id")
    es.add_argument("--data", default="data", help="the service's data folder")
    es.add_argument("--to", required=True, help="the folder to write (job.json and out/)")
    md = sub.add_parser("migrate-db", help="copy the accounts from the old SQLite file into PostgreSQL (the address is DATABASE_URL)")
    md.add_argument("--data", default="data", help="the service's data folder (holds clipcrow.db and clipcrow.secret)")
    md.add_argument("--sqlite", default=None, help="the SQLite file to read (default: <data>/clipcrow.db)")
    ms = sub.add_parser("migrate-stores", help="copy the records that live in files (brands, styles, settings, ...) into the database, or check the two agree with --verify")
    ms.add_argument("--data", default="data", help="the service's data folder")
    ms.add_argument("--verify", action="store_true", help="only compare the files with the database; change nothing")
    rc = sub.add_parser("r2-cors", help="set (or show) the bucket's CORS rule that lets browsers upload straight to R2")
    rc.add_argument("--origin", action="append", default=[], help="the site's address, e.g. https://clips.example.com (repeat for several)")
    rc.add_argument("--show", action="store_true", help="print the current rule instead of changing it")
    wk = sub.add_parser("worker", help="run speech recognition for a ClipCrow server on this machine (e.g. one with an NVIDIA GPU); "
                                       "needs CLIPCROW_WORKER_TOKEN in the environment")
    wk.add_argument("--server", required=True, help="the server's address, e.g. https://clipcrow.example.com")
    wk.add_argument("--name", default=os.environ.get("HOSTNAME", "worker")[:40] or "worker", help="a name shown on the server")
    wk.add_argument("--model", default="large-v3", help="faster-whisper model (large-v3 needs about 4-5 GB of GPU memory)")
    wk.add_argument("--no-render", action="store_true", help="do speech recognition only, even if this machine could also render clips")
    wk.add_argument("--cache-gb", type=float, default=20.0, help="disk the worker may use to keep source videos between a job's clips")
    wk.add_argument("--pool", choices=("home", "rented"), default=os.environ.get("CLIPCROW_WORKER_POOL", "home"), help="home: a machine you own; rented: the on-demand GPU (a job can ask for one kind)")
    wk.add_argument("--parallel", type=int, default=1, help="how many clips this machine renders at once (extra slots only render); speech recognition stays one at a time")
    st = sub.add_parser("selftest", help="can this machine render? checks the tools and renders a tiny Devanagari clip")
    st.add_argument("--no-render", action="store_true", help="check the tools only")
    u = sub.add_parser("usage", help="show the AI-usage record of an EDL")
    u.add_argument("edl")
    pl = sub.add_parser("plan", help="transcript -> EDL with an LLM (needs ANTHROPIC_API_KEY)")
    pl.add_argument("transcript"); pl.add_argument("--source", required=True, help="the source video file")
    pl.add_argument("--out", required=True); pl.add_argument("--media-root", default=".")
    pl.add_argument("--duration", type=float, help="source length in seconds, when the video file is not available")
    pl.add_argument("--job-id", default="job"); pl.add_argument("--language")
    pl.add_argument("--clips", type=int, help="how many clips (default: about one per 2 minutes)")
    pl.add_argument("--min-clip", type=int, default=15); pl.add_argument("--max-clip", type=int, default=60)
    pl.add_argument("--cutout", action="store_true", help="allow text behind the speaker (a cutout will exist)")
    pl.add_argument("--immersive", action="store_true", help="slow push-ins at sentence starts")
    pl.add_argument("--speaker-name"); pl.add_argument("--speaker-title", default="")
    pl.add_argument("--spread", type=float, default=0.5,
                    help="spread clips across the talk (0 = off, 0.5 = default): min gap = spread x length / clips")
    pl.add_argument("--model", help="default: $CLIPCROW_MODEL or claude-opus-5-5")
    pl.add_argument("--score-model", help="cheaper model for the window-scoring pass")
    pl.add_argument("--report", help="write the planning report (scores, drops, warnings) here")
    pl.add_argument("--answers", help="answer prompts from files in this directory instead of calling an API "
                                       "(queues unanswered prompts in DIR/pending/)")
    r = sub.add_parser("render", help="compile, check and render a clip to MP4")
    r.add_argument("edl"); r.add_argument("--clip", required=True); r.add_argument("--out", required=True)
    r.add_argument("--media-root", default="."); r.add_argument("--workdir", default="build")
    r.add_argument("--gsap"); r.add_argument("--workers", type=int)
    r.add_argument("--matte-device", default="auto"); r.add_argument("--no-check", action="store_true")
    a = ap.parse_args(argv)

    try:
        if a.cmd == "schema":
            print(json.dumps(json_schema(), indent=2)); return 0
        if a.cmd == "serve":
            import uvicorn
            from .service.api import create_app
            from .service.billing import Razorpay
            from .service.demo import DemoPipeline
            from .service.publish import SocioBird
            from .service.pipelines import RealPipeline
            if os.environ.get("CLIPCROW_SOCIOBIRD_SHARED"):
                print("note: CLIPCROW_SOCIOBIRD_SHARED is no longer used: with accounts on, everyone posts only to their OWN connected SocioBird (SOCIOBIRD_API_KEY is ignored).", file=sys.stderr)
            if os.environ.get("CLIPCROW_TOKEN"):
                print("note: CLIPCROW_TOKEN is no longer used. Sign-in is by emailed code only: set CLIPCROW_ACCOUNTS=1 (and email, see docs/deploy.md).", file=sys.stderr)
            accounts = None
            if a.accounts:
                from .service.accounts import Accounts
                url = os.environ.get("DATABASE_URL", "").strip()
                accounts = Accounts(url or Path(a.data) / "clipcrow.db", secret_file=Path(a.data) / "clipcrow.secret")
                print(f"accounts database: {accounts.describe()}", file=sys.stderr)
                if a.host not in ("127.0.0.1", "::1", "localhost"):
                    print("note: accounts use cookies: serve this behind TLS (a reverse proxy)", file=sys.stderr)
            if a.host not in ("127.0.0.1", "::1", "localhost") and not accounts and not a.insecure:
                raise ValueError(f"refusing to listen on {a.host} without a login: set CLIPCROW_ACCOUNTS=1 (sign-in by emailed code) "
                                 f"(or pass --insecure on a network you trust)")
            from .service import mailer as mailer_mod
            mailer = mailer_mod.from_env()
            if accounts and mailer is None:
                print("WARNING: accounts are on but email is not set up (CLIPCROW_EMAIL, RESEND_API_KEY or SMTP_HOST, MAIL_FROM): "
                      "sign-in is by emailed code, so nobody can sign in until it is.", file=sys.stderr)
            elif mailer is not None:
                print(f"email: {mailer.name}", file=sys.stderr)
            from .service.storage import S3Storage
            storage = S3Storage.from_env()
            if storage:
                print(f"object storage: bucket {storage.bucket} at {storage.host}", file=sys.stderr)
            app = create_app(a.data, DemoPipeline() if a.demo else RealPipeline(),
                             max_upload_mb=a.max_upload_mb, demo=a.demo, accounts=accounts, allow_signup=not a.no_signup,
                             razorpay=Razorpay.from_env() if accounts else None, sociobird=None if accounts else SocioBird.from_env(),
                             storage=storage, storage_redirect=os.environ.get("CLIPCROW_R2_REDIRECT") == "1",
                             backup_source=os.environ.get("CLIPCROW_R2_BACKUP_SOURCE") == "1",
                             local_clip_days=local_clip_days_from_env(os.environ.get("CLIPCROW_LOCAL_CLIP_DAYS", ""), storage is not None),
                             mailer=mailer, public_url=os.environ.get("CLIPCROW_PUBLIC_URL", ""),
                             worker_token=os.environ.get("CLIPCROW_WORKER_TOKEN") or None,
                             worker_pickup_seconds=float(os.environ.get("CLIPCROW_WORKER_PICKUP_SECONDS", "120") or 120))
            # behind the proxy (Coolify/Traefik, Cloudflare) every request arrives from the proxy's address: trust its X-Forwarded-* headers, or the per-IP limits become one
            # global bucket and the session cookie loses `Secure`. CLIPCROW_TRUSTED_PROXIES: addresses/CIDRs, or "*" (default when the public address is https).
            trusted = os.environ.get("CLIPCROW_TRUSTED_PROXIES", "").strip() or ("*" if os.environ.get("CLIPCROW_PUBLIC_URL", "").startswith("https://") else "127.0.0.1")
            uvicorn.run(app, host=a.host, port=a.port, log_level="info", timeout_graceful_shutdown=20, proxy_headers=True, forwarded_allow_ips=trusted)
            return 0
        if a.cmd == "migrate-db":
            from .service.accounts import Accounts
            from .service.dbmigrate import migrate
            url = os.environ.get("DATABASE_URL", "")
            if not url.startswith(("postgres://", "postgresql://")):
                raise ValueError("set DATABASE_URL to the PostgreSQL address first (it holds a password, so it is read from the environment, never from the command line)")
            src = a.sqlite or str(Path(a.data) / "clipcrow.db")
            target = Accounts(url, secret_file=Path(a.data) / "clipcrow.secret")
            report = migrate(src, target)
            print("copied:", ", ".join(f"{k} {v}" for k, v in report.items()))
            print(f"target: {target.describe()}. Now set DATABASE_URL on the service and redeploy.")
            return 0
        if a.cmd == "migrate-stores":
            from .service.accounts import Accounts
            from .service.docstore import DocStore
            from .service import storemigrate
            url = os.environ.get("DATABASE_URL", "").strip()
            target = Accounts(url or Path(a.data) / "clipcrow.db", secret_file=Path(a.data) / "clipcrow.secret")      # the same database the accounts use
            docs = DocStore(target.db)
            from .service.jobrows import JobRows
            rows = JobRows(target.db)
            if a.verify:
                rep = storemigrate.verify(a.data, docs, rows)
                print(f"files {rep['files']}, same {rep['same']}, different {len(rep['different'])}, only in files {len(rep['only_in_files'])}, only in the database {len(rep['only_in_db'])}")
                for name in rep["different"][:20] + rep["only_in_files"][:20] + rep["only_in_db"][:20]:
                    print("  -", name)
                return 0 if not (rep["different"] or rep["only_in_files"] or rep["only_in_db"]) else 1
            rep = storemigrate.migrate(a.data, docs, rows)
            print("copied:", ", ".join(f"{k} {v}" for k, v in rep.items()))
            print(f"target: {target.describe()}. Check with: clipcrow migrate-stores --verify")
            return 0
        if a.cmd == "export-sample":
            from .service.jobs import JobStore
            from .service.sample import export_sample
            n = export_sample(JobStore(a.data), a.job_id, a.to)
            print(f"sample written to {a.to} ({n} files). Set CLIPCROW_SAMPLE_DIR to that folder on the server.")
            return 0
        if a.cmd == "r2-cors":
            from .service.storage import S3Storage
            storage = S3Storage.from_env()
            if storage is None:
                raise ValueError("set R2_ACCOUNT_ID (or R2_ENDPOINT), R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY and R2_BUCKET first")
            if a.show or not a.origin:
                print(json.dumps(storage.get_cors(), indent=2) or "[]")
                if not a.show:
                    print("(pass --origin https://your-site to set the rule; the token needs permission to change bucket settings)", file=sys.stderr)
                return 0
            storage.set_cors(a.origin)
            print(f"CORS rule set on {storage.bucket} for: {', '.join(a.origin)}")
            return 0
        if a.cmd == "worker":
            from .worker import main as worker_main
            return worker_main(a.server, a.name, a.model, render=not a.no_render, cache_gb=a.cache_gb, parallel=a.parallel, pool=a.pool)
        if a.cmd == "selftest":
            from .selftest import run as selftest
            return 0 if selftest(skip_render=a.no_render) else 1
        if a.cmd == "transcribe":
            from .asr import release_models, transcribe
            tr = transcribe(a.media, a.language, progress=lambda m: print(f"  {m}", file=sys.stderr))
            Path(a.out).write_text(json.dumps({"language": tr.language, "words": [w.model_dump(exclude_none=True) for w in tr.words]},
                                              ensure_ascii=False, indent=1), encoding="utf-8")
            release_models()
            print(f"{len(tr.words)} words, {len(tr.sentences)} sentences, language {tr.language} -> {a.out}")
            return 0
        if a.cmd == "compare-asr":
            from . import asr_compare
            from .asr import AsrConfig, release_models
            cfg = AsrConfig(model=a.model) if a.model else AsrConfig()
            llm = None
            if a.refine:
                from .planner import AnthropicLLM
                llm = AnthropicLLM()
            res = asr_compare.compare(a.media, a.start, a.to, a.language, config=cfg, llm=llm, say=lambda m: print(f"  {m}", file=sys.stderr))
            release_models()
            print(asr_compare.render(res))
            if a.out:
                Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
            return 0
        if a.cmd == "make":
            from .make import make
            from .planner import AnthropicLLM, PlannerOptions
            if len(a.link) > 6:
                raise ValueError("use at most 6 --link addresses")
            from .assets.urlguard import assert_public_url
            for u in a.link:
                assert_public_url(u)
            if len(a.picture) > 6:
                raise ValueError("use at most 6 --picture files")
            pics = []
            for k, f in enumerate(a.picture):
                fp = Path(f)
                if not fp.is_file():
                    raise ValueError(f"--picture {f}: no such file")
                pics.append((hashlib.sha256(fp.read_bytes()).hexdigest()[:12], fp))
            images = None
            if a.ai_images:
                from .assets.images import GeminiImages
                images = GeminiImages()
            opts = PlannerOptions(min_clip=a.min_clip, max_clip=a.max_clip, target_clips=a.clips, whole=a.whole, cutout=a.cutout, immersive=a.immersive,
                                  explainer=a.explainer, links=tuple(a.link), ai_images=a.ai_images, pictures=tuple(pics),
                                  spread=a.spread,
                                  speaker_name=a.speaker_name, speaker_title=a.speaker_title)
            need_llm = not a.edl
            from .branding import BrandingChoice, BrandKit
            kit = BrandKit(logo=Path(a.logo) if a.logo else None, outro=Path(a.outro) if a.outro else None)
            choice = BrandingChoice(cover=a.cover, logo=bool(a.logo), outro=bool(a.outro))
            res = make(a.video, a.out, language=a.language, transcript_path=a.transcript, edl_path=a.edl, opts=opts,
                       llm=AnthropicLLM(a.model) if need_llm else None,
                       score_llm=AnthropicLLM(a.score_model) if need_llm and a.score_model else None,
                       render=not a.no_render, gsap_js=a.gsap, workers=a.workers, multi=a.multi, graphics=a.graphics, clean=a.clean, clean_pace=a.clean_pace, clean_take=a.clean_take, images=images, brand=kit, choice=choice,
                       say=lambda m: print(f"  {m}", file=sys.stderr))
            print(f"{len(res.edl.clips)} clip(s) -> {a.out}" + (f"; rendered {len(res.videos)}" if res.videos else ""))
            return 0
        edl = None if a.cmd in ("plan",) else load_edl(a.edl)
        if a.cmd == "usage":
            from .provenance import summary
            print(summary(edl)); return 0
        if a.cmd == "validate":
            print(f"ok: {len(edl.clips)} clip(s), {len(edl.transcript)} words"); return 0
        if a.cmd == "plan":
            from .planner import AnthropicLLM, PlannerOptions, plan_edl, load_transcript
            from .planner.source import probe_source
            tr = load_transcript(a.transcript, a.language)
            src_path = Path(a.media_root) / a.source
            if src_path.is_file():
                source = probe_source(src_path, uri=a.source)
            elif a.duration:
                from .planner import SourceInfo
                source = SourceInfo(uri=a.source, duration=a.duration)
            else:
                raise ValueError(f"{src_path} not found; pass --duration to plan from the transcript alone")
            if a.answers:
                from .planner import ReplayLLM
                llm, score_llm = ReplayLLM(a.answers), None
            else:
                llm = AnthropicLLM(a.model)
                score_llm = AnthropicLLM(a.score_model) if a.score_model else None
            opts = PlannerOptions(job_id=a.job_id, min_clip=a.min_clip, max_clip=a.max_clip, target_clips=a.clips,
                                  spread=a.spread,
                                  cutout=a.cutout, immersive=a.immersive, speaker_name=a.speaker_name, speaker_title=a.speaker_title,
                                  on_progress=lambda m: print(f"  {m}", file=sys.stderr))
            try:
                plan = plan_edl(tr, source, llm, opts, score_llm=score_llm)
            except Exception:
                if a.answers and getattr(llm, "pending", None):
                    print(f"{len(llm.pending)} prompt(s) need an answer: see {a.answers}/pending/", file=sys.stderr)
                raise
            Path(a.out).write_text(plan.edl.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
            if a.report:
                Path(a.report).write_text(json.dumps(plan.report, ensure_ascii=False, indent=2), encoding="utf-8")
            from .provenance import summary
            print(f"planned {len(plan.edl.clips)} clip(s) -> {a.out}")
            print(summary(plan.edl))
            for w in plan.report["warnings"]:
                print(f"  warning: {w}", file=sys.stderr)
            return 0
        if a.cmd == "compile":
            from .compile import compile_clip
            clip = next((x for x in edl.clips if x.id == a.clip), None)
            if clip is None:
                raise EDLError(f"no clip {a.clip!r}")
            res = compile_clip(edl, clip, a.out, media_root=a.media_root, gsap_js=a.gsap,
                               prepare=not a.no_media)
            print(f"wrote {res.project_dir} ({res.duration:.2f}s, scenes: {', '.join(res.scenes) or 'none'})")
            return 0
        if a.cmd == "render":
            from .pipeline import render_clip
            out, _ = render_clip(edl, a.clip, a.out, workdir=a.workdir, media_root=a.media_root,
                                 gsap_js=a.gsap, workers=a.workers, matte_device=a.matte_device,
                                 run_check=not a.no_check)
            print(f"rendered {out}"); return 0
    except (EDLError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr); return 2
    except Exception as e:  # RenderError and friends
        print(f"error: {e}", file=sys.stderr); return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
