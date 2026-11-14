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
   - *Accounts and plans:* put a `plans.json` in the `/data` volume and set `CLIPCROW_PLANS=/data/plans.json`. The built-in
     plan numbers are placeholders and there are no prices in the code.
   - *Razorpay:* `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`; in the Razorpay dashboard add the webhook
     `https://<your domain>/api/billing/razorpay/webhook` for `subscription.*` and `payment_link.paid`.
   - *Posting:* `SOCIOBIRD_API_URL` (the API root, `https://go.sociobird.app/api`: ClipCrow adds `/public/v1/...`), `SOCIOBIRD_API_KEY` (and `CLIPCROW_SOCIOBIRD_SHARED=1` if accounts are on).
   - *R2:* see section 3.
4. **Deploy.** The first start takes a minute (health check `start_period` is 90 s). Then, in the container terminal:
   `clipcrow selftest` should print `ok` for every line, ending with a real render.

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

What is still capped near 100 MB is anything that goes THROUGH the app host: ordinary API requests, and a GPU worker sending a rendered clip back.
That is why clips longer than `CLIPCROW_WORKER_MAX_CLIP_SECONDS` (default 100 s) render on the server. Sending worker results through the bucket as well
(presigned PUT) would lift that limit; it is not built yet.

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

A clip over `CLIPCROW_WORKER_MAX_CLIP_SECONDS` (default 100) is rendered on the server even when a worker is online: its MP4 is large (about 0.85 MB
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
