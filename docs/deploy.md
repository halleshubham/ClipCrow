# Deploying ClipCrow with Docker and Coolify

The image is built by GitHub Actions (`.github/workflows/image.yml`), smoke-tested (it renders a clip *inside* the built
image), and pushed to `ghcr.io/halleshubham/clipcrow`. Coolify pulls it; nothing is built on your server.

| Tag | What | Use |
|---|---|---|
| `latest` | CUDA 12 libraries included (also runs on CPU) | a server with an NVIDIA GPU (L4, ...) |
| `cpu` | smaller, no CUDA libraries | a CPU-only server |
| `sha-<7>` / `sha-<7>-cpu` | one exact build | pin it for a rollback (`CLIPCROW_TAG`) |
| `<version>` / `<version>-cpu` | pushed for a `v*` git tag | releases |

The package is **private** (GSAP and a GPL FFmpeg build are inside). Keep it that way.

## 1. The server

- Linux x86-64 with Docker. Plan on **8+ vCPU, 16 GB+ RAM**: Chrome renders and x264 encodes are CPU-bound, and jobs run one at a
  time. Disk: uploads can be up to 4 GB each; size `/data` for your retention settings (or use R2, below).
- **GPU (optional):** NVIDIA driver (535 or newer for CUDA 12) and `nvidia-container-toolkit`. Check it from the host:
  `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi`. The GPU speeds up Whisper. The person matte
  (text behind the speaker) uses HyperFrames' own `remove-background`: whether it uses the GPU depends on HyperFrames and has
  **not been verified**; expect about 2 fps on CPU (a 60 s reel is about 14 minutes) until you measure it.
- Log in to the registry once on the server, so it can pull the private image: `docker login ghcr.io -u <github user> -p <token>`
  with a token that has only `read:packages`.

## 2. Coolify

1. **Project > New resource > Docker Compose.** Paste `docker-compose.gpu.yml` (or `docker-compose.yml` for CPU), or point
   Coolify at this repository with the compose path. There is no `build:` key, so Coolify only pulls.
2. Pick the server. Set the **domain** on the `clipcrow` service (port 8000); Coolify's proxy handles TLS.
3. **Environment variables** (the compose file lists them all; empty means off):
   - *Access:* set `CLIPCROW_TOKEN` (one shared token) **or** `CLIPCROW_ACCOUNTS=1` (sign-up and per-user jobs). Without either
     the container refuses to start on a public address, on purpose.
   - *Planner:* `ANTHROPIC_API_KEY`. For Marathi and Hindi also `CLIPCROW_INDIC_MODEL=large-v3`.
   - *Pricing (one plan, pay as you go):* the built-in catalogue is ONE plan, "Pay as you go": a prepaid balance in minutes of video, recharged with any amount; one minute of video uploaded
     uses one minute of balance (at least `min_job_minutes` a job); minutes never expire; a failed or cancelled job gives its minutes back. The numbers are in `plans.json` (`CLIPCROW_PLANS`), key `payg`, and can be overridden by the environment (`CLIPCROW_PRICE_PER_MINUTE` in rupees, `CLIPCROW_MIN_JOB_MINUTES`, `CLIPCROW_SIGNUP_BONUS_MINUTES`):
     `price_paise_per_minute` (800 = Rs 8), `min_job_minutes` (3: the least any job is charged), `min_recharge_paise` (10000), `max_recharge_paise` (2000000), `signup_bonus_minutes` (10, once per new account),
     `bonus_tiers` (`[[200000, 5], [500000, 10]]`: from Rs 2000 add 5% minutes, from Rs 5000 add 10%). Set them, then check them against what jobs really cost: the Operator page's Money section shows each job's charge, cost and markup against your target
     (`CLIPCROW_MARGIN_PERCENT`, a markup on cost, 50 by default) and suggests the price and the minimum charge that would reach it; `scripts/job_costs.py --margin 100` does the same from outside.
     No Razorpay *subscription* is needed: recharges are Razorpay payment links, and the webhook only needs the `payment_link.paid` event. Subscription plans still work if a plan with a
     `razorpay_plan_id` is added. The landing page and the New job page read the price from `GET /api/pricing`.
   - *Accounts and plans:* put a `plans.json` in the `/data` volume and set `CLIPCROW_PLANS=/data/plans.json`. The built-in
     plan numbers are placeholders and there are no prices in the code.
   - *Razorpay:* `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`; in the Razorpay dashboard add the webhook
     `https://<your domain>/api/billing/razorpay/webhook` for `subscription.*` and `payment_link.paid`.
   - *Email (needed when accounts are on: sign-in is by emailed code):* `RESEND_API_KEY` and `MAIL_FROM` (`ClipCrow <support@shackyapps.in>`; the domain needs the SPF/DKIM/DMARC
     records Resend shows you). Optional: `MAIL_REPLY_TO`, `CLIPCROW_EMAIL=resend|smtp|off`, a backup provider `CLIPCROW_EMAIL_FALLBACK=smtp` with
     `SMTP_HOST`, `SMTP_PORT` (587), `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_SECURITY` (`starttls`, `ssl`; `none` only for localhost), and `CLIPCROW_PUBLIC_URL`
     (`https://clipcrow.shackyapps.in`: the links in mails are built from it, never from a request header). **Set these before deploying a build that has
     email sign-in, or nobody can sign in** (the server starts and prints a warning). `CLIPCROW_SECRET_KEY` (optional) keys the sign-in codes; without it a
     random key is made once in `<data>/clipcrow.secret` (mode 600).
   - *Sample job and welcome mail:* every new account gets one welcome mail and, when `CLIPCROW_SAMPLE_DIR` is set, a finished **sample job** copied from that folder (it costs the
     person no minutes and retention never deletes it). Make the folder from any finished job on the server: `clipcrow export-sample <job id> --data /data --to /data/sample`, then set
     `CLIPCROW_SAMPLE_DIR=/data/sample`. Pick a short, good-looking recording whose people agree to its use: every new account can watch it. The original video is not exported.
   - *Landing page and dashboard:* the marketing page is the front door at `/` (`clipcrow/service/static/index.html`, with `landing/landing.css` and `landing/landing.js`: no inline script, no third-party request); the app is at
     `/dashboard` (hash routes such as `/dashboard#/jobs/<id>`; mail links use this form). `/landing/` redirects to `/`, and an old link or bookmark like `/#/jobs/<id>` is sent on to `/dashboard#/jobs/<id>` by the landing page.
     Someone already signed in sees "Open dashboard" on the landing page. To host the landing page elsewhere (Cloudflare Pages), copy `index.html`, `landing/`, `icons.css`, `logo.png`, `logo-mark.png`, `icon-512.png` and the favicons beside it
     and change the "Start free" and Terms/Privacy links to the app's `/dashboard` address.
   - *Operator settings (no redeploy):* about 35 settings can be changed on the Operator page (`#/admin/settings`) and apply at once: pricing (price, minimum charge, free minutes, recharge limits, two bonus tiers, plan name), the margin target and cost rates,
     sign-up (open or closed, a daily cap, blocked mail domains, extra operators), per-person limits and the upload size, how long files are kept, GPU behaviour (fallback, workers switched off, pickup wait, the pod's idle time and daily spend), the alert address, the planning model and framing threads.
     They are saved in `<data>/settings.json` (mode 600) and beat the environment and `plans.json`; Reset puts one back to what the server started with. Secrets are never settings: the page lists which outside services are connected, yes or no, never a value.
   - *Operating:* `CLIPCROW_ADMINS` (comma-separated emails that see the Operator page), `CLIPCROW_ALERT_EMAIL` (who gets alert mails: disk nearly full, jobs stuck,
     failures, GPU tasks nobody can take, mail failing), per-person cost limits `CLIPCROW_MAX_ACTIVE_JOBS` (3), `CLIPCROW_MAX_JOBS_PER_DAY` (30),
     `CLIPCROW_MAX_JOB_MINUTES` (240), `CLIPCROW_MAX_SIGNUPS_PER_DAY` (200; 0 turns a limit off), `CLIPCROW_BLOCKED_EMAIL_DOMAINS` (added to the built-in
     list of throw-away mail domains). `GET /api/health` answers `{"ok": true}` for an uptime monitor.
   - *Posting, per person (accounts on):* each person connects their OWN SocioBird on their Account page, under Posting: by pasting their API key (SocioBird: Settings, Public API; the plan must include API access), or, when you have registered
     ClipCrow in SocioBird, with a "Connect with SocioBird" button (OAuth). The key or token is encrypted with a key derived from the server secret (`CLIPCROW_SECRET_KEY`, or `<data>/clipcrow.secret`: **back that file up with the database, or every
     connection must be made again**) and is never shown or logged. Nobody posts through someone else's SocioBird. For OAuth: in SocioBird's Developer settings create the ClipCrow app with the redirect URL
     `https://<your domain>/api/sociobird/callback`, then set `SOCIOBIRD_OAUTH_CLIENT_ID` and `SOCIOBIRD_OAUTH_CLIENT_SECRET` (optionally `SOCIOBIRD_OAUTH_URL`, default `https://go.sociobird.app`, and `SOCIOBIRD_OAUTH_TOKEN_URL`).
     **The OAuth path follows SocioBird's source code and is not yet tried against the live service**: do the manual round trip with a second SocioBird account first (`docs/sociobird-per-user-task.md`, step 0). The pasted key path needs none of this.
   - *Posting, shared (optional):* `SOCIOBIRD_API_URL` (the API root, `https://go.sociobird.app/api`: ClipCrow adds `/public/v1/...`), `SOCIOBIRD_API_KEY` (and `CLIPCROW_SOCIOBIRD_SHARED=1` if accounts are on).
   - *R2:* see section 3.
4. **Deploy.** The first start takes a minute (health check `start_period` is 90 s). Then, in the container terminal:
   `clipcrow selftest` should print `ok` for every line, ending with a real render.

## PostgreSQL for the accounts (recommended for a public launch)
The accounts, minutes, payments and SocioBird connections live in one database. By default it is the SQLite file `/data/clipcrow.db`; with `DATABASE_URL=postgresql://user:password@host:5432/clipcrow` (add `?sslmode=require` for a remote host) the same code runs on PostgreSQL 14 or newer. Jobs, uploads and brand files are still files on `/data` (and R2): PostgreSQL does not hold them yet (`docs/roadmap.md`, I1).
1. **Create the database.** Easiest in Coolify: Resources, New, Database, PostgreSQL, with scheduled backups to your R2 bucket switched on. Or run it in the same stack with `docker-compose.postgres.yml`. Copy its internal address into a secret variable `DATABASE_URL` on the ClipCrow service (do not deploy yet).
2. **Move the data once** (it is safe to repeat, and the SQLite file is never changed): in the ClipCrow container terminal, with `DATABASE_URL` set, run `clipcrow migrate-db --data /data`. It copies people, signed-in sessions, the minutes ledger, the handled payment-event ids and SocioBird connections, then prints counts only. Keep `/data/clipcrow.secret` (or the same `CLIPCROW_SECRET_KEY`): the stored SocioBird keys can only be read with it.
3. **Deploy.** The server tells you which database it uses at start (`accounts database: PostgreSQL host/db`, never the password). If something is wrong, remove `DATABASE_URL` and redeploy: the SQLite file is exactly as it was before the move (but it will not have anything made since).
4. **Backups.** PostgreSQL is not copied by ClipCrow (the daily file copy is SQLite's). Use the database service's scheduled backups, keep one copy off the machine, and do a restore drill (`docs/operations.md`). The pool size is `CLIPCROW_DB_POOL` (default 10).
5. **Tests.** CI runs the accounts tests on both engines. Locally: `CLIPCROW_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/clipcrow_test pytest -q` puts every test's accounts in a private schema of that database.

## 3. Cloudflare R2 (optional, recommended when disk is limited)

1. R2 > create a **private** bucket. Create an API token with *Object Read & Write* limited to that bucket.
2. Set `R2_ACCOUNT_ID` (or `R2_ENDPOINT` for the EU jurisdiction), `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`.
3. Finished jobs are mirrored to the bucket in the background. Useful knobs: `CLIPCROW_R2_REDIRECT=1` (playback and downloads
   come straight from R2, which offloads your server's bandwidth), `CLIPCROW_LOCAL_CLIP_DAYS=3` (free local disk once an identical
   copy is in R2), `CLIPCROW_R2_BACKUP_SOURCE=1` (also keep the uploaded original there).
4. **CORS (needed for direct uploads).** Browsers upload to the bucket's address, so the bucket must allow your site. Run, with a token
   that may change bucket settings (the Object Read & Write token above cannot):
   `clipcrow r2-cors --origin https://<your domain>` (add `--show` to see the current rule). Or in the Cloudflare dashboard, bucket
   > Settings > CORS policy, paste:
   `[{"AllowedOrigins": ["https://<your domain>"], "AllowedMethods": ["PUT", "GET", "HEAD"], "AllowedHeaders": ["*"], "ExposeHeaders": ["ETag"], "MaxAgeSeconds": 3600}]`.
   `ExposeHeaders: ETag` is essential: the browser needs each part's ETag to finish the upload. Uploads then show as parts in the
   job page's progress bar; `CLIPCROW_UPLOAD_PART_MB` changes the part size (default 64) and `CLIPCROW_DIRECT_UPLOAD=0` turns direct
   upload off.
5. R2 holds job outputs, **not the database**: back up the `/data` volume (it contains `clipcrow.db` with accounts and the minutes
   ledger).

### How big a recording can be (validated)

With direct upload on, the browser sends the video to R2 in 64 MB parts through presigned URLs on the bucket's own host
(`<account>.r2.cloudflarestorage.com`), so those bytes never pass the app or Cloudflare's proxy (which caps one request near 100 MB on the free
plan). Checked on the live server on 2026-11-14: a 3 GB upload started with 48 parts, every URL on the R2 host, and was cancelled cleanly; a 5 GB one
was refused with "larger than the 4096 MB limit". So the only size cap for a source video is ClipCrow's own `clipcrow serve --max-upload-mb`
(default 4096; the bucket allows far more: 10 000 parts x 64 MB). A raw 1080p hour is roughly 3-6 GB, so raise it if you upload long raw recordings.

What is still capped near 100 MB is anything that goes THROUGH the app host: ordinary API requests. A GPU worker's finished clip does NOT: with a bucket
the worker asks for presigned part URLs (`POST /api/worker/tasks/<id>/direct-upload`), PUTs the MP4 to the bucket in 32 MiB parts and says it is done
(`.../direct-done`); the server completes the upload, checks the size, copies the object to its disk, deletes it from the bucket and probes it like any other
worker clip. The worker never holds a storage key; the object is `worker/<task id>.mp4`. So clips of any length render on the GPU (up to
`CLIPCROW_WORKER_MAX_DIRECT_MB`, 2048). `CLIPCROW_WORKER_DIRECT=0` turns it off. Without a bucket, or when no online worker can do this (an older image), clips
longer than `CLIPCROW_WORKER_MAX_CLIP_SECONDS` (default 60) still render on the server. Restart the worker on the new image to get it.

### Testing direct uploads without Cloudflare

An S3-compatible server on localhost is enough: `pip install 'moto[server]'`, start `ThreadedMotoServer`, create a bucket, set its CORS
with `S3Storage.set_cors(["http://127.0.0.1:8767"])`, run `clipcrow serve --demo` with `R2_ENDPOINT=http://127.0.0.1:<port>`,
`R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` and `CLIPCROW_UPLOAD_PART_MB=5`, then
`node scripts/e2e_direct_upload.js <video over 3.5 min> <dir> direct` (also `fallback` and `expose`).

## 4. A GPU worker on your own machine (optional)

Speech recognition is the slowest step on a CPU-only server (a long Marathi video can take longer than the video). If you have a
machine with an NVIDIA GPU (8 GB is enough for `large-v3`), it can do that step for the server. The worker only makes outgoing HTTPS
requests, so the machine opens no port and needs no tunnel.

1. **Make a secret** (at least 16 characters): `openssl rand -hex 32`. Set it as `CLIPCROW_WORKER_TOKEN` in Coolify and redeploy. Leave it
   empty and the feature does not exist (the routes answer 404).
2. **On the GPU machine** (Linux, NVIDIA driver 535 or newer). The easiest way is the same image the server runs, so the versions match:
   ```bash
   export CLIPCROW_WORKER_TOKEN=...   # the same secret; it is read from the environment, never from the command line
   docker run --rm --gpus all --shm-size=2g --no-healthcheck -e CLIPCROW_WORKER_TOKEN -v clipcrow-models:/models \
     ghcr.io/halleshubham/clipcrow:latest clipcrow worker --server https://<your domain> --name home-gpu
   ```
   (needs `nvidia-container-toolkit`; check with `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi`).
   Without Docker: `pip install 'clipcrow[asr]' 'av<19' nvidia-cublas-cu12 'nvidia-cudnn-cu12==9.*'`, put those two libraries' folders on
   `LD_LIBRARY_PATH`, then run `clipcrow worker --server ...`. The first run downloads the model (about 3 GB).
3. **How it behaves.** The server checks whether a worker has asked for work in the last minute. If one has, a job's audio (about 0.3 MB a
   minute) is queued for it; if it does not pick the task up within `CLIPCROW_WORKER_PICKUP_SECONDS` (default 120), or stops sending
   heartbeats, or reports an error, the server transcribes by itself as before. If no worker is online the server does not wait at all.
   So switching the PC off is always safe, and the job log says which one did the work. `/api/config` shows `gpu_worker.online`.
4. **Privacy.** Audio goes only to a worker that holds the lease on that task, and only your machine. If other people use your server,
   their audio would be processed on that machine: only run a worker you control, and say so in your terms.

### The worker can render clips too

With the ClipCrow image (it has Node, Chrome, FFmpeg, the fonts and GSAP) the worker also offers to render. At start it runs the same
tool checks as `clipcrow selftest` and prints `rendering: ready` or why not (`--no-render` turns it off). The server then sends each clip's
render to it:
- The worker downloads the clip's EDL, the source video and the assets, only by names the server listed for that task. A source video is
  cached per job (so a job's other clips and later edits do not download it again) and the cache is trimmed to `--cache-gb` (20 by default).
- It renders with HyperFrames and uploads the MP4. A render task goes only to a worker whose HyperFrames version equals the server's pin
  (`render.HYPERFRAMES_VERSION`), so the pixels are the same wherever they were made: **use the same image tag on both**.
- The server never trusts the file: it probes it (a real MP4, H.264, exactly the EDL's size, about the clip's length, under
  `CLIPCROW_WORKER_MAX_UPLOAD_MB`, 300 by default) before it replaces anything, and makes the cover image and provenance itself.
  A clip that fails any check, a worker that crashes, stalls (no heartbeat) or takes over an hour: the server renders it itself.
- Framing (face tracking) and planning still run on the server. Re-renders after an edit use the worker too.
- **Cloudflare:** the clip goes back through your domain, and Cloudflare's proxy limits a request body (100 MB on the free plan). Typical
  clips are 10 to 40 MB; for longer ones use a DNS-only record for the domain, or raise the limit on your plan.
- **`--shm-size=2g` is required for rendering:** headless Chrome crashes on Docker's default 64 MB of shared memory (the compose files
  set the same for the server). Keep the `clipcrow-models` volume: the worker caches source videos there.
- The person-cutout step runs wherever the render runs. Whether HyperFrames uses the GPU for it is not verified.

### Long reels and the worker

A clip over `CLIPCROW_WORKER_MAX_CLIP_SECONDS` (default 60) is rendered on the server even when a worker is online: its MP4 is large (about 0.85 MB
per second) and would come back through Cloudflare, which caps a request at 100 MB on the free plan. Set it to `0` if your worker reaches the server
without such a limit.

### Checking Marathi / Hindi accuracy on your own audio

Devanagari speech is decoded in 15 s windows with no prompt, and the text is then corrected by the model (needs `ANTHROPIC_API_KEY`; turn it off
with `CLIPCROW_TEXT_REFINE=0`). To see the difference on a real stretch, on the worker machine:

```bash
docker run --rm --gpus all --shm-size=2g -e ANTHROPIC_API_KEY -v clipcrow-models:/models -v "$PWD":/data \
  ghcr.io/halleshubham/clipcrow:latest \
  clipcrow compare-asr /data/talk.mp4 --from 60 --to 180 --language mr --model large-v3 --refine
```

It prints each 15 s slice three times: `old` (whole-file with a script prompt), `new` (windowed, no prompt) and `refined` (corrected).

## 5. Things that will bite you

- **Cloudflare in front of the domain:** the orange-cloud proxy limits request bodies (100 MB on the free plan). With R2 configured,
  uploads go **straight from the browser to the bucket** and never touch your domain, so the limit does not apply to them: set the
  bucket's CORS rule (section 3). Without R2, large uploads do pass through the proxy and fail above the limit: use a DNS-only
  (grey cloud) record for this host, or a plan with a higher limit.
- **One instance only.** SQLite, the in-process queue and local disk mean you cannot run two copies or scale out.
- **Deploys:** on stop the app waits up to `DRAIN_TIMEOUT_SECONDS` (45) for the running job, then puts it back in the queue; it starts
  again by itself after the restart, with its minutes still reserved. Keep Coolify's stop grace period above 65 s (the compose files
  use 90 s). Waiting jobs stay queued. A running clip re-render is not requeued: it shows "render it again".
- **Rollback:** set `CLIPCROW_TAG=sha-<7>` (or `sha-<7>-cpu`) and redeploy.
- **First use of the matte** downloads a ~170 MB model into the `/models` volume; the first Whisper use downloads its model too
  (needs outbound access to HuggingFace). Both volumes survive redeploys.
- **Egress:** the server fetches pages for explainer screenshots after a guard that refuses private addresses, but DNS rebinding is
  not covered: also block private ranges at the network edge.
- Razorpay and SocioBird are written to their documented APIs and tested with fakes; **try them with a test account before real money
  or real channels**.

## Optional recogniser: SraVaani (Marathi / Hindi)
`CLIPCROW_ASR=sravaani` makes Marathi and Hindi recordings go through SraVaani (ARTPARK-IISc, MIT, runs on a CPU) instead of Whisper; any failure falls
back to Whisper and the job log says why ("SraVaani unavailable (...): using Whisper"). The server image already has what it needs (`torch` CPU, `transformers`,
`sentencepiece`, `soundfile`); the model (0.9 GB) is downloaded on first use into the `/models` volume at a PINNED revision (the repo's Python is run with
`trust_remote_code`), so set `HF_TOKEN` to a token of a Hugging Face account that has been granted access to the gated repo `ARTPARK-IISc/SraVaani-1.0`.
Set both in Coolify (`CLIPCROW_ASR=sravaani`, `HF_TOKEN=...`) and redeploy; the first job after that waits for the download. It writes every spoken word,
stumbles included, and gives its own word times (starts within about 0.1-0.3 s of ElevenLabs Scribe's on a 84 s test; ends are pulled back to where the voice
stops). Without Docker: `pip install -e '.[sravaani]' --extra-index-url https://download.pytorch.org/whl/cpu`, or point `CLIPCROW_SRAVAANI_DIR` at a folder with the files.

### Behind a proxy
`CLIPCROW_TRUSTED_PROXIES` (addresses/CIDRs, or `*`) says whose `X-Forwarded-For/-Proto` the service believes. Default: `*` when `CLIPCROW_PUBLIC_URL` is https, else loopback only.
Without it every visitor looks like the proxy and the per-IP sign-in limits become one shared bucket. Use `*` only if the container is reachable solely through the proxy.

### Worker tokens
`CLIPCROW_WORKER_TOKEN` is the main token (your own machine). Give rented pods their own: set `CLIPCROW_WORKER_TOKEN_RENTED` (at least 16 characters, different) and put THAT value in the
pod's `CLIPCROW_WORKER_TOKEN`. A pod holding it is always in the rented pool whatever it claims, so a stolen pod cannot ask for your home machine's work, and changing that one variable
revokes every pod without touching the home worker.
