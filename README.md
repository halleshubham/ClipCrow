# ClipCrow

EDL in, short video out. A planner (an LLM or a person) writes an **Edit Decision List**; clipcrow
compiles it into a [HyperFrames](https://github.com/heygen-com/hyperframes) project (HTML rendered
deterministically by headless Chrome + FFmpeg) and renders the MP4.

```
EDL JSON ──► validate ──► compile ──► hyperframes check ──► hyperframes render ──► MP4
 (pydantic)    (edl.py)   (compile.py)    (render.py)          (render.py)
                              │
                              ├─ footage cut at render size, keyframe every second (ffmpeg)
                              ├─ person matte → alpha WebM            (matte.py, optional)
                              ├─ captions grouped from word timings    (captions.py)
                              └─ fonts shipped with the project        (fonts.py)
```

The planner never touches pixels. Everything visible is a function of the EDL, so a render is
reproducible (same browser binary → byte-identical video, verified) and an edit is a JSON patch.

## What works today
- Face-head reels: footage + animated word-level captions + lower thirds + stat pills.
- **Text behind the speaker** (footage → text → alpha cutout of the person → captions).
- **Devanagari** (Marathi, Hindi) captions, correctly shaped, with the font shipped per project.
- Sentence breaks on danda/double-danda, pause-aware caption grouping.
- **Default font: Noto Serif Devanagari** (bold). It has no Latin letters, so Latin words ("WhatsApp", the x in
  "10x") get a **Noto Serif** companion shipped with the project and named in the font stack; the companion is only
  added when the text needs it. Needs `fonts-noto-core` or `CLIPCROW_FONT_DIR`.
- Hostile-input safe: EDL values are restricted to safe shapes and HTML-escaped; media must sit
  under `--media-root`; remote URLs are refused.

## Web service
```bash
pip install -e '.[service]'
clipcrow serve --demo                 # try it: built-in Marathi transcript + recorded model answers, no key, no Whisper
CLIPCROW_TOKEN=... clipcrow serve     # real: needs '.[asr,llm,vision]', ANTHROPIC_API_KEY, Node 22 + FFmpeg + Chrome
```
Open http://127.0.0.1:8000. **New job** (upload, language, clip count/length, spread, speaker card, text behind the
speaker) → the **job page** (four stages, live log, queue position, cancel) → **clips as they finish**: a timeline of
where they come from (with any clip left out for being too close), playable 9:16 cards, the AI's score and what it
decided, Download MP4, and an **AI usage and disclosure** panel (calls, tokens, estimated cost, per-asset AI
involvement).
- **Single worker**: jobs run one at a time in a background thread; state is on disk (`--data`), so a restart keeps
  history, requeues waiting jobs and marks interrupted ones failed.
- **Security (v1: one trusted user or a small team)**: binds to 127.0.0.1; it refuses another address without
  `CLIPCROW_TOKEN` (login cookie: HttpOnly, SameSite=Strict) unless you pass `--insecure`. Ids in URLs are strict hex and
  resolved inside the job's directory; uploads are size-capped, extension-checked and probed; errors shown to the
  browser never contain server internals; strict CSP, no third-party requests, and the UI inserts every job value as
  text (a test guards that). Put it behind TLS if it leaves localhost.
- **Clip editor** (the **Edit** button on a finished clip): title, hook, speaker card, a stat, captions (words per
  line, font, colours, height), cut points (type seconds or click words in the transcript), framing (keep / follow the
  faces again / centre crop / blurred fit; for several people cut, stack or one face), slow push-ins. **Save and
  re-render** re-renders only that clip, in the background, while the old video stays playable; **Reset** goes back to
  the AI's plan. An edit is a closed set of fields (never a raw EDL), the AI's own decisions stay in the record and a
  person's change is added as a note ("edited by a person: title"). Saving again mid-render renders the latest plan.
- **Face-to-camera**: *Text behind the speaker* (person matte; the matte is cached per clip window so a text edit
  re-renders in seconds, and if the matte cannot be made the clip still renders without that effect and says so) and
  *slow push-ins* (the picture eases in as a sentence begins, then snaps back; timing is code, and the footage, the
  text behind and the cutout scale together).
- **Explainer mode** (`--explainer --link URL ... [--ai-images]`, or the checkbox): the model picks, per clip, up to three
  sentences that deserve a picture and what to show: **a page you linked** (by number from your list, so it cannot
  invent an address; captured as a screenshot) or, if you allow it, **an AI-generated image**. When and for how long is
  code (a picture starts at its sentence, away from the hook, with room between). Pictures are made *before* rendering
  (the render browser never touches the network); one that fails is dropped with a note, never a failed job. Links are
  checked on submit and again for every request the page makes (public http/https only, standard ports, no credentials,
  no private/loopback/link-local/CGNAT addresses; known limit: DNS rebinding, so also block private ranges at the
  network edge). A generated image is a `generated` asset, so the usage record turns the **AI-disclosure recommendation**
  on by itself, and removing it in the editor turns it off. Screenshots need `pip install 'clipcrow[assets]'` and
  `playwright install chromium`; images use `GEMINI_API_KEY` (`CLIPCROW_IMAGE_MODEL` to change the model; written to
  Google's documented image output but **not tried against the live API from the build environment**). AI *video* has an
  interface (`clipcrow.assets.video`) and **no provider**: nothing asks for generated video until one is registered.
- **Not built yet**: accounts, retention/cleanup, a job queue across several machines, posting to social platforms.
- Browser test: `node scripts/e2e_ui.js <video> <dir>` (Playwright, real render and re-render, ~8 min;
  `JOB_ID=<id>` re-tests the editor on a job that already exists).

## From a video (`make`)
```
clipcrow make talk.mp4 --out out/ --clips 5 --speaker-name "शुभम"     # needs ANTHROPIC_API_KEY + a Whisper model
clipcrow make talk.mp4 --out out/ --edl edl.json                       # skip recognition and planning
clipcrow make talk.mp4 --out out/ --transcript transcript.json         # skip recognition only
```
```
video ─► transcribe (faster-whisper, Indic-aware) ─► plan (LLM) ─► frame (face-tracked crop) ─► render ─► out/cN.mp4
                                                                                                         out/cN.mp4.ai.json
```
- **Speech recognition** (`clipcrow transcribe`): faster-whisper with a language vote over three windows,
  a Devanagari seed prompt for Marathi/Hindi/Nepali/Sanskrit, fragment merging and repetition-loop removal.
  `CLIPCROW_INDIC_MODEL=large-v3` uses a larger model for Indic audio only. Not yet run on real speech
  (see *Status*).
- **Reframing** (`clipcrow/reframe.py`): faces from OpenCV's bundled Haar cascade, chained into one
  continuous track (stray false positives lose), then a "heavy tripod" camera: a dead band plus a speed limit,
  so head movement never whips the frame. No face in the clip → the whole frame fitted over a blurred copy.
  **Several people** (`--multi switch|split|off`, default `switch`): each persistent face is tracked, the
  talker is whoever's mouth region moves (normalised per person, with hysteresis so the picture does not
  flicker) and the picture *cuts* to them; `split` stacks two people instead, with the captions on the seam;
  two people close enough to share a crop window are framed together.
  Limits: the speaker is chosen by mouth movement, not by voice (no diarisation model), so a laugh can
  steal a turn for a moment; at most two people on screen; Haar needs frontal faces. Tested on synthetic
  talkers and pixels, not yet on real interview footage.
- **AI-usage record**: the EDL carries every planner call (model, tokens, estimated cost), what the AI decided
  per clip, and per-asset provenance (`none` / `processed` / `generated`, enforced so an AI image cannot be
  labelled "no AI"). `clipcrow usage edl.json` prints it; each render writes `cN.mp4.ai.json` including
  whether a platform AI-content disclosure is recommended (it turns on as soon as an asset is AI-generated).

## Planner (transcript → EDL)
```
clipcrow plan transcript.json --source podcast.mp4 --media-root media/ --out edl.json \
         --clips 5 --cutout --speaker-name "शुभम" --report plan-report.json
```
```
sentences ─► windows (≥90 s, overlapping) ─► [LLM] score hook / completeness / standalone / emotion
          ─► top windows ─► [LLM] pick clips + hook, title, emphasis words, stat, text-behind word
          ─► deterministic finalize: sentence-aligned cuts, 15-60 s bounds, padding, overlap removal
          ─► EDL (pydantic-validated)
```
- **The model only chooses sentence ids and short strings.** Cut points, lengths, overlay timing and
  overlap removal are code, so a bad answer can only drop a clip, never produce a broken video.
- **Spread**: clips are spread across the talk, not stacked on the single best section. A clip must be at least
  `spread × (talk length ÷ clips)` seconds from every clip already chosen (`--spread`, default 0.5; 0 = plain
  best-first); this is relaxed only when there are not enough distant candidates, and overlaps are never allowed.
  The same rule keeps the windows the model details from clustering. A clip left out for being too close is
  listed in the report.
- **Language**: Marathi/Hindi copy must be Devanagari. A romanised answer triggers one repair call;
  if it is still wrong the on-screen hook is dropped (the clip keeps its title and is flagged).
- **Untrusted transcript**: it is fenced as data, the system prompt says so, and every output is
  length-capped, control-character-stripped, validated and HTML-escaped later.
- **Models**: `$CLIPCROW_MODEL` or `claude-opus-5-5`; `--score-model` takes a cheaper one for the scoring pass.
  A refusal or truncation skips one window instead of failing the plan.
- Tested with a scripted model; **not yet run against the live API** (needs `ANTHROPIC_API_KEY`).
- `examples/plan_demo.py --render` runs planner → EDL → MP4 end to end with a scripted model.

## Use
```bash
pip install -e '.[dev,llm,asr,vision,examples,service]'
./scripts/fetch_gsap.sh                      # GSAP is fetched, never committed (see Licences)
python examples/gen_assets.py                # synthetic demo footage + cutout
clipcrow validate examples/demo.edl.json
clipcrow render examples/demo.edl.json --clip c1 --out build/c1.mp4 \
         --media-root examples/assets --gsap vendor/gsap.min.js
pytest                                       # unit tests
CLIPCROW_RENDER_TESTS=1 pytest -m render     # + a real render (needs Node 22, FFmpeg, Chrome)
```
Fonts: install `fonts-noto-core` or set `CLIPCROW_FONT_DIR`. The CLI version is pinned
(`render.HYPERFRAMES_VERSION`); override the command with `CLIPCROW_HF_BIN`, the browser with
`CLIPCROW_BROWSER`.

## Measured (spike, 4 CPU cores, no GPU)
12 s of 1080×1920 @30 fps renders in ~21 s (≈1.8 s per output second). Person matte on CPU is
~2 fps (a 60 s reel ≈ 14 min), so production needs a GPU (`device="cuda"`) or a lower matte rate.

## Roadmap
1. ~~Planner~~, ~~ASR~~, ~~reframing~~: built (see above). Still to do: run them on real Marathi/Hindi
   footage (see *Status*), a better face detector (YuNet) behind `FaceDetector`, and voice-based speaker
   identification to complement the mouth-movement signal.
2. **Service**: FastAPI + job queue + object storage; render workers (GPU for matte/encode).
3. **UI**: v1 built (new job, job page with live progress, clip review, AI usage) and the form editor (see above); a live preview is next (see below).
4. **Explainers**: built for news-link screenshots and AI images (see above). Still to do: HTML charts, an AI-video provider, stock footage.

### UI
Yes, it is in the plan. Order of build:
- **v1 job dashboard**: upload or paste a link, choose clip count and look, watch progress, review
  clips, download. No editing.
- **v1.5 form editor** (built): per clip, edit text, captions, framing and cut points. Each change patches the EDL and
  re-renders that clip, shown as video. No timeline. (A draft-quality quick render is still to do.)
- **v2 live preview**: load the compiled project in an iframe and drive its seekable timelines,
  for instant scrubbing. Only then consider a timeline editor.
Caveat: GSAP's licence forbids use in no-code visual animation tools that compete with Webflow. An
end-user visual animation editor could fall under that, so keep the editor to EDL fields (not
free-form animation), or get the licence position confirmed before building v2.

## Status: what has and has not been verified
- **Verified**: EDL validation, compile, real HyperFrames render (CI), Devanagari captions, planner logic with
  a scripted model, usage/provenance, face-tracked reframing on a real (public-domain) face photo panning
  across a frame, and the full chain video → reframe → render.
- **Not verified**: speech recognition with a real Whisper model on real Marathi/Hindi speech (the build
  environment could not download models); the planner against the live Claude API; person matte quality;
  GPU speed; multi-speaker footage; the matte on real footage (only a synthetic matte is tested: the text really
  sits behind it and the push scales both, checked on rendered pixels).

## Licences to keep in mind
- HyperFrames: Apache-2.0. Pin the version; Chrome build changes pixels.
- GSAP: free for commercial use under GreenSock's own licence, with the Webflow-competitor
  restriction above. Fetched from npm at build time (`scripts/fetch_gsap.sh`), not redistributed here.
- Noto fonts: SIL OFL. Parakeet (if used for non-Indic ASR): CC-BY-4.0, attribution required.
- Code taken from the OpenShorts fork keeps its MIT notice (MIT is compatible with AGPL). Do **not**
  copy its `cloud/` directory: it is under a separate commercial licence that is not compatible.
- `ultralytics` (AGPL-3.0) would be licence-compatible with this project, but it still carries its own
  terms; check them before adding it.

## Licence
GNU AGPL-3.0, see [LICENSE](LICENSE). In short: anyone who runs a modified version as a network
service must offer their users its source. As the copyright holder you can still run, license and
dual-license your own code; outside contributions would need a contributor agreement for that.
