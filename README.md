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
- Hostile-input safe: EDL values are restricted to safe shapes and HTML-escaped; media must sit
  under `--media-root`; remote URLs are refused.

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
pip install -e '.[dev]'
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
1. ~~Planner~~ (built, see above). Still to do: bring over the Indic ASR + language voting and
   reframing from the OpenShorts fork (MIT parts only), and validate the planner on real transcripts.
2. **Service**: FastAPI + job queue + object storage; render workers (GPU for matte/encode).
3. **UI** (see below).
4. **Explainers**: per-beat asset router (HTML chart, AI image, news-link screenshot, AI video).

### UI
Yes, it is in the plan. Order of build:
- **v1 job dashboard**: upload or paste a link, choose clip count and look, watch progress, review
  clips, download. No editing.
- **v1.5 form editor**: per clip, edit caption text, toggle/move overlays, trim in/out. Each change
  patches the EDL and triggers a **draft render** (`--quality draft`), shown as video. No timeline.
- **v2 live preview**: load the compiled project in an iframe and drive its seekable timelines,
  for instant scrubbing. Only then consider a timeline editor.
Caveat: GSAP's licence forbids use in no-code visual animation tools that compete with Webflow. An
end-user visual animation editor could fall under that, so keep the editor to EDL fields (not
free-form animation), or get the licence position confirmed before building v2.

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
