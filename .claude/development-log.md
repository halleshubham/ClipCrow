# Development log

What was built, what real recordings taught us, and what is still open. Newest first. Add an entry (date, what, why, what to watch) whenever behaviour changes.
The rules that came out of these are in `development-rules.md`; this file is the story behind them.

## 2026-11-15
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

## Open
- Caption legibility over a white shirt (contrast 1.25-1.64:1); landscape outro letterboxed with black bars; ~2.8 s frozen/black seam before the outro; one clip at -22 LUFS vs another at -14.6.
- Per-stage timings in the job log; an audio-similarity repeat detector; the SraVaani/Whisper choice in the job log; SraVaani in the container image.
- The live effect of the read-first judge on a new real recording (offline replays only so far).
- Sound cues are not yet placed or mixed into renders.
