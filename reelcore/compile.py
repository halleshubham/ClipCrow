"""Compile one EDL clip into a HyperFrames project directory.

    <project>/
      index.html                  root: footage, text-behind scene, cutout, captions, cards
      compositions/behind.html    text that sits behind the speaker (only with a cutout)
      compositions/captions.html  caption groups
      compositions/cards.html     lower thirds, stat pills
      assets/footage.mp4          the clip window, re-encoded with a keyframe every second
      assets/cutout.webm          person matte (alpha), when layout.mode == cutout_behind
      assets/gsap.min.js          vendored: the render browser must not need the network
      assets/fonts/*.ttf          the fonts the text needs (HyperFrames' bundled ones are Latin only)
      hyperframes.json, meta.json project config

Everything that came from the EDL is HTML-escaped or restricted to a safe shape
(see edl.py); script blocks only ever receive numbers and validated ids.
"""
from __future__ import annotations

import html
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import fonts
from .captions import CaptionGroup, clip_words, group_words
from .edl import EDL, Clip, EDLError, LowerThird, StatPill, TextBehind

GSAP_HINT = "run scripts/fetch_gsap.sh, or pass gsap_js=<path to gsap.min.js>"


@dataclass
class CompiledClip:
    project_dir: Path
    duration: float
    groups: list[CaptionGroup] = field(default_factory=list)
    font_family: str = ""
    scenes: list[str] = field(default_factory=list)


# --- media ------------------------------------------------------------------

def _inside(media_root: Path, uri: str) -> Path:
    """A local media file, which must live under media_root (an EDL is untrusted input)."""
    if "://" in uri:
        raise EDLError(f"remote media is not allowed in an EDL ({uri!r}); fetch it first")
    root = media_root.resolve()
    p = (root / uri).resolve() if not Path(uri).is_absolute() else Path(uri).resolve()
    if not p.is_relative_to(root):
        raise EDLError(f"media {uri!r} is outside the media root")
    if not p.is_file():
        raise EDLError(f"media file not found: {uri!r}")
    return p


def prepare_footage(src: Path, start: float, duration: float, fps: int,
                    width: int, height: int, dest: Path) -> None:
    """Cut the clip window out of the source at render size, a keyframe every
    second (HyperFrames freezes on seek when keyframes are further apart)."""
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
           "-i", str(src),
           "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                  f"crop={width}:{height},fps={fps}",
           "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-g", str(fps),
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", str(dest)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise EDLError(f"ffmpeg could not cut the clip: {r.stderr.strip()[-400:]}")


# --- html -------------------------------------------------------------------

def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _num(x: float) -> str:
    return f"{x:.3f}".rstrip("0").rstrip(".") or "0"


def _font_face(family: str, file_name: str, weight: int) -> str:
    return (f'@font-face {{ font-family: "{family}"; src: url("assets/fonts/{file_name}"); '
            f'font-weight: {weight}; }}')


def _scene(name: str, w: int, h: int, css: str, body: str, script: str) -> str:
    return f"""<template>
  <style>
    #root {{ position: absolute; inset: 0; }}
{css}
  </style>
  <div id="root" data-composition-id="{name}" data-width="{w}" data-height="{h}">
{body}
  </div>
  <script>
    const tl = gsap.timeline({{ paused: true }});
{script}
    window.__timelines["{name}"] = tl;
  </script>
</template>
"""


def _caption_size(chars: int) -> int:
    # ~0.62 em per Devanagari/Latin glyph on average; the safe band is ~900 px wide.
    return int(max(48, min(92, 900 / (max(chars, 1) * 0.62))))


def _captions_scene(clip: Clip, groups: list[CaptionGroup], family: str, face: str,
                    w: int, h: int) -> str:
    st = clip.captions
    emph = set(st.emphasis_words)
    css = f"""    {face}
    .grp {{ position: absolute; left: 0; right: 0; top: {st.top_px}px; text-align: center; overflow: visible;
      font-family: "{family}", sans-serif; font-weight: {st.font_weight}; line-height: 1.25; color: {st.color};
      text-shadow: 0 4px 0 rgba(0,0,0,.85), 0 0 24px rgba(0,0,0,.6); }}
    .grp .w {{ display: inline-block; margin: 0 .16em; }}
    .grp .hot {{ color: {st.highlight_color}; }}"""
    body, timeline = [], []
    for n, g in enumerate(groups):
        chars = sum(len(x.text) for x in g.words) + len(g.words) - 1
        spans = "".join(
            f'<span class="w{" hot" if x.index in emph else ""}" id="g{n}w{m}">{_esc(x.text)}</span>'
            for m, x in enumerate(g.words))
        body.append(f'    <div id="g{n}" class="grp clip" data-start="{_num(g.start)}" '
                    f'data-duration="{_num(g.end - g.start)}" style="font-size:{_caption_size(chars)}px">{spans}</div>')
        timeline.append([f"g{n}", round(g.start, 3), round(g.end, 3), [round(x.start, 3) for x in g.words]])
    script = f"""    const G = {json.dumps(timeline)};
    G.forEach(([id, s, e, times]) => {{
      tl.fromTo("#" + id, {{ opacity: 0, y: 24 }}, {{ opacity: 1, y: 0, duration: 0.18, ease: "power2.out" }}, s);
      times.forEach((t, i) => tl.fromTo("#" + id + "w" + i, {{ scale: 0.7, opacity: 0.3 }},
        {{ scale: 1, opacity: 1, duration: 0.16, ease: "back.out(2)" }}, Math.max(t, s)));
      tl.to("#" + id, {{ opacity: 0, duration: 0.12, ease: "power2.in" }}, Math.max(s, e - 0.12));
      tl.set("#" + id, {{ opacity: 0, visibility: "hidden" }}, e);
    }});"""
    return _scene("captions", w, h, css, "\n".join(body), script)


def _behind_scene(items, family: str, face: str, w: int, h: int) -> str:
    css = f"""    {face}
    .bw {{ position: absolute; left: 0; right: 0; text-align: center; overflow: visible; line-height: 1.1;
      font-family: "{family}", sans-serif; font-weight: 700; text-shadow: 0 8px 40px rgba(0,0,0,.35); }}"""
    body, script = [], []
    for o, t0, d in items:
        body.append(f'    <div id="ov-{o.id}" class="bw clip" data-start="{_num(t0)}" data-duration="{_num(d)}" '
                    f'style="top:{o.top_px}px;font-size:{o.font_size}px;color:{o.color}" '
                    f'data-layout-allow-occlusion>{_esc(o.text)}</div>')
        script.append(f'    tl.fromTo("#ov-{o.id}", {{ opacity: 0, scale: 0.8 }}, '
                      f'{{ opacity: 1, scale: 1, duration: 0.35, ease: "power3.out" }}, {_num(t0)});')
        script.append(f'    tl.to("#ov-{o.id}", {{ opacity: 0, duration: 0.25, ease: "power2.in" }}, '
                      f'{_num(max(t0, t0 + d - 0.25))});')
    return _scene("behind", w, h, css, "\n".join(body), "\n".join(script))


def _cards_scene(items, family: str, face: str, w: int, h: int) -> str:
    css = f"""    {face}
    .lt {{ position: absolute; left: 60px; width: 700px; padding: 26px 34px; border-radius: 22px;
      background: rgba(15,16,22,.78); color: #fff; font-family: "{family}", sans-serif; font-weight: 700; }}
    .lt .n {{ font-size: 54px; line-height: 1.3; }}
    .lt .r {{ font-size: 36px; color: #ffd54a; margin-top: 6px; }}
    .pill {{ position: absolute; right: 60px; padding: 20px 30px; border-radius: 999px; background: #ffd54a;
      color: #111; font-family: "{family}", sans-serif; font-weight: 700; font-size: 52px; }}"""
    body, script = [], []
    for o, t0, d in items:
        end_fade = _num(max(t0, t0 + d - 0.35))
        if isinstance(o, LowerThird):
            sub = f'<div class="r">{_esc(o.subtitle)}</div>' if o.subtitle else ""
            body.append(f'    <div id="ov-{o.id}" class="lt clip" data-start="{_num(t0)}" data-duration="{_num(d)}" '
                        f'style="top:{o.top_px}px"><div class="n">{_esc(o.title)}</div>{sub}</div>')
            script.append(f'    tl.fromTo("#ov-{o.id}", {{ opacity: 0, x: -60 }}, '
                          f'{{ opacity: 1, x: 0, duration: 0.4, ease: "power2.out" }}, {_num(t0)});')
        else:
            body.append(f'    <div id="ov-{o.id}" class="pill clip" data-start="{_num(t0)}" data-duration="{_num(d)}" '
                        f'style="top:{o.top_px}px">{_esc(o.text)}</div>')
            script.append(f'    tl.fromTo("#ov-{o.id}", {{ opacity: 0, scale: 0.6 }}, '
                          f'{{ opacity: 1, scale: 1, duration: 0.3, ease: "back.out(2)" }}, {_num(t0)});')
        script.append(f'    tl.to("#ov-{o.id}", {{ opacity: 0, duration: 0.3, ease: "power2.in" }}, {end_fade});')
    return _scene("cards", w, h, css, "\n".join(body), "\n".join(script))


def _index(duration: float, w: int, h: int, lang: str, scenes: set[str], cutout: bool) -> str:
    def host(name, z, track, kind):
        return (f'      <div id="{name}" data-composition-id="{name}" data-composition-src="compositions/{name}.html" '
                f'data-start="0" data-duration="{_num(duration)}" data-track-index="{track}" '
                f'data-track-kind="{kind}" data-width="{w}" data-height="{h}" style="z-index:{z}"></div>')
    parts = [f'      <video id="footage" class="clip full" src="assets/footage.mp4" data-start="0" '
             f'data-duration="{_num(duration)}" data-track-index="0" data-has-audio="true" playsinline '
             f'style="z-index:1"></video>']
    if "behind" in scenes:
        parts.append(host("behind", 2, 1, "graphics"))
    if cutout:
        parts.append(f'      <video id="cutout" class="clip full" src="assets/cutout.webm" data-start="0" '
                     f'data-duration="{_num(duration)}" data-track-index="2" muted playsinline '
                     f'data-layout-allow-occlusion style="z-index:3"></video>')
    if "captions" in scenes:
        parts.append(host("captions", 4, 3, "captions"))
    if "cards" in scenes:
        parts.append(host("cards", 5, 4, "graphics"))
    return f"""<!doctype html>
<html lang="{_esc(lang)}">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width={w}, height={h}" />
    <script src="assets/gsap.min.js"></script>
    <style>
      * {{ margin: 0; padding: 0; box-sizing: border-box; }}
      html, body {{ width: {w}px; height: {h}px; overflow: hidden; background: #000; }}
      #root {{ position: relative; width: 100%; height: 100%; }}
      .full {{ position: absolute; left: 0; top: 0; width: {w}px; height: {h}px; object-fit: contain; }}
    </style>
  </head>
  <body>
    <div id="root" data-composition-id="main" data-start="0" data-duration="{_num(duration)}" data-width="{w}" data-height="{h}">
{chr(10).join(parts)}
    </div>
    <script>
      window.__timelines = window.__timelines || {{}};
      window.__timelines["main"] = gsap.timeline({{ paused: true }});
    </script>
  </body>
</html>
"""


# --- entry point --------------------------------------------------------------

def _overlay_times(edl: EDL, clip: Clip):
    """(overlay, clip-relative start, duration) for each overlay, clamped to the clip."""
    out = []
    for o in clip.overlays:
        if o.t_in is not None:
            t0 = o.t_in
        else:
            if o.trigger_word_index >= len(edl.transcript):
                raise EDLError(f"overlay {o.id!r}: trigger_word_index {o.trigger_word_index} is not in the transcript")
            t0 = edl.transcript[o.trigger_word_index].s - clip.source_in
        if not 0 <= t0 < clip.duration:
            raise EDLError(f"overlay {o.id!r} starts at {t0:.2f}s, outside the {clip.duration:.2f}s clip")
        out.append((o, t0, min(o.duration, clip.duration - t0)))
    return out


def compile_clip(edl: EDL, clip: Clip, project_dir: str | Path, *, media_root: str | Path,
                 gsap_js: str | Path | None = None,
                 matte_fn: Optional[Callable[[Path, Path], object]] = None,
                 prepare: bool = True) -> CompiledClip:
    """Write the HyperFrames project for ``clip`` into ``project_dir``.

    ``prepare=False`` writes only the HTML (no ffmpeg, no matte), for tests."""
    project = Path(project_dir)
    media_root = Path(media_root)
    w, h, fps = edl.render.width, edl.render.height, edl.render.fps
    duration = round(clip.duration * fps) / fps

    (project / "assets" / "fonts").mkdir(parents=True, exist_ok=True)
    (project / "compositions").mkdir(parents=True, exist_ok=True)

    # vendored GSAP: the render browser must not fetch it from a CDN
    gsap = Path(gsap_js) if gsap_js else None
    if gsap is None or not gsap.is_file():
        raise EDLError(f"gsap.min.js not found; {GSAP_HINT}")
    shutil.copyfile(gsap, project / "assets" / "gsap.min.js")

    words = clip_words(edl.transcript, clip.source_in, clip.source_out)
    groups = group_words(words, duration, max_words=clip.captions.max_words,
                         pause_break=clip.captions.pause_break) if clip.captions.enabled else []
    overlays = _overlay_times(edl, clip)

    texts = [x.text for g in groups for x in g.words]
    for o, _, _ in overlays:
        texts += [getattr(o, a) for a in ("text", "title", "subtitle") if hasattr(o, a)]
    family = fonts.effective_family(clip.captions.font_family, texts)
    font_file = fonts.resolve_font(family, clip.captions.font_weight)
    shutil.copyfile(font_file, project / "assets" / "fonts" / font_file.name)
    face = _font_face(family, font_file.name, clip.captions.font_weight)

    behind = [x for x in overlays if isinstance(x[0], TextBehind)]
    cards = [x for x in overlays if isinstance(x[0], (LowerThird, StatPill))]
    scenes: list[str] = []
    if behind:
        (project / "compositions" / "behind.html").write_text(_behind_scene(behind, family, face, w, h), "utf-8")
        scenes.append("behind")
    if groups:
        (project / "compositions" / "captions.html").write_text(
            _captions_scene(clip, groups, family, face, w, h), "utf-8")
        scenes.append("captions")
    if cards:
        (project / "compositions" / "cards.html").write_text(_cards_scene(cards, family, face, w, h), "utf-8")
        scenes.append("cards")

    cutout = clip.layout.mode == "cutout_behind"
    (project / "index.html").write_text(
        _index(duration, w, h, edl.language.primary, set(scenes), cutout), "utf-8")
    (project / "hyperframes.json").write_text(json.dumps({
        "paths": {"blocks": "compositions", "components": "compositions/components", "assets": "assets"},
        "media": {"autoProxy": True}}, indent=2), "utf-8")
    (project / "meta.json").write_text(json.dumps({"id": clip.id, "name": clip.id}, indent=2), "utf-8")

    if prepare:
        src = _inside(media_root, edl.source.uri)
        footage = project / "assets" / "footage.mp4"
        prepare_footage(src, clip.source_in, clip.duration, fps, w, h, footage)
        if cutout:
            dest = project / "assets" / "cutout.webm"
            if clip.layout.cutout_uri:
                shutil.copyfile(_inside(media_root, clip.layout.cutout_uri), dest)
            elif matte_fn is not None:
                matte_fn(footage, dest)
            else:
                raise EDLError("layout.mode 'cutout_behind' needs layout.cutout_uri or a matte_fn")
    return CompiledClip(project, duration, groups, family, scenes)
