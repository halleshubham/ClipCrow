# Development rules

The detailed rules for ClipCrow, moved out of `CLAUDE.md` so that file can stay short. Read the section for the area you are about to touch BEFORE changing
it; each rule is there because something went wrong without it. Sections: **Pipeline rules** (EDL, render, branding, pictures, speech recognition, cleanup),
**Web service** (UI, accounts, billing, storage, uploads, styles, container, GPU worker, editor), **Sound library**, **Deploying**, **CI**.
History, incidents and open items are in `development-log.md`: add an entry there when you change behaviour.

## Pipeline rules
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
- Brands are per account and several: every brand route goes through `BrandStore._dir(owner, brand)` (ids strict hex `\Z`, checked against the owner's registry, so another account's id is a KeyError/404); a job stores `Params.brand` when it is made and never follows the default afterwards.
- Branding: the cover is the TOP scene (z9) and shows from frame 0 with no fade-in; its words are the hook (else the title),
  never a new model call (replay tests key on the prompt hash). Cover/logo/outro files are COPIED into `<out>/brand/` and
  named by assets `brand-logo` / `brand-outro`; the EDL never points outside the job folder. Logos are PNG/JPEG/WebP only,
  sniffed by magic bytes (no SVG). The brand PNG overlay (`Clip.brand_overlay`, asset `brand-overlay`) is PNG-only, decoded and re-encoded to RGBA by `put_overlay`; the compile layer is `pngoverlay` (z5, under captions and logo). The outro is joined after the render (`branding.append_outro`); a join failure delivers
  the clip without it and a warning. `render_one` must move `cN.new.cover.png` to `cN.cover.png`. Editor toggles flip
  `enabled`, they never drop the style.
- Explainer pictures: the PERSON supplies them (web: `POST /api/pictures`, `service/pictures.py`; the service never opens a web page, so there is no capture or SSRF
  surface in it; `--link` capture remains a CLI-only tool and keeps `assets.urlguard.assert_public_url`). An upload is sniffed by magic bytes (no SVG), probed, RE-ENCODED
  by ffmpeg to one PNG <= 2160 px, a draft per owner (24 h, `MAX_DRAFTS`), copied into `<job>/pictures/<id>.png` only AFTER minutes are reserved, ids strict hex ending in `\Z`,
  owner-scoped, erased with the account. The model is SHOWN the pictures (labelled `[P0]`...) and picks one BY NUMBER per sentence (`Beat.picture`), each at most once, and may leave one
  unused; it never names a file or an address. The asset is `origin="upload"`, `ref=<id>`, `ai="none"`; `fetch_assets` copies it into `<out>/assets` before render (assets are never
  fetched at render time); a missing one is dropped with a note. The pictures are sent to the model, so the form says so. Generated media must be `ai="generated"` so the disclosure
  flag turns on; keep the editor's removal pruning orphan assets.
- Marathi/Hindi copy must be Devanagari; keep tests with Devanagari fixtures.
- Cancel: `make.Cancelled` is a BaseException ON PURPOSE (the recogniser, cleanup and text correction have `except Exception: carry on` handlers that must not swallow a stop). Long steps call `check()`: per recognition window
  (`asr.transcribe(check=)`), per SraVaani window, and every AI call goes through `planner.llm.Interruptible` (a helper thread polled every 0.5 s, so a cancel does not wait for a call that takes minutes; the abandoned call
  is dropped, still billed). `Job.cancel_requested` is saved at cancel time: a deploy's drain does not requeue such a job and `recover()` ends it as cancelled. The UI says "Stopping…" until it ends.
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
  What three real jobs taught (keep these true): (1) NEVER treat "no recognised word" as silence: `missed_speech` keeps voiced stretches (>= `MISSED_RUN`) between kept words,
  and `asr.fill_holes` listens again with relaxed settings (no no-speech / log-prob discard) to every voiced stretch the first pass returned nothing for; Whisper throws
  low-confidence Marathi windows away as silence. (2) The text is CORRECTED FIRST (`textfix.refine`) and the cuts are decided on the corrected text, because
  Whisper's raw Marathi is too garbled for any repeat rule or for the model. (3) Raw recordings contain retakes of whole passages, reworded: `restart` (a long
  pause, then the opening of an earlier passage), the passage/sentence rules (an earlier try that the next passage says again, share >= `SENTENCE_SHARE`, up to
  `REDO_PASSAGES` ahead) and the editing pass, which is shown the pauses. Guards: a run of three alike lines is a list, short lines are emphasis, a recap much
  shorter than the line is not a redo, a name that merely comes back is not a restart (the opening must align as a phrase). (4) The job page lists every cut with
  its original time (`GET /api/jobs/{id}/cleanup`, `cleanup.json`): a person must be able to check the cuts. The model for the editing pass, the text correction
  and the planner is `$CLIPCROW_MODEL` (default `claude-opus-5-5`); each step and its model show in the job's AI usage table.
  (5) The text correction must keep STUMBLES (repeats, an abandoned "तर मग…", every clause and question): a smoothed fumble cannot be cut later, and a dropped clause leaves
  speech with no captions; `textfix.acceptable` refuses a fix whose length or word count moves too far. (6) The editing pass sees SHORT phrases (`JUDGE_PAUSE`,
  `JUDGE_LINE_WORDS`), not the rules' sentences, so an abandoned fragment inside a sentence has a line of its own and only that line is removed.
  (7) The editing pass READS FIRST: `Review` is answered in the order `intent`, `passages`, `drop` (understanding before cuts), sees the WHOLE recording in one call with the
  lines the rules already cut marked "(cut already)", and a second call (`verify_cuts`, purpose `retake-verify`) reads the RESULT as a viewer: it may bring back an AI cut
  or drop a leftover repeat, never undo a rule. What it understood is shown on the job page. (8) Whisper can give ONE word the time of several unwritten ones ("मधे" over six
  seconds, hiding "आपल्याला माहितीय…" said twice): `asr.fill_holes` shortens any word over `LONG_WORD` and listens to the freed gap again, so the repeats exist as words and the
  repeat rules can cut them. Repeats are compared with `same_word` (spelling drift and the है/आहे family count as the same).
  A cleaned recording is usually ONE reel of 3-8 minutes: a clip may run `edl.MAX_CLIP_SECONDS` (15 min), the job form offers 1½-3, 3-5, 5-8 minutes and
  "the whole video as one reel" (`Params.whole`, `PlannerOptions.whole`: one window, no scoring, the clip IS the recording whatever sentences the model
  named). Anything that assumed a short must follow the clip's real length: the editor's trim limit (`max_seconds_for`), the render timeout (12 x the
  clip), and the GPU worker (clips over `CLIPCROW_WORKER_MAX_CLIP_SECONDS`, default 60 (a 98 s clip was 105 MB: over the proxy cap, a 413), go only to a worker that sends the MP4 to the
  bucket in presigned parts (`WorkerHub.direct_plan` / `direct_finish`, task `direct=True`); with no bucket or no such worker online they render on the server).
- Ending video gap (`Outro.gap_ms`, brand setting `outro_gap_ms`, default 400, 0-1500; `branding.append_outro(gap_ms=)`): the clip fades out (0.2 s), a black SILENT gap, the ending fades in (0.2 s); 0 = the old straight cut. The joined clip is
  longer by the gap (`validate_clip` allows it). A real render test checks the gap is dark.
- Loudness (`loudness.py`, called by `pipeline.render_clip` AFTER the outro join, `CLIPCROW_LOUDNORM=0` turns it off): two-pass `loudnorm` to -14 LUFS / -1.5 dBTP on the finished clip, picture copied, only
  audio re-encoded. A clip within 1 LU of the target with headroom is left byte for byte (a good render stays deterministic); silence, no audio, or a clip needing more than 18 dB of gain is left alone
  (raising it only raises the noise); a result that is not closer to the target is discarded; any failure keeps the original. It runs on the worker too (same `render_clip`).
- Sound cues (`cues.py`, `Clip.sfx`, job option `sounds`, editor toggle, OFF by default): which sound goes where is a FIXED table of the clip's own events (`CUE_FOR`: stat = ding, picture = shutter,
  lower third = soft whoosh, hook = pop, push-in = click), never the model's choice and never on a caption word; one cue per `MIN_GAP` (2 s) with `PRIORITY` deciding; each file is gained so its PEAK lands
  at `Sfx.level_db` (default -10 dBFS) from the peak in `sfx/manifest.json`; a cue starts `LEAD` before its event, never before 0. One ffmpeg pass (`adelay`, `volume`, `amix normalize=0`), picture
  copied, the speech keeps its channel layout; no audio / no cue / any failure leaves the clip as it was. Order in `render_clip`: cues, outro join, loudness. The editor flips `enabled`, the level stays.
- Caption motion (`CaptionStyle.animation`: pop | fade | slide | karaoke, editor select "Motion"): a CLOSED set compiled by `compile._caption_script`. `pop` is the original motion and its script must stay
  byte for byte what it was (a test pins it: same pixels for every clip made before the field existed). Karaoke tweens `color` of the word being said and sends an emphasised word back to the highlight colour.
- Transitions (`transitions.py`, `Clip.transition` flash|blur|dip|whip, `Clip.seams`, job option `transition`, editor select; OFF by default): which cuts get one is code, `hard_cuts(clip)` = the cleanup's joins
  (`seams`, set by `make()` from `cleanup.json`'s timemap through `with_seams`, clip time = cleaned-video time - source_in), speaker jumps in a tracked framing (a keyframe jump of > 0.1 within 0.12 s, never a pan) and
  both edges of a fit span; none in the first/last 0.3 s, cuts under 0.6 s apart share one effect. The look is a full-frame layer (`compile._transitions_scene`, z 8: over cards and logo, under the cover).
  Sound cues put a click on every hard cut when cues are on.
- Background music and ducking (`music.py`, `Clip.music`, brand kit, job option `music`, editor toggle, OFF by default): the track is one of the built-in ones (`clipcrow/tracks`, below) or the ACCOUNT'S OWN upload
  (the person is responsible for the right to use it; the Brand page says platforms can mute or claim it; `music_track` "" = the upload), re-encoded to one stereo AAC, 5 s-15 min, 30 MB, copied into the job as asset `brand-music`. One ffmpeg pass: looped, faded,
  gained to `level_db` (default -10) relative to the SPEECH's integrated loudness (`loudness.measure` of both), and pushed down further by `sidechaincompress` keyed on the speech while someone talks; the speech keeps
  its own channel layout (mono -> stereo lowers it by 3 dB), picture copied. No speech to measure / no audio / unreadable track = clip untouched. Order in `render_clip`: music, cues, outro join, loudness.
- Default font is Noto Serif Devanagari (`fonts.DEFAULT_DEVANAGARI`). It lacks Latin letters: ship the Noto Serif
  companion (`fonts.companion`) whenever the text has any, or Latin falls back to an unknown system font.

## Web service (`clipcrow/service/`)
- Logo: `static/logo*.png`, the favicons and `docs/assets/logo-card.png` (README) are generated by `scripts/make_logo_assets.py` from `docs/assets/logo-source.webp`; change the source and re-run, never hand-edit. The artwork is dark: on the dark UI it sits on a `--logo-tile` tile (see `docs/ui.md`).
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
  new route that takes a job id must too. There are no passwords: sign-in is an emailed 6-digit code (HMAC-stored, 10 minutes, single use, 5 tries, per-address and per-IP limits), sessions are stored hashed, sign-in errors do not say
  whether an email exists. A wrong confirmation on a signed-in action (deleting the account asks for the email typed again) is 403, never 401 (the UI treats 401 as "sign in"). Email: one `Mailer` interface (Resend default, SMTP, a fallback), mails go through `MailQueue`, nothing from outside reaches a header or markup unescaped, links come from `CLIPCROW_PUBLIC_URL`, failures are logged without address or body. New jobs pass `enforce_limits` before minutes are reserved; an operator (`CLIPCROW_ADMINS`) is exempt; `/api/admin/*` answers 404 to everyone else.
- Billing: plans change and minutes are added ONLY from a signature-verified Razorpay webhook (raw body, constant-time
  compare); amounts come from the catalog, never the request; events are idempotent; no prices in code. Deleting an
  account cancels the subscription first and aborts if that fails.
- Minutes: reserve at submit (atomic), refund once on failed/cancelled (`JobRunner.on_finish`); keep both when adding a
  job state. Retention never touches queued/running jobs.
- Posting: captions are escaped plain text; only checked platforms are allowed; with accounts on it uses ONLY the person's own SocioBird connection (never the server's key, no shared fallback: that once exposed the operator's channels to every account). AI-generated media needs the person's confirmation.
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
  made by the server, never uploaded by a worker. Any failure falls back to a local render.
  A finished clip goes worker -> bucket (presigned multipart, so the proxy's ~100 MB cap does not apply) -> server disk: the object is the task's own `worker/<task id>.mp4` (allow-listed in
  `storage.REL`), the part URLs sign their length, one plan per leased render task, the finished size must equal the announced one, the object is deleted after the copy and on every failure
  (`discard_direct`), and the copy is probed by `validate_clip` like any worker clip. The worker token is never sent to the bucket host. No bucket / 404 = the old PUT through the server. Name patterns use `fullmatch` (a `$` regex lets
  a trailing newline through). `Transport` methods raise `NotImplementedError`, so a test double without `upload` fails loudly.
- Transcript edits (`GET/PUT /api/jobs/{id}/transcript`, `editor.apply_transcript`, `runner.edit_transcript`) are optional and only touch WORDS: a sentence keeps its time span (same word count = every
  word keeps its own time, else `textfix._retime`), cut points never move, only clips overlapping an edited sentence are re-rendered (`_gen` as for any edit), a stale page is refused by id AND start time,
  each edit is one line of <= 600 characters (never emptied), and the clip gets the note "edited by a person: transcript". The sentences shown are the planner's own (`parse_transcript` on the EDL's words), the
  "AI corrected" original comes from `transcript.raw.json` and is shown only when no cleanup changed the timeline. `edl.original.json` is saved first, so a clip reset keeps the corrected words.
- Clip edits are `service.editor.ClipEdit`, a closed set of fields applied by the pure `apply_edit`; never accept a raw
  EDL or a path from the browser. The AI's `decided` list is not rewritten by a person: add a note instead.
  All edl.json writes go through the runner's edit lock; a render finishes only the latest edit (`_gen`).

## Sound library
- `clipcrow/sfx/*.mp3` and `manifest.json` are made by `scripts/make_sfx.py` (ElevenLabs sound-effects API, key from `ELEVENLABS_API_KEY`, never printed). Existing files are kept: each
  generation costs credits and sounds different. Every sound has a provenance row (prompt, date, tool, sha256, length, peak). Commercial use needs a PAID ElevenLabs plan on the day it was
  made: keep the invoice. Never add a third-party sound (Freesound unless CC0, YouTube audio library, Epidemic/Artlist, any music) without a written licence and a manifest row.
- A cue sits UNDER speech: short (0.3-3 s), placed by CODE on structural events (hard cut, punch-in, cover/card/logo entry, number reveal), never by the model, about one per 2-3 s at most, mixed
  in a separate ffmpeg pass (`adelay`, `volume`, `amix=normalize=0`) on the clip's own timeline. A silent generation is a failed one (the first "click" was): `tests/test_sfx.py` checks it.

- Built-in tracks (`clipcrow/tracks/*.mp3` + `manifest.json`, made by `scripts/make_tracks.py` with the ElevenLabs music API, instrumental, 90 s, kept when they exist): same licence rule as the cues (a PAID ElevenLabs plan on
  the day, keep the invoice) and a provenance row each; `tests/test_music_brand.py` checks the hash, length and that a track is audible. A brand picks one with the `music_track` setting (validated against the manifest, ids
  `^[a-z0-9-]{1,40}\Z`); the job COPIES the chosen file as `brand/music.<ext>`. A track shorter than the clip loops (an abrupt seam; a crossfaded loop is not built).

## Deploying
- Push to `main`; deploy only after CI and the Image workflow are green (a commit that changes only tests has no Image run: the previous image has the same code). Never restart while a job is
  `running` or `queued` (a drain requeues it, but wait anyway). Coolify: `POST $COOLIFY_URL/api/v1/services/<uuid>/restart?latest=true` (it is POST now; `COOLIFY_URL` has no scheme).
  Sign-in is by emailed code only (no shared token): check for running jobs with `GET /api/health` (`busy`: true while a job runs or waits). A restart WITHOUT `?latest=true` restarts the OLD image. Confirm the new code is live by a route that only the new code has (never say "deployed" before).
- The GPU worker runs the `:latest` image on the operator's PC: a new worker feature needs `docker pull` there; deploy the SERVER first (an older server rejects new poll fields).

## CI
A green pipeline closes a task, not the push: after pushing, confirm the commit's workflow is green.

## Several people on screen
- Who is shown is decided by the VOICE (`voices.py` turns -> `seats.py` voice-to-seat map -> `reframe.plan_voices`), never by mouth movement alone; the mouth-movement path in `reframe.py` is the fallback.
- `speakers.json` belongs to the video it was made from (size + mtime signature); a re-cut or a new working video rebuilds it. A person's corrections live in its `overrides` and always win.
- Anything a model downloads is pinned (`voices.MODEL_REVISION` once checked) and its failure must degrade to the old behaviour, never fail the job. The YuNet model is MIT (OpenCV zoo): keep its licence note.
- Tests never download a model: `CLIPCROW_SPEAKERS=0` in `tests/conftest.py`; the speaker tests pass a tone-pitch embedder and a fixed-faces detector.

## Viewing as another account
Superadmin "view as" is read-only and logged: never add a route that lets a viewing session change data, spend minutes, read or use another person's SocioBird/credentials, or hide its own start and stop from `admin_audit`.
New write routes are safe by default (every non-GET is refused while viewing); a new GET that reaches a person's secrets or their social accounts must be added to `VIEW_ONLY_BLOCKED` in `api.py`.

## Superadmin actions
A new privileged route: use `superadmin_only` (money, settings, data) or `admin_only` (look, disable), call `audit(request, admin, action, target, note)` BEFORE doing the thing, and add its wording to `AUDIT_WORDS` in `app.js`.
Never add a way to create a superadmin from the UI; `CLIPCROW_ADMINS` is the only source.
