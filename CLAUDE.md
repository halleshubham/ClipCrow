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

## Rules
- **Everything from the EDL or the LLM is untrusted**: restricted shapes in `edl.py`, HTML-escaped in
  `compile.py`, media confined to `--media-root`. Never interpolate raw EDL text into HTML/CSS/JS.
- The LLM only picks sentence ids and short strings; cut points, lengths and overlay timing are code.
- The render browser must not use the network: GSAP is vendored from npm by `scripts/fetch_gsap.sh`
  (never committed; GreenSock licence). Fonts for non-Latin text are shipped per project.
- Pin the HyperFrames version (`render.HYPERFRAMES_VERSION`): another Chrome/CLI build changes pixels.
- Licence is AGPL-3.0. Do not copy code from OpenShorts' `cloud/` directory (separate commercial licence).
- A speaker change is a hard cut (a jump between two `reframe` keyframes), never a pan; the active speaker comes
  from mouth movement normalised per person (`reframe.normalise_activity`) with hysteresis. Keep both when changing it.
- Marathi/Hindi copy must be Devanagari; keep tests with Devanagari fixtures.
- Default font is Noto Serif Devanagari (`fonts.DEFAULT_DEVANAGARI`). It lacks Latin letters: ship the Noto Serif
  companion (`fonts.companion`) whenever the text has any, or Latin falls back to an unknown system font.

## Web service (`clipcrow/service/`)
- `clipcrow serve --demo` runs it with no key, no Whisper (recorded answers); the browser test is `scripts/e2e_ui.js`.
- The UI is plain JS with no build step. **Never put job data into markup**: use `textContent`/`el()`; a test bans
  `innerHTML` and inline handlers. The CSP forbids inline scripts and styles and third-party requests.
- Ids in URLs are strict hex (`jobs.JOB_ID`, `CLIP_ID`); files are resolved inside the job directory. Show a person
  only `EXPECTED` errors' messages; unexpected ones get a generic message and a server log entry.
- A pipeline implements `check_ready()` and `run(job, video, out, hooks, say)`; progress goes through `make.Hooks`.

## CI
A green pipeline closes a task, not the push: after pushing, confirm the commit's workflow is green.
