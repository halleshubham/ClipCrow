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
- Devanagari speech recognition (`asr.WINDOWED_LANGS`): independent 15 s windows with the language pinned, NO prompt or hotwords (they are echoed back
  as the transcript), NO voice-activity filter (it drops speech), loop guards on (`asr.WINDOWED_DECODE`). It was found on real Marathi interviews;
  do not "improve" it with a seed. Whisper's Marathi is still Hindi-flavoured, so `textfix.refine` corrects the sentences with one model call per
  batch: only ids and text go to the model, word TIMES stay ours (1:1 keeps them, otherwise spread in proportion), an answer that no longer looks
  like the sentence is dropped, a failure keeps the raw words, and the original is saved as `transcript.raw.json`. It runs only for a transcript the
  ASR made (never a supplied one); `CLIPCROW_TEXT_REFINE=0` turns it off. `clipcrow compare-asr` runs old and new side by side on one stretch.
- Slides and graphics (`graphics.py`, the "Video has slides, cards or graphics" option, `Params.graphics`, `make --graphics`): an explainer source cuts away
  from the speaker to cards, charts and pictures that a 9:16 crop cuts in half. Nothing is learned or asked of a model: sampled frames are grouped into
  shots by a tiny thumbnail, the speaker's shot is the biggest one with a MOVING face, and a shot that is neither it nor another live camera (no face, or
  a face that never moves: a portrait on a slide) is shown whole as `Layout.fit_spans` (`track` only, ordered, hard cuts at both ends, shorter than
  `MIN_SPAN` dropped). The camera ignores faces inside a span, coverage is counted over the non-graphic samples, and a push-in never overlaps a span.
  It is opt-in (default off): without it the pipeline is unchanged. The thresholds are heuristics, tuned on synthetic video: look at a real result.
- Fumble-free cut (`cleancut.py`, option "Raw recording: cut fumbles and dead air", `Params.clean`, `make --clean`): adapted from the `fumble-free-cut` skill. It
  runs BEFORE planning: hesitation sounds, repeats (Hindi/Marathi reduplication like धीरे धीरे is only a suggestion, never applied), false starts, retake
  lines, voice in a gap with no word, and long pauses shrunk to the pace; the result is `out/clean.mp4` and the transcript remapped onto it, and EVERY
  later step (plan, framing, render, worker, re-renders, the editor's length limits) uses that video: `JobStore.working_video`, never `source_path`, for
  anything on the plan's timeline. Only what the skill applies by default is applied (no person reviews it). The editorial pass is one model call per batch
  of lines that returns line ids only (caps: 35% of a batch, never the last line); times and cut points are code. More than `MAX_REMOVED` of the recording,
  too few words, or a cue-timed transcript = leave the video alone. Audio is cut sample-exact with 8 ms fades; the frame grid keeps A/V in sync (a test
  checks the two durations). Retention deletes `clean.mp4` with the original.
  A cleaned recording is usually ONE reel of 3-8 minutes: a clip may run `edl.MAX_CLIP_SECONDS` (15 min), the job form offers 1½-3, 3-5, 5-8 minutes and
  "the whole video as one reel" (`Params.whole`, `PlannerOptions.whole`: one window, no scoring, the clip IS the recording whatever sentences the model
  named). Anything that assumed a short must follow the clip's real length: the editor's trim limit (`max_seconds_for`), the render timeout (12 x the
  clip), and the GPU worker (clips over `CLIPCROW_WORKER_MAX_CLIP_SECONDS`, default 100, render on the server because the MP4 would come back through
  a proxy that caps a request near 100 MB).
- Default font is Noto Serif Devanagari (`fonts.DEFAULT_DEVANAGARI`). It lacks Latin letters: ship the Noto Serif
  companion (`fonts.companion`) whenever the text has any, or Latin falls back to an unknown system font.

## Web service (`clipcrow/service/`)
- **UI/UX consistency is part of "done"**: read `docs/ui.md` before touching `static/`. Reuse the shared classes (`.card`, `label.field`, `.check`, `.btn`, `.chip`),
  tokens only (no literal colours), every control, including browser-drawn ones (date picker, file, range), is styled to look like the text field beside it,
  and a new screen is checked in dark AND light at 360 px. A new `<input type=...>` must be added to `STYLED_INPUT_TYPES` in `tests/test_ui_consistency.py`
  after you have looked at it. The calendar popup is the browser's: it follows `color-scheme`, do not replace it with a third-party widget (CSP, no build step).
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
- Source size: with direct upload the bytes go browser -> R2 on the bucket's own host, so Cloudflare's ~100 MB request cap does NOT apply to a source video;
  the only cap is `--max-upload-mb` (default 4096), checked at `POST /api/uploads` (413). Validated live and pinned by tests in `test_direct_upload.py`.
  Anything routed THROUGH the app host (API bodies, worker uploads) is still capped near 100 MB: do not design a feature that streams big files through it.
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
  must stay equal to the module constants (replay tests depend on them).
- The caption LOOK (`styles_look.py`) is the one place a model looks at pixels: one vision call over a few sampled frames returns
  `CaptionLook`, a closed bounded schema, and `clean()` drops any colour that is not `#RRGGBB`. A font is never taken from the model:
  it names a look and `family_for` maps it to a family we ship, so Devanagari copy keeps its font and never goes uppercase. The
  look is a bonus: if the model declines, the pacing numbers still stand. `_caption_size` must stay pixel-identical at
  `size_scale == 1.0` (every clip without a style).
- Container: the image is built by `image.yml` and smoke-tested by `scripts/smoke_image.sh` (a real render inside it); FFmpeg,
  fonts and GSAP come from the same scripts CI uses. `DRAIN_TIMEOUT_SECONDS` + the 20 s server wait must stay below the
  compose `stop_grace_period`. A drained job is requeued, never marked failed or refunded. A job a hard kill left `running` is queued again ONCE by `recover()` (`Job.attempts`), then failed with its step marked failed, so a job that takes the server down cannot loop; keep that cap. The HyperFrames pin in the
  Dockerfile must equal `render.HYPERFRAMES_VERSION` (the build checks). HyperFrames' contrast audit is a warning, not a
  reason to fail a render (`render.check_warnings`).
- GPU worker (`service/workers.py`, `clipcrow/worker.py`): a machine the operator owns PULLS speech-recognition tasks over HTTPS (no inbound
  port). One shared secret `CLIPCROW_WORKER_TOKEN` (>= 16 chars, constant-time compare, a different secret from any login; unset = the routes
  404). Everything a worker sends is untrusted: bounded by `ResultIn`, then `parse_transcript`; a worker can only ever cost time, because
  the job falls back to the server's own recognition when none is online, none picks the task up in `CLIPCROW_WORKER_PICKUP_SECONDS`, the
  lease lapses (heartbeats), or it reports an error. Audio is served only to the worker holding the lease; the copy is deleted afterwards.
  The worker reads its token from the environment, never argv. Task ids are 32 hex, checked with `re.fullmatch`.
  A RENDER task (`RemoteRenderer`) ships the EDL, the source and the clip's assets by manifest NAMES (a dict lookup on the server, `safe_join`
  on the worker: never a path built from a name), only to a worker whose HyperFrames pin equals ours. What comes back is untrusted media:
  `validate_clip` probes container, codec, size, duration and bytes before anything is replaced, and the cover and the provenance sidecar are
  made by the server, never uploaded by a worker. Any failure falls back to a local render. Name patterns use `fullmatch` (a `$` regex lets
  a trailing newline through). `Transport` methods raise `NotImplementedError`, so a test double without `upload` fails loudly.
- Clip edits are `service.editor.ClipEdit`, a closed set of fields applied by the pure `apply_edit`; never accept a raw
  EDL or a path from the browser. The AI's `decided` list is not rewritten by a person: add a note instead.
  All edl.json writes go through the runner's edit lock; a render finishes only the latest edit (`_gen`).

## CI
A green pipeline closes a task, not the push: after pushing, confirm the commit's workflow is green.
