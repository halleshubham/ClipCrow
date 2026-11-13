---
name: watch
description: Watch a reference video (a reel, Short or file) frame by frame, break down how it was edited (cuts, captions, push-ins, text behind the speaker, pacing, sound), and record what ClipCrow could adopt. Use when the user pastes a video link or points at a video file and says "watch this", "/watch", "analyze this video", "learn this style" or "how was this edited".
---

# Watch (ClipCrow development skill)

A tool for the developer, not part of the product: it is not in the Docker image and no ClipCrow user can reach it.
It studies a reference video the way an editor would, then writes down which of ClipCrow's levers would reproduce the look.

Paths are relative to the repo root. Reference videos and their analysis go to `references/<slug>/`, which is git-ignored:
never commit someone else's footage. Only the conclusions go in `docs/style/`.

## 1. Get the video

Slug: `YYYY-MM-DD_<creator>_<short-title>`, lowercase with dashes.

- **From a link:** `yt-dlp -o "references/<slug>/source.%(ext)s" --write-info-json --merge-output-format mp4 -f "bv*+ba/b" "<URL>"`.
  `source.info.json` holds the caption and stats. If a site demands a login, stop and tell the user: they can save the file
  into `references/<slug>/` themselves, or say yes in chat to `--cookies-from-browser chrome` (it reads their browser cookies,
  so only after a clear yes).
- **From a file:** copy it to `references/<slug>/source.<ext>`. Never move or delete the original.
- Several videos: one slug each, then one combined pass at the end (step 5).

## 2. Measure it

```bash
uv run --python 3.12 --with numpy --with pillow --with faster-whisper --with "av<19" \
  .claude/skills/watch/scripts/analyze.py references/<slug>/source.mp4 references/<slug>/watch
```
Needs `ffmpeg` and `ffprobe` on PATH. Transcription uses faster-whisper, the engine ClipCrow uses (`--model large-v3` for
Marathi or Hindi; the first run downloads the model; `--no-transcript` for music-only videos). The `av<19` pin matters:
PyAV 19 breaks faster-whisper.

It writes to `references/<slug>/watch/`: `index.md` (stats and reading order: read first), `shots_NN.jpg` (one thumbnail
per shot), `sheets/` (frame pairs 0.2 s apart, labelled with times), `audio/` (spectrogram strips with loudness, cuts,
sound-effect and syllable markers), `data.json` (exact numbers) and `transcript.json/.srt/.txt` (word timings).
Options: `--grid 3x3 --width 300` for long videos, `--step 0.5`, `--threshold 0.2` (missed cuts) or `0.4` (camera moves
counted as cuts).

**Dense mode** for fast moments (a transition, a caption animation, a push-in):
```bash
uv run --python 3.12 --with numpy --with pillow --with faster-whisper --with "av<19" \
  .claude/skills/watch/scripts/analyze.py references/<slug>/source.mp4 references/<slug>/watch \
  --start 12 --end 14 --step 0.1 --gap 0.033 --frames-only --tag dense_12-14
```

## 3. Watch it

In time order, every sheet. Videos over 3 minutes: the first 60 s and every cut-adjacent pair, sample the rest. For each pair
compare A and B (what moved, appeared, scaled, changed). Read the matching audio strip per 15 s and tie sounds to picture.
Keep a running `references/<slug>/watch/log.md`, one line per moment (time, what, how). Measure, do not guess easing or timing.

Look for, at least: hook (first 1 to 3 s), shot lengths and cuts per minute, pauses removed, punch-ins (scale, duration,
on which words), reframing and speaker changes (hard cut or pan), captions (size against frame height, position, words per
caption, highlight rule and colour, entrance and exit, sync with speech), on-screen text and cards, text behind the
person and how it is revealed, transitions, music and sound effects, colour grade, retention devices, the ending.

## 4. Write the breakdown

`references/<slug>/analysis.md` from [analysis-template.md](references/analysis-template.md). Concrete and measurable, a
timestamp on every claim, and mark what was measured against what is inferred.

## 5. Map it to ClipCrow

Record, in `docs/style/`, what the reference does and how ClipCrow's levers would reproduce it (or that they cannot):

| Look | ClipCrow lever |
|---|---|
| Caption size, words per caption, colours, position | `edl.CaptionStyle` (`max_words`, `pause_break`, `color`, `highlight_color`, `top_px`), `captions.py` |
| Push-in on a phrase | `edl.PunchIn` (`scale` 1.03 to 1.5, `duration` 0.5 to 8 s), `punch.py` |
| Framing, face tracking, split screen | `edl.Layout.framing` (`cover`, `track`, `fit_blur`, `split`), `framing.py`, `reframe.py` |
| Text behind the speaker | `Layout.mode = cutout_behind`, `matte.py` |
| Cover, logo, outro | `branding.py` |
| Clip length, spread, number of clips | planner options (`min_clip`, `max_clip`, `spread`, `clips`) |
| Anything else | a gap: describe it, do not invent an EDL field here |

Rules:
- **Learn techniques, not content.** Never copy another creator's footage, logos, music or scripts into the repo.
- **Respect ClipCrow's invariants** (see `CLAUDE.md`): the EDL and LLM output are untrusted, cut points and overlay timing are
  code, a speaker change is a hard cut, layer order is footage z1, text behind z2, cutout z3, captions z4, cards z5. A finding
  that would break one of these is recorded as "does not fit", with the reason.
- **Marathi/Hindi copy stays Devanagari.** Note a reference's font only as a look to translate to `fonts.DEFAULT_DEVANAGARI`.
- **This skill only proposes.** Do not change `clipcrow/` while watching. Turn findings into code as a separate, reviewed change.
- Each finding names its source slugs and a strength count, e.g. `_(seen in: 2026-11-13_creator_abc · 1)_`. A new reference
  that agrees adds its slug and bumps the count. One that contradicts becomes a named variant; never overwrite.

Add one entry to `docs/style/reference-log.md`: slug, link, creator, date, format, 3 to 5 takeaways.

## 6. Report back

Briefly: what the video does best; what ClipCrow could adopt, and through which lever; what does not fit and why; what could
not be determined. Offer to prototype one finding as a real change.
