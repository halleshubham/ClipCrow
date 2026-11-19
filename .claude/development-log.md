# Development log

What was built, what real recordings taught us, and what is still open. Newest first. Add an entry (date, what, why, what to watch) whenever behaviour changes.
The rules that came out of these are in `development-rules.md`; this file is the story behind them.

## 2026-11-17 (job ba56794375f8: Marathi labelled Hindi)
- A 53-minute Marathi podcast was labelled Hindi (`vote_language`: Whisper's sound-only guess on 3 x 30 s windows, majority wins). The cost was visible: the planner wrote every title and hook in HINDI
  (`language_instruction("hi")`), while the captions (the speaker's words) stayed Marathi. The words themselves were unmistakable (852 Marathi-only function words against ~0 Hindi-only ones).
- Fix, two layers: `indic.settle_hi_mr` decodes the vote windows in the chosen language and lets `language_from_text` (Marathi-only vs Hindi-only function words, >= 8 of one and >= 3x the other) overrule the
  label BEFORE the long decode, and `make()` relabels after any transcription (a worker's, SraVaani's, a reused one) with the same rule. A language the person picked is never second-guessed.
  Workaround for older jobs: pick Marathi in "Spoken language" instead of Auto-detect.
- With `CLIPCROW_ASR=sravaani` Hindi and Marathi both go to SraVaani (it reads either), Whisper's vote only picks the engine and SraVaani's WORDS name the language (the Whisper-decode check is for the
  Whisper path only). A vote of another language (Nepali, Sanskrit) still goes to Whisper.

## 2026-11-17 (interface audit: icons, platform logos, short copy)
- Audited every page (new job, jobs, job, clip editor, share, styles, brand) at 360 px and desktop in both themes. Findings: text-only buttons and navigation, platform names as plain words on the share page, a long
  grey paragraph under almost every field and checkbox, an empty "Posts" card, unlabeled technical units on the brand page.
- Done: icon sheet (64 icons, `icons.css`), icons beside nav items/buttons/summaries/headings (from their words), platform logos with their colours on the share page and the posts list, long hints and checkbox
  explanations folded into "i" popovers (hover/focus/tap, Escape, hoverable), shorter file-limit labels on the brand page, an empty state for posts, a film icon in the jobs list. Rules in `docs/ui.md`.
- Not done: Styles page copy was not shortened beyond what `decorate()` does; the clip editor's long tooltips-by-title (`title=`) were left alone.

## 2026-11-17 (progress while telling people apart)
- The three slow steps of the speakers search now log a line at each quarter ("looking for the people on screen: 20 of 53 minutes", "listening for who speaks: 450 of 900 stretches", "matching voices to people: ..."), so
  a long video is no longer silent for many minutes (job ba56794375f8, a 53-minute video, showed one line for a long time).

## 2026-11-17 (the speakers task on the GPU worker)
- Built on the owner's request although the measurement said ~2 min saved: `speakers` task kind, `SpeakersIn` validation, `RemoteSpeakers` with server fallback, `worker.handle_speakers`, a GPU-capable
  `WavLMEmbedder(device="auto")`, `seats.analyse` split from caching. Workers need the new image AND a server that knows the kind (deploy the server first). Details and the CPU-torch limit: `docs/plan-speakers-on-worker.md`.

## 2026-11-17 (reel length)
- **Shortest / longest reel (seconds) replace the length dropdown** on the job form (15 and 60 prefilled). One end given: the other follows (`api.derive_lengths`: min only -> max = max(2 x min, min + 30);
  max only -> min = max/3). **Both empty: the whole video is one reel** (up to 15 minutes), in the UI and for API callers that send no length and no style (`whole` is implied). A style still sets the length.
  Cleaning a recording (a max of 90 s or less) empties both fields, as the old dropdown switched to "whole".

## 2026-11-17 (still later: metrics of job 9a4ddd051616, download all, stage times)
- **Download all reels**: `GET /api/jobs/<id>/clips.zip` (stored zip of the ready clips, named like the single downloads; bucket-only clips are fetched for it) and a button over the clips of a finished job.
- **Stage times**: `Stage.started/seconds`, shown on the job page ("took 2 min 10 s"). The log had no timings, so the frame stage of the first real several-people job (6 seats found for 4 people,
  3 voices, 10 clips) could not be read back; jobs from now on record it.
- **Seats**: tracks of one head found twice are merged (`seats.merge_seats`); the job showed 6 seats for 4 people (1 and 2, 5 and 6 on top of each other).
- Worker plan measured, not built (see `docs/plan-speakers-on-worker.md`).

## 2026-11-17 (later: three improvement ideas)
- **One person is detected by itself.** `seats.count_faces` looks at 16 frames (a few quick seeks); fewer than ~3 frames with two or more faces = one person: no seat search, no voices, and the empty result is
  cached in `speakers.json`. The form's "Follow one face" is now "One person on screen (skip the people check)" (`multi=off`, the explicit choice).
- **The same file twice reuses its transcript.** The runner fingerprints every upload (`Job.source_sha`, SHA-256); every job saves the recogniser's own output as `out/asr.json` (words, language, `engine`, date);
  `service/asr_cache.CachedTranscriber` wraps the transcribe function: a job of the SAME account with the same bytes and the same language setting starts from it (the log says when and by which recogniser);
  everything after (text correction, cleanup, planning, framing, render) runs as before. "Listen to the recording again" (`Params.fresh_transcript`) switches it off. Workers now report their `engine`
  (optional field; older workers show as "worker"). Not reused: other accounts' files, a different language setting, a damaged file, jobs made before this.
- **Planned, not built:** the speakers step on the GPU worker (`docs/plan-speakers-on-worker.md`); build it only if the measured stage takes more than ~4 minutes.

## 2026-11-17 (job 1f01b69cd70b: four people on screen, one or two talk, the reel showed the wrong one)
- **The voice decides who is shown.** `voices.py` finds who speaks when (WavLM speaker embeddings via `transformers`, 1.5 s windows, average-linkage clustering, turns snapped to pauses); `seats.py`
  finds the fixed seats (YuNet face detector, vendored ONNX in `clipcrow/models/`, MIT; falls back to the Haar cascade) and matches each voice to the seat whose mouth is busier while it is heard
  (lift >= 1.15 on a few watched turns per voice, one voice per seat, brute-force assignment). A voice with no seat is off screen. Saved as `out/speakers.json` (+ `speakers.jpg`, not mirrored to R2).
  `reframe.plan_voices` cuts to the speaker's seat at each change of voice (hard cut), ignores turns under 1 s, groups seats that fit one window, holds the last speaker through a short off-screen voice
  and shows the whole frame through one longer than 4 s. The old mouth-movement guess is only the fallback (no map, one voice, graphics mode, `multi=off`).
- **Cost/limits.** Only videos with >= 2 seats pay for voices (a few seconds of face finding otherwise); a 20-minute video takes a few minutes on the server CPU (face finding at <= 600 frames,
  ~0.2 s per voice window, a few hundred mouth frames). It assumes a camera that stays put. `CLIPCROW_SPEAKERS=0` switches voices off (tests set it). A model that will not load never fails a job.
- **Person in the loop.** Job page card "Who is who on screen" (picture with numbered seats, a sample of each voice, a menu per voice) -> `PUT /api/jobs/<id>/speakers` -> the clips that followed voices
  are framed again. A job made before this can run "Find who is speaking" (`POST .../speakers/build`, background). Clip editor: "Which person" (stay on person N, or follow the voice).
- **Not verified on the real source** (it cannot be fetched): checked on made-up videos with known speakers, and the real YuNet + WavLM components on a wide copy of a real reel (2 seats, 2 voices).

## 2026-11-16 (market research on retake removal: Descript, Gling, Tella, FireCut, Smart-Cut)
- **Reviewable cuts.** `GET /api/jobs/<id>/cleanup` lists every applied cut AND the AI's unapplied suggestions with a number; the job page has a switch for each and "Cut again with my choices"
  (`POST .../cleanup/review {applied: [numbers]}`). The choices are saved as `out/cleanup.review.json`; the job runs again from the saved words (`transcript.original.json`) and the ORIGINAL upload with
  exactly those cuts (`clean_video(cuts=...)`, nothing is detected again, the 70% cap does not apply), the clips are planned and rendered again (clip/word edits are lost; the minutes are not charged again,
  and a failed re-cut does not refund them).
- **Read-through acts.** `Check.suggest` (lines it suspects but will not cut) become `kind="suggested"`, `apply=False` cuts the person can switch on; the verify prompt now says that a fragment the next line
  restarts goes in `drop_more` (job 1717e2deda8a: it named the fragment and left it).
- **Best take** (`Params.clean_take = "best"`, `--clean-take best`, job form): the judge may swap, i.e. list a worse LATER take in `drop` and the earlier take it replaces in `keep` (one `keep` per `drop`,
  only retake/restart/false_start rule cuts come back). Default stays "last".
- **Restart after a short pause** (>= 0.45 s) when 5 of the 6 opening words come back (0.9 s needed 4 before); a kept aside is at most 6 words (`INSERT_MAX`). **Fades at every join 8 -> 20 ms.**
- **The deploy of fea5f37 never happened**: the restart lacked `?latest=true`, so Coolify restarted the OLD image (job 1717e2deda8a ran on old code). Always add `?latest=true` and confirm with a route only the new
  code has (`can_review` in `GET /api/jobs/<id>/cleanup`). Also fixed: `RealPipeline.run` used `Interruptible` without importing it (NameError on every real job since d07c816; the import sat in `reference_look`).

## 2026-11-15
- **A pause before the ending video**: the clip fades out, 400 ms of black and silence, the ending fades in (brand setting, 0 = straight cut). Asked for because the end looked abrupt.
- **First real direct clip upload (job 55350aaed187)**: the worker rendered a 37 s clip from a 4K source and began sending 49 MB to R2; the upload then sat with no output. A part could wait 900 s without data, 3 times,
  and printed nothing. Now: a quiet connection fails after 60 s, each part is logged, and after 3 failed tries a clip under 90 MB goes through the server instead (above that the failure is reported and the server renders).
  Tip for a stuck worker: the finished file is in the container at `/models/home/.cache/clipcrow-worker/<job>/out/<clip>.<task8>.mp4` until the worker stops: `docker cp` it out first.
- **SraVaani on the server** (CPU image now has torch CPU, transformers 5.23.0, sentencepiece, soundfile; Whisper stays the fallback). The server's job log had said "SraVaani unavailable (ModuleNotFoundError): using
  Whisper": `CLIPCROW_ASR=sravaani` was already set in Coolify but the image lacked the packages. Needs `HF_TOKEN` (a fresh token of an account with access) in Coolify for the model download.
- **A cancel that did not cancel** (job 47107ba0b5f7): a running job only noticed a cancel between steps, and one AI call or one recognition window can take minutes. Now every AI call is interruptible, recognition
  checks per window, `Cancelled` can no longer be swallowed, and the flag is saved (a deploy must not restart a cancelled job: it would have re-run that one).

## 2026-11-14
- **Adoption of the OpenReel ideas, step 1: loudness.** Every finished reel is brought to -14 LUFS (`loudness.py`). Why: one real clip was -22 LUFS and another -14.6, so viewers would reach for the volume.
  Not yet verified on a real render (tests use synthetic clips).
- **Adoption step 2: sound cues** (`cues.py`): quiet pop/whoosh/shutter/ding/click under the speech at overlays and push-ins, off by default, one checkbox on the job form and one per clip in the
  editor. Tested with synthetic audio only: a person has to listen to a real reel before it is switched on for anyone (levels, density, whether a click on every push-in is wanted). Not done: a music bed
  with ducking (licence risk), cues on hard cuts (the cleaned recording's seams are not in the EDL).
- **Adoption step 3: caption motion presets** (pop, fade, slide, karaoke) chosen per clip in the editor; default unchanged. Rendered once for real (demo clip with karaoke + cues + loudness): the active
  word was yellow, loudness -13.99 LUFS. Fonts for a local real render: `scripts/install_fonts.sh <dir>` then `CLIPCROW_FONT_DIR=<dir>` (the sandbox has none).
- **Adoption step 4: transitions, music and ducking.** Transitions (flash, blur, dip, whip) on hard cuts, rendered for real and looked at in frames. The cleanup's join times now travel in the EDL (`Clip.seams`), so
  cues and transitions can use the jump cuts. Music: the account uploads its own track (Brand page), the render ducks it under the speech with a sidechain compressor; ducking tested with real audio (bed 6+ dB
  lower under a tone than in the pause). One real render with transition + karaoke + cues + music: no warnings, -14.0 LUFS. All of it is OFF by default and unlistened-to on a real Marathi reel: judge by ear.
- **Built-in background tracks**: two instrumental 90 s tracks generated with the ElevenLabs music API ("Calm keys", "Bright pulse", `scripts/make_tracks.py`), selectable on the Brand page next to the upload.
  Not listened to by me (no ears): check them before offering them to anyone. A clip longer than 90 s loops the track with a hard seam.
- **Platform length/size checks: only YouTube Shorts (3 min, vertical) is noted on the Share page.** Searches for Instagram, TikTok, Facebook, X and LinkedIn limits returned third-party guides that
  disagree widely (Instagram 3-20 min, TikTok 10-60 min, Facebook 90 s to no cap) and no official page, so a check there would be guesswork. Add one only from the platform's own help page, with its date. Next on the list: ducking/SFX placed from the EDL's word times, platform length/size checks before posting, a closed set of transitions,
  caption animation presets.
- **Optional transcript check/edit** on the job page ("Check or fix the words"): sentences with times, the clips that use them, an "AI corrected" mark with what was first heard; saving re-renders only
  the affected clips. Why: Marathi recognition is still imperfect and captions show the words. A pre-cut review step (pause after recognition, before cleanup and planning) was NOT built: it needs a new job
  state, which touches minutes, drains and retention (see the rules).
- **OpenReel Video audit** (MIT, TypeScript, browser/desktop manual editor, WebCodecs export, in-browser Whisper): not comparable (manual NLE vs batch generator). Ideas worth taking, none of its code:
  two-pass `loudnorm` to -14 LUFS after render, ducking driven by the EDL's word times, a closed set of transitions compiled to GSAP, platform export presets with duration/size checks, a caption-animation
  preset enum, an undo snapshot per AI turn. Its loudness measure is approximate (not a model to copy); its export is not deterministic.
- **Pictures are the person's own.** The web service no longer opens web pages for an explainer (no capture, no SSRF surface there). A person adds screenshots (`POST /api/pictures`), the
  model is SHOWN them and places each by number. Why: server-side capture was slow, fragile and a security surface; a person knows which page matters. CLI `--link` still captures.
- **Sound library.** 8 short cues generated once with ElevenLabs (`scripts/make_sfx.py`, `clipcrow/sfx/`). Not wired into the render yet. First `click` came back silent (peak -49 dB): regenerated
  with a clearer prompt; the test now rejects silent sounds. Research brief (vendor pages only, no retention data): add keyword emphasis, sparse cut/zoom ticks, whooshes on cover/card/logo, a number
  reveal, then a ducked music bed (licence risk), a progress bar, zoom variety. Skip glitch, shake, grain, speed ramps.
- **New logo** everywhere (header tile, sign-in, favicons, README) from `docs/assets/logo-source.webp` by `scripts/make_logo_assets.py`. The art is dark, so it sits on a light tile in the dark UI.
- **GPU worker sends the clip straight to the bucket** (presigned multipart): clips of any length render on the GPU. Deployed (server first, then `docker pull` on the PC). Untested against real R2 until
  the first long clip; the fallback is a server render.
- **SraVaani** (ARTPARK-IISc, MIT, CPU) as an opt-in recogniser for Marathi/Hindi (`CLIPCROW_ASR=sravaani`, `asr_sravaani.py`): wrote every word incl. repeats on the 84 s test, word starts within
  0.1-0.3 s of ElevenLabs Scribe. Needs `torch transformers sentencepiece soundfile` and a Hugging Face token with access (gated, pinned revision). Whisper stays the fallback.
- ASR bake-off on the 84 s Marathi recording: Whisper lost ~21 s as "silence"; ElevenLabs Scribe, Sarvam (saaras:v3, 30 s REST limit) and SraVaani all kept the whole recording and the three repeats.
  Scribe: tight word times but Latin spellings of Marathi words. Sarvam: best spelling, no word times. Keys pasted in chat must be rotated.

## Earlier (before 2026-11-14)
- **Cleanup that reads first.** `Review` (intent, passages, drop) sees the WHOLE recording with the rules' cuts marked, then `verify_cuts` reads the result like a viewer. Fixed after job audits:
  speech Whisper returned no words for was deleted as dead air; one word stretched over seconds hid repeats; retakes of whole passages, reworded, were not caught; text correction smoothed away the
  stumbles the cleanup needs; the 45% removal cap refused real recordings (now 70%).
- **Long reels / whole video as one reel**, `edl.MAX_CLIP_SECONDS` 15 min; trim limit, render timeout and worker limit follow the clip's real length.
- **Direct browser -> R2 uploads** (multipart) removed the source-size cap except `--max-upload-mb`; validated live (3 GB accepted, 5 GB refused).
- **Marathi/Hindi recognition**: windowed 15 s decode, no prompt/VAD, `fill_holes`, `textfix.refine`, `clipcrow compare-asr`.
- **Slides/graphics option**, **fumble-free cut option** (adapted from the `fumble-free-cut` skill), **styles from a reference video**, **brand kit**, **social posting** (SocioBird: base URL
  `https://go.sociobird.app/api`, needs a User-Agent; Cloudflare turns an origin 502/504 into its own page, so the app uses 424 for upstream failures), **accounts and billing**, **object storage**.

## 2026-11-15 (audit of job 55350aaed187)
- **Restated phrase with a clause between the tries** ("X, many farmers said that, X is meagre"): the false-start rule used to cut the first X AND the clause (the quote's attribution was lost). Now only the repeat
  is cut when the words between (>= 2) were said in one breath (no filler, no pause >= 0.45 s inside or before the restart); a pause still marks an abandoned try and cuts it all.
- **Cuts before a removed word** stop `START_LAG` (0.12 s) early (recogniser starts trail the sound by ~0.1 s, a plosive burst leaked at the seam); quiet-point search window 60 -> 100 ms.
- **Run-on speech** (no punctuation, 40-word cap) is split at its longest pause (>= 12 words in), not mid-phrase. **Word ends** keep >= 0.12 s (`MIN_WORD`) so caption highlights never flash for 50 ms.
- **Digits are always Latin** (`edl.latin_digits`, applied by every EDL model and by the transcript loader): 33%, 1998, 1,00,300/-. The planner prompt is unchanged (the recorded replay is keyed on it).
- **Restart with changed endings** (job 1717e2deda8a: "कारण काय फार मांसाच्या | कारण काय भारी मांसाचं", the same opening, two words re-said with another ending, 0.35 s apart): rule 3 needs `n-1`
  same words and the changed word differed too much (`same_word` 0.85). It now also counts `kin_word` (same stem: >= 4 shared leading letters and 60% of the shorter word). The read-first verify
  noticed the fragment but "left it as is" (a problem it reports is never acted on): see Open.

## 2026-11-17 (several brands per account)
- **Brands**: an account holds up to 20 brands (`BrandStore`: `brand/<owner>/brands.json` + one folder per brand, ids `^[0-9a-f]{12}\Z`; the first use of an old single-brand account migrates its files into a brand called "My brand").
  A brand carries logo, ending video, music, look, the speaker's name/role and the default caption style (colour, highlight, font, animation, words per line, height). Routes take `?brand=<id>` (none = the default brand);
  `/api/brands` lists, creates, renames, sets the default and deletes (the last brand stays). Someone else's brand id is a 404.
- **A job fixes its brand when it is made** (`Params.brand`); changing the default later never moves it. The brand's speaker fills the lower third unless the job names someone; the brand's caption look applies unless the job
  picked a reference style (a style wins). A brand deleted after the job was made falls back to the copies in the job.
- UI: Brand page switcher (+ Speaker and Captions sections); New job has a Brand select (shown with two or more brands) that re-fills the logo/ending/music rows.

- **One or two people skip the people search** (`seats.is_small_group`): only a video where three or more faces are seen together in >= 20% of the 16 quick frames gets seats, voice listening and mouth watching; one or two people are
  framed the older way (`reframe` face/mouth tracking, no `speakers.json`, no who-is-who card). Trade-off: with two people the voice-to-seat matching is no longer used.

- **Run again** (`POST /api/jobs/{id}/retry`, button on the job page): a failed or cancelled job runs again from its saved video with its options; minutes are reserved again (the failed run refunded them).
  A job a restart interrupted twice is marked failed on purpose (so one that takes the server down cannot loop); this is how a person starts it again.

- The New job form now starts on **One person on screen** (`multi=off`, the quickest); "Several people" is a choice. `POST /api/jobs/{id}/retry` also takes `{"multi": ...}` and then runs a FINISHED job again with that option (its clips are replaced, minutes taken again).

- **Faces that are not people** (job ba56794375f8: a statue and a caption graphic were seats; the main voice got no seat and clip 2 cut to the statue): (1) `plausible` keeps a detection only when the detector scores >= 0.8 and the box is
  2.5-40% of the frame wide and 0.6-1.7 tall; (2) every seat carries `live`, the median change of its face picture between samples 1 s apart, and a seat that stays below 1.5 while another is above 3 is dropped ("never move" in the log);
  (3) seats within 3.5% of the frame are one place; (4) `assign` never gives a voice to a seat whose mouth region never changes (p90 < `ACT_FLOOR`) and a seat's own scale is floored at the noise; (5) the quick one-or-two-people check
  runs on the same filters, so a statue no longer makes a third person. Thresholds are first guesses checked on made-up videos only: watch the "never move" line on real recordings. Old `speakers.json` files stay as they were until "Find who is who" is run again.

- **The who-is-who picture is a typical moment** (`_typical_frame`): the middle, in time, of the sampled moments where the most of the final seats have a face, cut from the video at `picture_t` (before: the single frame with the most
  faces, which on a podcast is the intro card). `/api/jobs/{id}/speakers` also returns each seat's `w`, `h`, `seen` and `live` so a map that looks wrong can be explained from the job itself.

- **A worker's seats carry `live`** (`SeatIn.live`): the server's answer model forbids unknown fields, so a worker built after the still-face check would have been rejected ("did not deliver") and the server would have redone the work. Deploy the
  server BEFORE pulling a new worker image whenever the answer's shape grows.

- **Per-shot framing for one person** (`reframe.shot_cuts`, `pick_subject_per_shot`, `smooth_shots`): the sampled frames' tiny thumbnails (always taken now) show camera cuts (change > 25 and > 4x the usual between samples; a blip shorter than 0.5 s cuts
  away and back and is ignored). Each shot picks its own main face and the crop starts on it at the cut, a hard cut with no pan across the room; a shot with no face holds the last position. A clip with no cuts is framed exactly as before.
  Applies wherever the single-subject path runs ("One person on screen", and the one-person fallback of the other modes). Checked on made-up videos only: watch real cut-angle recordings (cut thresholds 25 / 4x are first guesses).

- **The logo keeps off the face** (job ba56794375f8: in a close-up the corner sat on the head): framing records `Layout.face_zone` (where the followed face, or the shown seats, sit in the vertical frame over the clip) and
  `branding.fit_logo` keeps the logo clear of it and of the caption band: the brand's corner when it is clear, else down the left/right side (`Logo.top_px`), else with a narrower side margin (`side_px`), else it keeps its size and fades to 30% opacity (`faded_opacity`; it used to shrink to 40%, which made it invisible).
  Done when the brand is applied (`apply_brand`) and again when a clip is re-framed. Clips made before this have no zone, so they keep the corner until framed again. Brand page: a live preview (sample picture, logo, caption line; `POST /api/brand/preview` asks
  the server for the placement so the preview and a real clip agree).

- **Watch-full-video banner** (`edl.Banner`, `compile._banner_scene`, brand settings `banner_*`, job option `banner`, editor switch): words ("पूर्ण व्हिडीओ पहा:" / "Watch full video:" by language unless the brand wrote them) over a white pill with the platform mark
  (`platform_marks.py`, Simple Icons CC0, 8 platforms) and the channel name; height and "only the last N seconds" are brand settings; the logo keeps clear of its band. Needs a channel name on the brand.
- **Brand page audit** (a subagent read the page at 360 and 1280 px): fields grouped into sections with one sticky Save bar that also holds the messages; uploads start when a file is chosen and save the form first (a re-render used to lose typed values);
  the music track is saved with the rest; empty = "App default" for words per line and caption height; opacity in %; choosing a caption colour ticks "use it"; the cover solid colour shows only for a solid background; 44 px touch targets for info buttons,
  ticks, file and colour inputs. Not done: tabs, inline new/rename/delete instead of prompt()/confirm(), an upload progress bar, the cover title and speaker line in the preview. A live hint beside a New job checkbox is kept (`data-keep`) so `decorate()` does not fold it away.

- **Google Fonts in the Brand page** (`fonts_catalog.json`, `fonts.py`, `scripts/install_fonts.py`): 104 families (53 cover Devanagari: every Devanagari family on Google Fonts that npm carries, plus Poppins/Hind/Mukta; the rest are the best-known Latin ones), installed in the image
  (+about 50 MB) from the @expo-google-fonts packages at pinned versions. A brand has two caption fonts: `caption_font` (Marathi/Hindi/Nepali/Sanskrit clips) and `caption_font_latin` (the rest; else the first one); the preview draws them from `/api/fonts/file`.
  A font missing on the machine that renders (a worker image older than the list) falls back to the default with a note instead of failing the render. To add a family: add it to the catalogue (name, pkg, ver, deva/latin coverage, files) and rebuild the image.

- **Banner scene and HyperFrames' lint** (job cd1ab619104a failed its check): a CSS `transform` on an element GSAP also moves (`y`) is flagged. The banner now starts with `opacity: 0` in CSS only and `tl.fromTo(... y: 24 -> 0)`; a test pins it. Checked with the real `npx hyperframes@0.9.29 check` on a compiled banner scene: no banner finding left.

- **Bulk scheduling** (`service/schedule.py`, `POST /api/jobs/{id}/publish/bulk`, page `#/jobs/<id>/schedule`): pick reels on the job page, channels on the schedule page, "posts per day on each channel" (1-10) and a first day; each reel goes to each channel,
  `per_day` a day, at that platform's best local times (defaults from general advice for short video in India, editable per platform, saved per account with the time zone; working-days-only for LinkedIn; a slot within 10 minutes of now is skipped; two
  channels of one platform are 15 minutes apart). More posts a day than best times split the biggest gaps of the day. A dry run returns the plan; the real call checks everything first, uploads each reel once and posts each pair with its own time; per-post
  status (`scheduled`/`failed`) is kept in the job's `posts.json`. At most 30 reels x 6 channels, 60 days. The best times are NOT measured from anyone's audience. `tzdata` is a dependency (the slim image has no zone files).
- `docs/roadmap.md`: the planning pass for the next months (first ten items, quick wins, what not to build, questions for the product owner).

- **Rented GPU trial** (Race Engineering RTX 3090, see docs/rented-gpu.md and scripts/rented-gpu/): the worker stack (Node, ffmpeg, HyperFrames + Chrome, fonts, GSAP, venv, SraVaani) installs into `/workspace` in about 6 minutes,
  survives pause/resume with `post_resume.sh`, and a production worker ("race-3090") ran against the server. Learned: only the built-in templates deploy via the API; a resume is a new container; the Ubuntu mirror is too slow (use a nearby one for Chrome's libraries);
  Whisper large-v3 on Marathi is poor and SraVaani is right; WavLM merged two speakers into one voice again.

- **GPU cost, capacity and the on-demand pod**: every worker task is timed on the server (`gpu_usage.json`), costed in INR from `CLIPCROW_GPU_RATES` (a `"*"` price also covers the home GPU) and listed in the job's AI usage and disclosure;
  overlapping tasks on one machine share its hourly price. The stronger idle worker (`power`) is offered a task first (a weaker one yields for 4 s); `clipcrow worker --parallel N` adds render-only slots; `--pool home|rented` marks the machine.
  A job picks `gpu`: auto, home or pod (New job page, with a per-GPU cost estimate); `gpu_pod.PodController` wakes a paused pod for a waiting job, boots the worker through Jupyter, pauses after idle, keeps a daily INR cap, never terminates.
  The New job page shows one rough cost (AI planning + the chosen GPU, + margin, `CLIPCROW_MARGIN_PERCENT`, default 50, USD→INR `CLIPCROW_USD_INR`) worked out in the browser from the video length and reel count.
  The numbers are assumptions, to be replaced with measured per-task times from `gpu_usage.json` and real token counts.

- **Banner vs speaker name** (job 8b13efc5b2c1 failed at clip 1 with HyperFrames' "two text blocks overlap"): the "watch the full video" banner and the speaker's lower-third both sit at 1440 px. The banner now starts after the name card has gone; and if the layout check still finds overlapping text, the clip is rendered without the banner and the name card (a note says so) instead of failing the job.

- **Pictures of the person's own** (job 1f8793be3074 audit: a tall screenshot shown whole with black bars, 5 s, one of three used): the model now also gives a `crop` (the part that matters), a `cue` word (where it appears) and `read_words`; code validates them (`clean_crop`, `_cue_time`), holds the picture for reading time (3 s + 1 s per 3 words, 4.5-9 s, until its sentence is said), lets it start in the first or last sentence and follows a picture still up with the next, and warns when a picture was not placed. The render crops the file with ffmpeg and sizes the panel to the picture's shape (no black bars, centred in its zone); the panel's own caption is left out when the spoken captions are on. The thumbnail the model sees is 1568 px (was 1024) so a screenshot's text is readable to it.

## Open
- `verify_cuts` problems are only reported; a fragment it names (an abandoned, restarted line) could be offered as a cut when the rules missed it.
- Caption legibility over a white shirt (contrast 1.25-1.64:1); landscape outro letterboxed with black bars; ~2.8 s frozen/black seam before the outro; one clip at -22 LUFS vs another at -14.6.
- Per-stage timings in the job log; an audio-similarity repeat detector; the SraVaani/Whisper choice in the job log; SraVaani in the container image.
- The live effect of the read-first judge on a new real recording (offline replays only so far).
- Sound cues are not yet placed or mixed into renders.
