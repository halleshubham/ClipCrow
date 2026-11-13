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

import hashlib
import html
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import fonts
from .captions import CaptionGroup, clip_words, group_words
from .edl import EDL, Clip, Cover, EDLError, Hook, Logo, LowerThird, MediaCard, StatPill, TextBehind

GSAP_HINT = "run scripts/fetch_gsap.sh, or pass gsap_js=<path to gsap.min.js>"


@dataclass
class CompiledClip:
    project_dir: Path
    duration: float
    groups: list[CaptionGroup] = field(default_factory=list)
    font_family: str = ""
    scenes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)    # things a person should know (e.g. no matte, so no text behind)


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


def _even(n: float) -> int:
    return max(2, int(n) // 2 * 2)


def prepare_footage(src: Path, start: float, duration: float, fps: int, width: int, height: int, dest: Path,
                    *, framing: str = "cover", keyframes=(), src_size: tuple[int, int] | None = None,
                    split=()) -> None:
    """Cut the clip window out of the source at render size, a keyframe every second (HyperFrames freezes
    on seek when keyframes are further apart), fitted to the canvas by ``framing``:

      cover     scale to fill and centre-crop
      track     crop a 9:16 window whose x follows ``keyframes`` (clip time, cx fraction of source width)
      fit_blur  whole frame fitted to the width over a blurred copy of itself
      split     two people stacked: each half is a crop of the source centred on that person (``split`` is
                two ``(cx, cy)`` fractions of the source), scaled to half the canvas height
    """
    dest = Path(dest).resolve()
    enc = ["-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-g", str(fps),
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k"]
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(src)]
    cover = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},fps={fps}"
    cwd = None
    if framing == "track" and keyframes and src_size and src_size[0] / src_size[1] > width / height * 1.05:
        sw, sh = src_size
        cw = min(_even(sh * width / height), _even(sw))
        xs = [(t, min(max(cx * sw - cw / 2, 0), sw - cw)) for t, cx in keyframes]
        cmds = "\n".join(f"{t:.3f} crop@c x {x:.0f};" for t, x in xs) + "\n"
        (dest.parent / "framing.cmd").write_text(cmds, "utf-8")
        cwd = dest.parent                                    # relative name: no path escaping in the filtergraph
        vf = (f"sendcmd=f=framing.cmd,crop@c=w={cw}:h={sh}:x={xs[0][1]:.0f}:y=0,"
              f"scale={width}:{height}:flags=lanczos,fps={fps}")
        cmd += ["-vf", vf, *enc, str(dest)]
    elif framing == "split" and len(split) == 2 and src_size:
        sw, sh = src_size
        half = height // 2
        cw = min(_even(sh * width / half), _even(sw))          # window with the aspect of one half
        ch = min(_even(cw * half / width), _even(sh))
        wins = []
        for cx, cy in split:
            x = min(max(cx * sw - cw / 2, 0), sw - cw)
            y = min(max(cy * sh - ch / 2, 0), sh - ch)
            wins.append(f"crop={cw}:{ch}:{x:.0f}:{y:.0f},scale={width}:{half}:flags=lanczos,setsar=1")
        fc = (f"[0:v]split=2[a][b];[a]{wins[0]}[top];[b]{wins[1]}[bot];[top][bot]vstack,fps={fps}[v]")
        cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "0:a?", *enc, str(dest)]
    elif framing == "fit_blur":
        fc = (f"[0:v]split=2[a][b];[a]scale={width}:{height}:force_original_aspect_ratio=increase,"
              f"crop={width}:{height},boxblur=40:6[bg];[b]scale={width}:-2[fg];"
              f"[bg][fg]overlay=(W-w)/2:(H-h)/2,fps={fps}[v]")
        cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "0:a?", *enc, str(dest)]
    else:
        cmd += ["-vf", cover, *enc, str(dest)]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if r.returncode != 0:
        raise EDLError(f"ffmpeg could not cut the clip: {r.stderr.strip()[-400:]}")


def _cached_matte(footage: Path, dest: Path, matte_fn, cache_dir: str | Path | None) -> None:
    """The matte is the slow step (about 2 fps on a CPU), and it depends only on the prepared footage. Keyed by a
    hash of that file, so re-rendering after a text edit reuses it and a change to the cut or the framing does not."""
    if cache_dir is None:
        matte_fn(footage, dest)
        return
    h = hashlib.sha256()
    with open(footage, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    hit = cache / f"{h.hexdigest()[:24]}.webm"
    if hit.is_file() and hit.stat().st_size > 0:
        shutil.copyfile(hit, dest)
        return
    matte_fn(footage, dest)
    tmp = hit.with_suffix(".tmp")
    shutil.copyfile(dest, tmp)
    tmp.replace(hit)


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


def _shown(text: str) -> str:
    """Caption text as displayed: terminal full stops, commas and dandas are dropped (? and ! stay)."""
    return text.rstrip(".,;:\u0964\u0965\u2026") or text


def _caption_size(chars: int) -> int:
    # ~0.62 em per Devanagari/Latin glyph on average; the safe band is ~900 px wide.
    return int(max(48, min(92, 900 / (max(chars, 1) * 0.62))))


def _captions_scene(clip: Clip, groups: list[CaptionGroup], family: str, face: str,
                    w: int, h: int) -> str:
    st = clip.captions
    emph = set(st.emphasis_words)
    css = f"""    {face}
    .grp {{ position: absolute; left: 0; right: 0; top: {st.top_px}px; text-align: center; overflow: visible;
      font-family: {family}, sans-serif; font-weight: {st.font_weight}; line-height: 1.25; color: {st.color};
      text-shadow: 0 4px 0 rgba(0,0,0,.85), 0 0 24px rgba(0,0,0,.6); }}
    .grp .w {{ display: inline-block; margin: 0 .16em; }}
    .grp .hot {{ color: {st.highlight_color}; }}"""
    body, timeline = [], []
    for n, g in enumerate(groups):
        chars = sum(len(_shown(x.text)) for x in g.words) + len(g.words) - 1
        spans = "".join(
            f'<span class="w{" hot" if x.index in emph else ""}" id="g{n}w{m}">{_esc(_shown(x.text))}</span>'
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
      font-family: {family}, sans-serif; font-weight: 700; text-shadow: 0 8px 40px rgba(0,0,0,.35); }}"""
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
      background: rgba(15,16,22,.78); color: #fff; font-family: {family}, sans-serif; font-weight: 700; }}
    .lt .n {{ font-size: 54px; line-height: 1.3; }}
    .lt .r {{ font-size: 36px; color: #ffd54a; margin-top: 6px; }}
    .hook {{ position: absolute; left: 60px; right: 60px; padding: 24px 32px; border-radius: 24px; text-align: center;
      background: rgba(15,16,22,.82); color: #fff; line-height: 1.25; font-family: {family}, sans-serif; font-weight: 700; }}
    .pill {{ position: absolute; right: 60px; padding: 20px 30px; border-radius: 999px; background: #ffd54a;
      color: #111; font-family: {family}, sans-serif; font-weight: 700; font-size: 52px; }}"""
    body, script = [], []
    for o, t0, d in items:
        end_fade = _num(max(t0, t0 + d - 0.35))
        if isinstance(o, LowerThird):
            sub = f'<div class="r">{_esc(o.subtitle)}</div>' if o.subtitle else ""
            body.append(f'    <div id="ov-{o.id}" class="lt clip" data-start="{_num(t0)}" data-duration="{_num(d)}" '
                        f'style="top:{o.top_px}px"><div class="n">{_esc(o.title)}</div>{sub}</div>')
            script.append(f'    tl.fromTo("#ov-{o.id}", {{ opacity: 0, x: -60 }}, '
                          f'{{ opacity: 1, x: 0, duration: 0.4, ease: "power2.out" }}, {_num(t0)});')
        elif isinstance(o, Hook):
            size = 64 if len(o.text) <= 40 else 52
            body.append(f'    <div id="ov-{o.id}" class="hook clip" data-start="{_num(t0)}" data-duration="{_num(d)}" '
                        f'style="top:{o.top_px}px;font-size:{size}px">{_esc(o.text)}</div>')
            script.append(f'    tl.fromTo("#ov-{o.id}", {{ opacity: 0, y: -40 }}, '
                          f'{{ opacity: 1, y: 0, duration: 0.3, ease: "power2.out" }}, {_num(t0)});')
        else:
            body.append(f'    <div id="ov-{o.id}" class="pill clip" data-start="{_num(t0)}" data-duration="{_num(d)}" '
                        f'style="top:{o.top_px}px">{_esc(o.text)}</div>')
            script.append(f'    tl.fromTo("#ov-{o.id}", {{ opacity: 0, scale: 0.6 }}, '
                          f'{{ opacity: 1, scale: 1, duration: 0.3, ease: "back.out(2)" }}, {_num(t0)});')
        script.append(f'    tl.to("#ov-{o.id}", {{ opacity: 0, duration: 0.3, ease: "power2.in" }}, {end_fade});')
    return _scene("cards", w, h, css, "\n".join(body), "\n".join(script))


MEDIA_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def _media_scene(items, files: dict[str, str], stack: str, face: str, w: int, h: int) -> str:
    """Pictures shown over the footage: a full-frame cutaway or a rounded panel, with a slow move and a fade."""
    css = f"""    {face}
    .mc {{ position: absolute; overflow: hidden; opacity: 0; background: #000; }}
    .mc.full {{ left: 0; top: 0; width: {w}px; height: {h}px; }}
    .mc.panel {{ left: 60px; width: {w - 120}px; border-radius: 36px; box-shadow: 0 18px 60px rgba(0,0,0,.55); }}
    .mc img {{ position: absolute; inset: 0; width: 100%; height: 100%; }}
    .mc.full img {{ object-fit: cover; }} .mc.panel img {{ object-fit: contain; }}
    .mc .cap {{ position: absolute; left: 0; right: 0; bottom: 0; padding: 22px 36px; text-align: center;
      font-family: {stack}, sans-serif; font-weight: 700; font-size: 42px; line-height: 1.3; color: #fff;
      background: linear-gradient(transparent, rgba(0,0,0,.72)); }}"""
    body, script = [], []
    for o, t0, d in items:
        geom = f"top:{o.top_px}px;height:{o.height_px}px" if o.placement == "panel" else ""
        cap = f'<div class="cap">{_esc(o.caption)}</div>' if o.caption else ""
        body.append(f'    <div id="m-{o.id}" class="mc {o.placement} clip" data-start="{_num(t0)}" '
                    f'data-duration="{_num(d)}" style="{geom}"><img id="mi-{o.id}" src="{files[o.asset]}" alt="">{cap}</div>')
        move = {"zoom_in": '{ scale: 1.12 }', "pan_left": '{ x: -70, scale: 1.1 }', "pan_right": '{ x: 70, scale: 1.1 }'}.get(o.motion)
        script.append(f'    tl.fromTo("#m-{o.id}", {{ opacity: 0 }}, {{ opacity: 1, duration: 0.25, ease: "power1.out" }}, {_num(t0)});')
        if move:
            script.append(f'    tl.fromTo("#mi-{o.id}", {{ scale: 1, x: 0 }}, Object.assign({move}, '
                          f'{{ duration: {_num(d)}, ease: "none" }}), {_num(t0)});')
        script.append(f'    tl.to("#m-{o.id}", {{ opacity: 0, duration: 0.25, ease: "power1.in" }}, {_num(max(t0, t0 + d - 0.25))});')
    return _scene("media", w, h, css, "\n".join(body), "\n".join(script))


def cover_font_size(text: str) -> int:
    """The cover's type size from the text length alone (deterministic, so a re-render never reflows)."""
    n = len(text.strip())
    for limit, size in ((14, 190), (28, 150), (44, 120), (64, 100)):
        if n <= limit:
            return size
    return 86


def _cover_scene(cv: Cover, stack: str, face: str, w: int, h: int) -> str:
    """The title card for the first ``duration_ms``: solid from the very first frame (no fade-in), so a platform that
    takes frame 0 as the poster gets the title."""
    words = cv.text.split()
    head, last = (" ".join(words[:-1]), words[-1]) if len(words) > 1 else ("", cv.text.strip())
    shade = "rgba(0,0,0,.58)" if cv.background == "frame" else cv.bg_color
    html_text = (f'{_esc(head)} ' if head else "") + f'<span class="ac">{_esc(last)}</span>'
    css = f"""    {face}
    .cv {{ position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; text-align: center;
      padding: 0 90px; background: {shade}; }}
    .cv .t {{ font-family: {stack}, sans-serif; font-weight: 700; font-size: {cover_font_size(cv.text)}px; line-height: 1.18;
      color: {cv.color}; text-shadow: 0 6px 28px rgba(0,0,0,.6); }}
    .cv .ac {{ color: {cv.accent}; }}"""
    body = (f'    <div id="cover" class="cv clip" data-start="0" data-duration="{_num(cv.duration_ms / 1000)}">'
            f'<div class="t">{html_text}</div></div>')
    return _scene("cover", w, h, css, body, '    tl.set("#cover", { opacity: 1 }, 0);')


_LOGO_TOP_SAFE, _LOGO_BOTTOM_SAFE = 250, 380    # clear of the platform status bar / the hook, and of the reel's own buttons


def _logo_scene(lg: Logo, src: str, start: float, dur: float, w: int, h: int) -> str:
    width = round(w * lg.size_pct / 100)
    v = f"top:{_LOGO_TOP_SAFE + lg.margin_px}px" if lg.position[0] == "t" else f"bottom:{_LOGO_BOTTOM_SAFE + lg.margin_px}px"
    side = f"left:{lg.margin_px}px" if lg.position[1] == "l" else f"right:{lg.margin_px}px"
    css = f"""    #logo {{ position: absolute; {v}; {side}; width: {width}px; }}
    #logo img {{ display: block; width: 100%; height: auto; opacity: 0; }}"""
    body = (f'    <div id="logo" class="clip" data-start="{_num(start)}" data-duration="{_num(dur)}">'
            f'<img id="logo-img" src="{src}" alt=""></div>')
    script = f'    tl.to("#logo-img", {{ opacity: {_num(lg.opacity)}, duration: 0.2, ease: "power1.out" }}, {_num(start)});'
    return _scene("logo", w, h, css, body, script)


def _index(duration: float, w: int, h: int, lang: str, scenes: set[str], cutout: bool, punch=()) -> str:
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
    if "media" in scenes:                       # above the cutout: a cutaway must hide the person too
        parts.append(host("media", 4, 5, "graphics"))
    if "captions" in scenes:
        parts.append(host("captions", 5, 3, "captions"))
    if "cards" in scenes:
        parts.append(host("cards", 6, 4, "graphics"))
    if "logo" in scenes:
        parts.append(host("logo", 7, 6, "graphics"))
    if "cover" in scenes:                       # on top of everything: the first frames are the poster
        parts.append(host("cover", 9, 7, "graphics"))
    pushes = ""
    if punch:
        # The footage, the text behind the speaker and the cutout must move together or the text would slide
        # across the person: one tween on all of them, around the same point.
        tweens = []
        for p in punch:
            tweens.append(f'      tl.fromTo(stage, {{ scale: 1, transformOrigin: "50% {_num(p.anchor_y * 100)}%" }}, '
                          f'{{ scale: {_num(p.scale)}, duration: {_num(p.duration)}, ease: "none" }}, {_num(p.t)});\n'
                          f'      tl.set(stage, {{ scale: 1 }}, {_num(p.t + p.duration)});')
        pushes = ('      const stage = ["#footage", "#behind", "#cutout"].filter((s) => document.querySelector(s));\n'
                  + "\n".join(tweens) + "\n")
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
      const tl = gsap.timeline({{ paused: true }});
      window.__timelines["main"] = tl;
{pushes}    </script>
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
                 matte_cache: str | Path | None = None, asset_root: str | Path | None = None,
                 prepare: bool = True) -> CompiledClip:
    """Write the HyperFrames project for ``clip`` into ``project_dir``.

    ``prepare=False`` writes only the HTML (no ffmpeg, no matte), for tests."""
    project = Path(project_dir)
    media_root = Path(media_root)
    w, h, fps = edl.render.width, edl.render.height, edl.render.fps
    duration = round(clip.duration * fps) / fps

    (project / "assets" / "fonts").mkdir(parents=True, exist_ok=True)
    (project / "compositions").mkdir(parents=True, exist_ok=True)
    for stale in (project / "compositions").glob("*.html"):   # a re-render (after an edit) reuses the folder:
        stale.unlink()                                         # a scene that no longer exists must not linger

    # vendored GSAP: the render browser must not fetch it from a CDN
    gsap = Path(gsap_js) if gsap_js else None
    if gsap is None or not gsap.is_file():
        raise EDLError(f"gsap.min.js not found; {GSAP_HINT}")
    shutil.copyfile(gsap, project / "assets" / "gsap.min.js")

    words = clip_words(edl.transcript, clip.source_in, clip.source_out)
    groups = group_words(words, duration, max_words=clip.captions.max_words,
                         pause_break=clip.captions.pause_break) if clip.captions.enabled else []
    overlays = _overlay_times(edl, clip)

    cover = clip.cover if clip.cover and clip.cover.enabled else None
    logo = clip.logo if clip.logo and clip.logo.enabled else None
    texts = [x.text for g in groups for x in g.words]
    if cover:
        texts.append(cover.text)
    for o, _, _ in overlays:
        texts += [getattr(o, a) for a in ("text", "title", "subtitle", "caption") if hasattr(o, a)]
    family = fonts.effective_family(clip.captions.font_family, texts)
    font_file = fonts.resolve_font(family, clip.captions.font_weight)
    shutil.copyfile(font_file, project / "assets" / "fonts" / font_file.name)
    face = _font_face(family, font_file.name, clip.captions.font_weight)
    stack = f'"{family}"'
    latin = fonts.companion(family, *texts)           # Latin letters (WhatsApp, 10x) need their own face
    if latin:
        lfile = fonts.resolve_font(latin, clip.captions.font_weight)
        shutil.copyfile(lfile, project / "assets" / "fonts" / lfile.name)
        face += "\n    " + _font_face(latin, lfile.name, clip.captions.font_weight)
        stack += f', "{latin}"'

    media = [x for x in overlays if isinstance(x[0], MediaCard)]
    behind = [x for x in overlays if isinstance(x[0], TextBehind)]
    cards = [x for x in overlays if isinstance(x[0], (LowerThird, StatPill, Hook))]
    files: dict[str, str] = {}
    if media:
        by_id = {x.id: x for x in edl.assets}
        (project / "assets" / "media").mkdir(parents=True, exist_ok=True)
        for o, _, _ in media:
            asset = by_id[o.asset]
            if not asset.uri:
                raise EDLError(f"overlay {o.id!r}: asset {o.asset!r} has no file yet (it has to be fetched or generated first)")
            if asset.kind != "image":
                raise EDLError(f"overlay {o.id!r}: only images can be shown for now ({o.asset!r} is a {asset.kind})")
            suffix = Path(asset.uri).suffix.lower()
            if suffix not in MEDIA_SUFFIXES:
                raise EDLError(f"asset {o.asset!r}: {suffix or 'no extension'} is not a supported picture type")
            files[o.asset] = f"assets/media/{o.asset}{suffix}"
            if prepare:
                shutil.copyfile(_inside(Path(asset_root) if asset_root else media_root, asset.uri), project / files[o.asset])
    scenes: list[str] = []
    if media:
        (project / "compositions" / "media.html").write_text(_media_scene(media, files, stack, face, w, h), "utf-8")
        scenes.append("media")
    if behind:
        (project / "compositions" / "behind.html").write_text(_behind_scene(behind, stack, face, w, h), "utf-8")
        scenes.append("behind")
    if groups:
        (project / "compositions" / "captions.html").write_text(
            _captions_scene(clip, groups, stack, face, w, h), "utf-8")
        scenes.append("captions")
    if cards:
        (project / "compositions" / "cards.html").write_text(_cards_scene(cards, stack, face, w, h), "utf-8")
        scenes.append("cards")
    if logo:
        asset = next((a for a in edl.assets if a.id == logo.asset), None)
        if asset is None or not asset.uri:
            raise EDLError(f"clip {clip.id!r}: the logo asset {logo.asset!r} has no file")
        suffix = Path(asset.uri).suffix.lower()
        if suffix not in MEDIA_SUFFIXES:
            raise EDLError(f"asset {logo.asset!r}: {suffix or 'no extension'} is not a supported logo type")
        (project / "assets" / "brand").mkdir(parents=True, exist_ok=True)
        rel = f"assets/brand/logo{suffix}"
        if prepare:
            shutil.copyfile(_inside(Path(asset_root) if asset_root else media_root, asset.uri), project / rel)
        t0 = min(cover.duration_ms / 1000, duration) if cover else 0.0
        (project / "compositions" / "logo.html").write_text(_logo_scene(logo, rel, t0, duration - t0, w, h), "utf-8")
        scenes.append("logo")
    if cover:
        (project / "compositions" / "cover.html").write_text(_cover_scene(cover, stack, face, w, h), "utf-8")
        scenes.append("cover")

    cutout = clip.layout.mode == "cutout_behind"
    (project / "index.html").write_text(
        _index(duration, w, h, edl.language.primary, set(scenes), cutout, clip.layout.punch_ins), "utf-8")
    (project / "hyperframes.json").write_text(json.dumps({
        "paths": {"blocks": "compositions", "components": "compositions/components", "assets": "assets"},
        "media": {"autoProxy": True}}, indent=2), "utf-8")
    (project / "meta.json").write_text(json.dumps({"id": clip.id, "name": clip.id}, indent=2), "utf-8")

    if prepare:
        src = _inside(media_root, edl.source.uri)
        footage = project / "assets" / "footage.mp4"
        prepare_footage(src, clip.source_in, clip.duration, fps, w, h, footage,
                        framing=clip.layout.framing,
                        keyframes=[(k.t, k.cx) for k in clip.layout.reframe],
                        src_size=(edl.source.width, edl.source.height),
                        split=[(p.cx, p.cy) for p in clip.layout.split])
        if cutout:
            dest = project / "assets" / "cutout.webm"
            if clip.layout.cutout_uri:
                shutil.copyfile(_inside(media_root, clip.layout.cutout_uri), dest)
            elif matte_fn is not None:
                _cached_matte(footage, dest, matte_fn, matte_cache)
            else:
                raise EDLError("layout.mode 'cutout_behind' needs layout.cutout_uri or a matte_fn")
    return CompiledClip(project, duration, groups, family, scenes)
