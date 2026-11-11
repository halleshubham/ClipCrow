# reelcore

EDL in, short video out. A planner (an LLM or a person) writes an **Edit Decision List**; reelcore
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

## Use
```bash
pip install -e '.[dev]'
./scripts/fetch_gsap.sh                      # GSAP is fetched, never committed (see Licences)
python examples/gen_assets.py                # synthetic demo footage + cutout
reelcore validate examples/demo.edl.json
reelcore render examples/demo.edl.json --clip c1 --out build/c1.mp4 \
         --media-root examples/assets --gsap vendor/gsap.min.js
pytest                                       # unit tests
REELCORE_RENDER_TESTS=1 pytest -m render     # + a real render (needs Node 22, FFmpeg, Chrome)
```
Fonts: install `fonts-noto-core` or set `REELCORE_FONT_DIR`. The CLI version is pinned
(`render.HYPERFRAMES_VERSION`); override the command with `REELCORE_HF_BIN`, the browser with
`REELCORE_BROWSER`.

## Measured (spike, 4 CPU cores, no GPU)
12 s of 1080×1920 @30 fps renders in ~21 s (≈1.8 s per output second). Person matte on CPU is
~2 fps (a 60 s reel ≈ 14 min), so production needs a GPU (`device="cuda"`) or a lower matte rate.

## Roadmap
1. **Planner** (next): transcript → EDL with an LLM (moment picking, hooks, overlay plan), in the
   transcript's language/script; bring over the Indic ASR + language voting and reframing from the
   OpenShorts fork (MIT parts only).
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
- Code taken from the OpenShorts fork keeps its MIT notice. Do **not** copy its `cloud/` directory
  (separate commercial licence) or depend on `ultralytics` (AGPL) without a licence.

Project licence: not chosen yet.
