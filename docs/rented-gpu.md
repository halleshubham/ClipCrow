# A worker on a rented GPU pod (Race Engineering, tried 2026-11-19)

ClipCrow's worker only makes outgoing HTTPS calls, so any rented GPU machine with internet can run it. This records what worked on a Race Engineering RTX 3090 pod and the scripts
in `scripts/rented-gpu/`. Nothing here is automated yet (no code starts or pauses pods).

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
