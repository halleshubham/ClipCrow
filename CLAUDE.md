# CLAUDE.md

ClipCrow turns a transcript (and later a video) into short vertical videos. Pipeline:
transcript -> LLM planner -> **EDL (JSON)** -> HyperFrames project -> MP4. The EDL is the contract:
the planner never touches pixels, the compiler turns the EDL into HTML, HyperFrames renders it.

## Commands
```bash
pip install -e '.[dev,llm,examples,service]'
pytest -q                                          # unit tests, no network, no API key
scripts/fetch_gsap.sh && python examples/gen_assets.py
CLIPCROW_RENDER_TESTS=1 pytest -q -m render        # real render: Node 22, FFmpeg, Chrome
```

## The rules that matter most (the full rules are in `.claude/development-rules.md`)
- **Everything from the EDL, the LLM, a worker or an upload is untrusted**: closed shapes (`edl.py`), HTML-escaped (`compile.py`), media confined to `--media-root`, files sniffed and re-encoded. Never interpolate
  raw text into HTML/CSS/JS. The LLM only picks ids and short strings; cut points, lengths and timing are code.
- **Secrets** (tokens, API keys, R2 keys, cookies) are never printed, logged, committed or put in a URL. Read them from the environment.
- The render browser never uses the network; pin `render.HYPERFRAMES_VERSION`; the licence is AGPL-3.0 (never copy OpenShorts' `cloud/`).
- Marathi/Hindi copy is Devanagari (tests too). Every step after cleanup uses `JobStore.working_video`, never `source_path`.
- Web UI: read `docs/ui.md` first; reuse the shared classes and tokens; never put job data into markup (`textContent`/`el()`); dark AND light at 360 px.
- Ids in URLs are strict hex ending `\Z`; every job route goes through `job_or_404`; storage keys come only from the `storage.REL` allow-list; money and minutes change only from a verified webhook /
  the atomic reserve and refund.
- A green CI closes a task, not the push. Deploy only after CI and Image are green and no job is running.

## Where to look
- **Before touching an area, read its section in `.claude/development-rules.md`**: pipeline (EDL, render, branding, pictures, recognition, cleanup, graphics), web service (UI, accounts, billing,
  storage, uploads, styles, container, GPU worker, editor), sound library, deploying, CI.
- **`.claude/development-log.md`**: what was built and why, what real recordings taught us, open items. Add an entry when you change behaviour.
- Smaller docs: `docs/ui.md`, `docs/deploy.md`, `README.md`.
