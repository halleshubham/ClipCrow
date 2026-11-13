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
- Layer order and push-ins: footage z1, text behind z2, cutout z3, captions z4, cards z5. A push-in scales footage,
  `#behind` and `#cutout` with the same tween, or the text slides across the person. `compile_clip` clears stale
  `compositions/*.html` (a re-render reuses the folder). Without a matte the clip renders without text-behind and says
  so (`CompiledClip.warnings`, the provenance `notes`); the matte is cached by a hash of the prepared footage.
- Branding: the cover is the TOP scene (z9) and shows from frame 0 with no fade-in; its words are the hook (else the title),
  never a new model call (replay tests key on the prompt hash). Cover/logo/outro files are COPIED into `<out>/brand/` and
  named by assets `brand-logo` / `brand-outro`; the EDL never points outside the job folder. Logos are PNG/JPEG/WebP only,
  sniffed by magic bytes (no SVG). The outro is joined after the render (`branding.append_outro`); a join failure delivers
  the clip without it and a warning. `render_one` must move `cN.new.cover.png` to `cN.cover.png`. Editor toggles flip
  `enabled`, they never drop the style.
- Explainer pictures: the model picks a link BY NUMBER or writes an image description; it never supplies an address.
  Every link goes through `assets.urlguard.assert_public_url` (on submit, before capture, and for each browser
  request). Assets are fetched before render into `<out>/assets` (`asset_root`), never at render time; a failed picture
  is dropped with a note. Generated media must be `ai="generated"` so the disclosure flag turns on; keep the editor's
  removal pruning orphan assets.
- Marathi/Hindi copy must be Devanagari; keep tests with Devanagari fixtures.
- Default font is Noto Serif Devanagari (`fonts.DEFAULT_DEVANAGARI`). It lacks Latin letters: ship the Noto Serif
  companion (`fonts.companion`) whenever the text has any, or Latin falls back to an unknown system font.

## Web service (`clipcrow/service/`)
- `clipcrow serve --demo` runs it with no key, no Whisper (recorded answers); the browser test is `scripts/e2e_ui.js`.
- The UI is plain JS with no build step. **Never put job data into markup**: use `textContent`/`el()`; a test bans
  `innerHTML` and inline handlers. The CSP forbids inline scripts and styles and third-party requests.
- Ids in URLs are strict hex (`jobs.JOB_ID`, `CLIP_ID`); files are resolved inside the job directory. Show a person
  only `EXPECTED` errors' messages; unexpected ones get a generic message and a server log entry.
- A pipeline implements `check_ready()`, `run(job, video, out, hooks, say)` and `render_clip(...)` (re-render after an
  edit); progress goes through `make.Hooks`.
- Accounts: every job route goes through `job_or_404(job_id, request)` (owner check; another user's job is a 404). A
  new route that takes a job id must too. Passwords are scrypt, sessions are stored hashed, sign-in errors do not say
  whether an email exists. Wrong password on a signed-in action is 403, never 401 (the UI treats 401 as "sign in").
- Billing: plans change and minutes are added ONLY from a signature-verified Razorpay webhook (raw body, constant-time
  compare); amounts come from the catalog, never the request; events are idempotent; no prices in code. Deleting an
  account cancels the subscription first and aborts if that fails.
- Minutes: reserve at submit (atomic), refund once on failed/cancelled (`JobRunner.on_finish`); keep both when adding a
  job state. Retention never touches queued/running jobs.
- Posting: captions are escaped plain text; only checked platforms are allowed; with accounts on it needs
  `CLIPCROW_SOCIOBIRD_SHARED=1` because the key is the operator's. AI-generated media needs the person's confirmation.
- Object storage: keys come only from a validated job id and the allow-list `storage.REL`, never from user text. ID
  patterns end in `\Z`, not `$` (`$` also matches before a trailing newline). Deleting a job/account or expiring one
  purges the bucket FIRST and aborts/retries if that fails. A clip is served from the bucket only when the local file is
  missing or (redirect mode) byte-identical (`storage.signature`). Presigned links live at most an hour; the bucket is
  never public.
- Direct upload: the part URLs MUST sign `ContentLength` (so a URL cannot store more than announced); the finished object's size
  must equal the announced size; the upload id IS the job id (the object lands at `jobs/<id>/source.<ext>`); options are validated
  BEFORE the multipart upload is completed; the source is probed by presigned URL, never by a person-supplied one; abandoned
  uploads expire after 24 h. The CSP `connect-src` gets the bucket origin only when direct upload is on. A part PUT from a
  test must send `Content-Type: application/octet-stream` (urllib's default form type breaks the S3 test server).
- Styles (`clipcrow/styles.py`, `service/style_store.py`): a reference video is MEASURED, never interpreted by a model; `derive` turns the
  numbers into `StyleSettings`, a closed set of bounded fields, and that is all a style may change (clip length, caption group size
  and pause, push-in rhythm). The reference is measured in a temp folder and deleted. A style belongs to an account (same owner
  scoping as the brand kit, ids strict hex ending in `\Z`), is erased with the account, at most 10 per account, one measuring at a
  time. A job COPIES the settings (`Params.style`), so editing or deleting a style never changes work already made; an explicit clip
  length from the person beats the style's. Timing stays in code: `punch.plan_punch_ins` takes `min_gap`/`max_per_clip`, its defaults
  must stay equal to the module constants (replay tests depend on them). Caption look from frames is Phase 2 (not built).
- Container: the image is built by `image.yml` and smoke-tested by `scripts/smoke_image.sh` (a real render inside it); FFmpeg,
  fonts and GSAP come from the same scripts CI uses. `DRAIN_TIMEOUT_SECONDS` + the 20 s server wait must stay below the
  compose `stop_grace_period`. A drained job is requeued, never marked failed or refunded. The HyperFrames pin in the
  Dockerfile must equal `render.HYPERFRAMES_VERSION` (the build checks). HyperFrames' contrast audit is a warning, not a
  reason to fail a render (`render.check_warnings`).
- Clip edits are `service.editor.ClipEdit`, a closed set of fields applied by the pure `apply_edit`; never accept a raw
  EDL or a path from the browser. The AI's `decided` list is not rewritten by a person: add a note instead.
  All edl.json writes go through the runner's edit lock; a render finishes only the latest edit (`_gen`).

## CI
A green pipeline closes a task, not the push: after pushing, confirm the commit's workflow is green.
