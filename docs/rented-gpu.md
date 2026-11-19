# A worker on a rented GPU pod (Race Engineering, tried 2026-11-19)

ClipCrow's worker only makes outgoing HTTPS calls, so any rented GPU machine with internet can run it. This records what worked on a Race Engineering RTX 3090 pod and the scripts
in `scripts/rented-gpu/`. The server can now pause and wake the pod by itself (see "Pause and resume per job" below).

## What was found
- **API** (`https://api.raceengineering.ai`, Bearer key with `read`/`deploy` scopes): `GET /v1/gpu/gpu-list/`, `GET /v1/gpu/templates/`, `POST /v1/gpu/start-pod/` (`slice_id`, `template_id`, `disk_size`, `enable_ssh`),
  `GET /v1/gpu/pod-list/`, `POST /v1/gpu/pause-pod/`, `POST /v1/gpu/resume-pod/`, `POST /v1/gpu/terminate-pod/`, `GET /v1/gpu/resume-status/?pod_id=`; `Idempotency-Key` header; 10 deploy-type calls a minute.
  Only the built-in templates deploy through the API (no custom Docker image). `enable_ssh` needs an SSH key on the account: without it, use `enable_ssh=false` and drive the pod through its Jupyter REST API
  (`/api/terminals`, `/api/contents`, token from `web_url`).
- **Pause and resume**: pause about 10-14 s, resume 3-5 s, but a resume starts a NEW container. ONLY `/workspace` (the pod's `disk_size`) survives; `/root`, `/opt`, `/usr/local`, `/tmp` and installed packages do not.
  So everything lives in `/workspace` and `post_resume.sh` recreates the few links outside it. A paused pod showed no visible compute charge.
- **Network from the pod**: the Ubuntu apt mirror is unusably slow (use `ftp.iitm.ac.in/ubuntu` for the few Chrome libraries, unpacked with `dpkg -x` into `/workspace/chromelibs`); GitHub, npm, PyPI, Hugging Face and our server are fast.
- **Prices seen live** (INR/hour): RTX 3090 45, PRO4500 55, RTX 4090 69, RTX 5090 91 (the pricing page said less for a 4090).
- **Measured on the 3090** (68 s Marathi street-interview clip): SraVaani 20 s (reads the Marathi correctly); Whisper large-v3 51 s incl. first model download (mostly Hindi words: poor on Marathi); WavLM voices 15 s (1 voice: the reporter
  and interviewee merged, see the roadmap A3); the 6 render integration tests 5 min 14 s.

## The scripts (copy to `/workspace` on the pod)
`setup_pod.sh` (Node 22, static ffmpeg with NVENC, HyperFrames 0.9.29 + its Chrome, fonts, GSAP, a Python environment that reuses the template's CUDA PyTorch; run once), `chromelibs.sh` (Chrome's libraries from the fast mirror),
`env.sh` (paths and variables), `post_resume.sh` (run after EVERY resume), `check_stack.sh` (OK/MISSING per part), `start_worker.sh` / `stop_worker.sh`.
The SraVaani model is NOT in the repo: download `ARTPARK-IISc/SraVaani-1.0` at the revision pinned in `clipcrow/asr_sravaani.py` with an account that has access, and copy it to `/workspace/models/sravaani`
(`CLIPCROW_SRAVAANI_DIR`) so the Hugging Face token never goes onto the pod. `worker.env` (mode 600) holds `CLIPCROW_WORKER_TOKEN`.

## Cautions
- The worker token lets its holder fetch job media and return results. On rented third-party hardware, give the pod its own token if the server ever supports it, and rotate the token after a trial.
- docs/deploy.md: only run a worker you control when other people's media may reach it.
- An idle running pod bills by the minute. Pause it when no GPU work is expected.

## Pause and resume per job (automatic)
Set these in the server's environment (Coolify), never in the repository:

| Variable | Meaning |
|---|---|
| `RACE_GPU_API_KEY` | the provider API key (a key with the `deploy` scope; read-only keys cannot pause or resume) |
| `RACE_POD_ID` / `RACE_POD_IDS` | the pod's id (from `GET /v1/gpu/pod-list/`); `RACE_POD_IDS` lists several in order of preference, each `id` or `id:hourly-INR` (e.g. `7401:45,8123:91`): when a pod has no free GPU (or its worker will not start) the next is woken |
| `CLIPCROW_POD_HOURLY` | the pod's price in INR an hour (default 45), used for the daily cap and for estimates |
| `CLIPCROW_POD_IDLE_MINUTES` | pause after this long with nothing to do (default 10) |
| `CLIPCROW_POD_MAX_DAILY_INR` | stop waking the pod once this much has been spent today (default 500; 0 = no cap) |
| `CLIPCROW_POD_BOOT` | what to run on the pod after a resume (default `bash /workspace/post_resume.sh; bash /workspace/start_worker.sh`) |
| `CLIPCROW_GPU_RATES` | JSON of INR an hour per worker name, e.g. `{"race-3090": 45, "home": 20}`; `"*"` is a price for every other machine, so the home GPU is costed the same way |
| `CLIPCROW_WORKER_PICKUP_SECONDS` | raise to 300 or more: a job waits for the pod to wake (about a minute and a half) |

How it works: a job that needs a GPU while the pod sleeps is queued for it; the controller resumes the pod, runs the boot command in the pod's Jupyter terminal (a resume
is a new container), and the worker there starts polling. With nothing pending or running for the idle time the pod is paused again; a paused pod is not billed for compute.
The controller never terminates a pod. The pod's worker is started with `--pool rented`; the home worker uses the default `--pool home`.

On the New job page a person chooses **Automatic**, **Home GPU only** or **On-demand GPU only**, and sees a rough cost estimate for each from the video's length
(worked out in the browser from `gpu_cost` planning numbers sent with the config, plus a margin (`CLIPCROW_MARGIN_PERCENT`, default 50); not a quote). Each job's "AI usage and disclosure" lists the GPU time of every task and its cost in INR; tasks that overlap on one
machine (the worker's `--parallel 2` render slots) share its hourly price. When several workers are idle the stronger one (`power`: a CUDA GPU over a CPU) is offered a task first.

**The worker token on the pod** is a real secret of the production server: rotate `CLIPCROW_WORKER_TOKEN` after the trial or when the pod is given up.

## Fallbacks when the chosen machine is not available
For a job that asked for the on-demand GPU: the preferred pod, then the next pod in `RACE_POD_IDS`, then any other connected worker (e.g. the home GPU), and only then the server. A job that asked for the
home GPU tries the on-demand pod when no home worker is connected. A second pod needs the same setup as the first (`setup_pod.sh`, then `post_resume.sh` and `start_worker.sh`), with its own
`/workspace/worker.env` holding the worker token and `WORKER_NAME` (e.g. `race-5090`); put that name in `CLIPCROW_GPU_RATES` so its time is priced.
