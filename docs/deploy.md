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
   - *Posting:* `SOCIOBIRD_API_URL`, `SOCIOBIRD_API_KEY` (and `CLIPCROW_SOCIOBIRD_SHARED=1` if accounts are on).
   - *R2:* see section 3.
4. **Deploy.** The first start takes a minute (health check `start_period` is 90 s). Then, in the container terminal:
   `clipcrow selftest` should print `ok` for every line, ending with a real render.

## 3. Cloudflare R2 (optional, recommended when disk is limited)

1. R2 > create a **private** bucket. Create an API token with *Object Read & Write* limited to that bucket.
2. Set `R2_ACCOUNT_ID` (or `R2_ENDPOINT` for the EU jurisdiction), `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`.
3. Finished jobs are mirrored to the bucket in the background. Useful knobs: `CLIPCROW_R2_REDIRECT=1` (playback and downloads
   come straight from R2, which offloads your server's bandwidth), `CLIPCROW_LOCAL_CLIP_DAYS=3` (free local disk once an identical
   copy is in R2), `CLIPCROW_R2_BACKUP_SOURCE=1` (also keep the uploaded original there).
4. R2 holds job outputs, **not the database**: back up the `/data` volume (it contains `clipcrow.db` with accounts and the minutes
   ledger).

## 4. Things that will bite you

- **Cloudflare in front of the domain:** the orange-cloud proxy limits request bodies (100 MB on the free plan), so large uploads
  fail. Use a DNS-only record for this host, or a plan with a higher limit. (Uploading straight to R2 is future work.)
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
