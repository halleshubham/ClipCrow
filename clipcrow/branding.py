"""Cover title, logo and ending video: putting a brand on a clip.

  * the cover title is a scene of the HyperFrames project (``compile``); here is what decides its words,
  * the logo is a scene too; here is where the brand's files come from,
  * the ending video is joined AFTER the render, with FFmpeg: ``append_outro``,
  * ``extract_cover`` saves frame 0 of the finished clip as a PNG (the poster).

Honest limit: a platform decides for itself which frame is the poster. Frame 0 carrying the title is the best
a video file can do; Instagram and TikTok let the person pick a cover when they post.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .edl import EDL, Asset, Clip, Cover, Logo, Music, Outro

CANVAS_FPS = 30
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


class BrandError(ValueError):
    """A sentence that is safe to show a person."""


@dataclass
class BrandKit:
    """What one account (or the whole install) has uploaded, and how it likes it drawn."""
    logo: Optional[Path] = None
    outro: Optional[Path] = None
    music: Optional[Path] = None
    logo_position: str = "tr"
    logo_size_pct: int = 18
    logo_opacity: float = 0.9
    logo_margin_px: int = 60
    cover_ms: int = 250
    cover_background: str = "frame"
    cover_color: str = "#FFFFFF"
    cover_accent: str = "#FFD54A"
    cover_bg_color: str = "#111111"
    outro_max_seconds: float = 6.0
    outro_gap_ms: int = 400
    captions: dict = field(default_factory=dict)       # CaptionStyle fields this brand sets (colour, font, position ...); empty = the app's own look
    speaker_name: str = ""                              # the lower third every clip gets unless the job names someone
    speaker_title: str = ""


@dataclass
class BrandingChoice:
    cover: bool = False
    logo: bool = False
    outro: bool = False
    music: bool = False
    captions: bool = False                              # the brand's caption look (not when a reference style already set it)

    def any(self) -> bool:
        return self.cover or self.logo or self.outro or self.music or self.captions


def cover_text(clip: Clip) -> str:
    """The words on the cover: the hook if the clip has one, else the title. The model is not asked for another line."""
    hook = next((o.text for o in clip.overlays if o.kind == "hook" and o.text.strip()), "")
    text = re.sub(r"\s+", " ", hook or clip.title or "").strip()
    if len(text) > 80:
        text = text[:80].rsplit(" ", 1)[0] or text[:80]
    return text


LOGO_TOP_SAFE, LOGO_BOTTOM_SAFE = 250, 380       # the same two numbers as compile._LOGO_*_SAFE (a test keeps them equal)
CAPTION_BAND = (-30, 330)                         # the caption band, from the caption top: a few lines of text
SHRINKS = (1.0, 0.85, 0.7, 0.55, 0.4)
SIDE_TIGHT = 24                                   # px: the narrowest side margin the placement will use


def logo_aspect(path) -> float:
    """Height over width of the logo picture (1.0 when it cannot be read)."""
    try:
        import cv2
        img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        return float(img.shape[0]) / float(img.shape[1]) if img is not None and img.shape[1] else 1.0
    except Exception:
        return 1.0


def fit_logo(logo: Logo, zone, caption_top: Optional[int], aspect: float, width: int = 1080, height: int = 1920) -> Logo:
    """The logo kept off the face. ``zone`` is where the face is in the frame over the clip (fractions), ``caption_top`` where the captions start (None: no captions).
    The brand's corner is used when it is clear. Otherwise the logo slides down the left or right side to the nearest spot that is clear and, if there is none,
    gets smaller (never below 40% of its size) until there is; when nothing is clear it takes the spot that overlaps least at the smallest size."""
    base = logo.model_copy(update={"top_px": None, "shrink": 1.0, "side_px": None})
    if zone is None:
        return base
    x0, y0, x1, y1 = zone
    keep = [(x0 * width - 0.04 * width, y0 * height - 0.08 * height, x1 * width + 0.04 * width, y1 * height + 0.03 * height)]      # hair above the face box, a margin around it
    if caption_top is not None:
        keep.append((0.0, caption_top + CAPTION_BAND[0], float(width), caption_top + CAPTION_BAND[1]))
    m = logo.margin_px

    def hits(r) -> float:
        total = 0.0
        for a, b, c, d in keep:
            w_, h_ = min(r[2], c) - max(r[0], a), min(r[3], d) - max(r[1], b)
            if w_ > 0 and h_ > 0:
                total += w_ * h_
        return total

    def rect(side: str, top: float, wpx: float, gap: Optional[int] = None):
        g = m if gap is None else gap
        x = g if side == "l" else width - g - wpx
        return (x, top, x + wpx, top + wpx * aspect)
    wpx = width * logo.size_pct / 100
    hpx = wpx * aspect
    own_top = LOGO_TOP_SAFE + m if logo.position[0] == "t" else height - LOGO_BOTTOM_SAFE - m - hpx
    if hits(rect(logo.position[1], own_top, wpx)) == 0:
        return base
    other = "l" if logo.position[1] == "r" else "r"
    worst = None
    for shrink in SHRINKS:
        wpx = width * logo.size_pct * shrink / 100
        hpx = wpx * aspect
        lo, hi = LOGO_TOP_SAFE, int(height - LOGO_BOTTOM_SAFE - hpx)
        gaps = (m, min(m, SIDE_TIGHT))                               # the brand's side margin first, a narrower one when that is what keeps it off the face
        spots = [(side, float(top), gap) for gap in gaps for side in (logo.position[1], other) for top in range(lo, max(lo, hi) + 1, 20)]
        clear = [(side, top, gap) for side, top, gap in spots if hits(rect(side, top, wpx, gap)) == 0]
        if clear:
            side, top, gap = min(clear, key=lambda c: (c[2] != m, c[0] != logo.position[1], abs(c[1] - own_top)))
            return logo.model_copy(update={"position": ("t" if top < height / 2 else "b") + side, "top_px": int(top), "shrink": shrink, "side_px": None if gap == m else gap})
        worst = min(spots, key=lambda c: (hits(rect(c[0], c[1], wpx, c[2])), c[2] != m, c[0] != logo.position[1], abs(c[1] - own_top))) + (shrink,)
    side, top, gap, shrink = worst
    return logo.model_copy(update={"position": ("t" if top < height / 2 else "b") + side, "top_px": int(top), "shrink": shrink, "side_px": None if gap == m else gap})


def make_cover(clip: Clip, kit: BrandKit, text: str | None = None) -> Optional[Cover]:
    text = (text if text is not None else cover_text(clip)).strip()
    if not text:
        return None
    ms = min(kit.cover_ms, int(clip.duration * 500))
    if ms < 100:
        return None
    return Cover(text=text[:80], duration_ms=ms, background=kit.cover_background, color=kit.cover_color,
                 accent=kit.cover_accent, bg_color=kit.cover_bg_color)


def apply_brand(edl: EDL, kit: BrandKit, choice: BrandingChoice, out: str | Path) -> EDL:
    """The EDL with the cover, logo and outro set on every clip. The brand's files are COPIED into ``<out>/brand``: a job
    keeps the logo it was made with, whatever the account uploads later, and the EDL only ever names files under ``out``."""
    out = Path(out)
    assets = [a for a in edl.assets if not a.id.startswith("brand-")]
    folder = out / "brand"
    new_assets: list[Asset] = []
    for what, src, kind, key in (("logo", kit.logo, "image", "brand-logo"), ("outro", kit.outro, "video", "brand-outro"),
                                 ("music", kit.music, "audio", "brand-music")):
        if src is None or not Path(src).is_file():
            continue
        suffix = Path(src).suffix.lower()
        if kind == "image" and suffix not in IMAGE_SUFFIXES:
            continue
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"{what}{suffix}"
        if Path(src).resolve() != dest.resolve():
            shutil.copyfile(src, dest)
        new_assets.append(Asset(id=key, kind=kind, origin="upload", uri=f"brand/{dest.name}", alt=f"brand {what}"))
    have = {a.id for a in new_assets}
    if choice.logo and "brand-logo" not in have:
        raise BrandError("No logo has been uploaded yet.")
    if choice.outro and "brand-outro" not in have:
        raise BrandError("No ending video has been uploaded yet.")
    if choice.music and "brand-music" not in have:
        raise BrandError("No background music has been uploaded yet.")
    clips = []
    for c in edl.clips:
        upd: dict = {}
        if choice.cover:
            upd["cover"] = make_cover(c, kit)
        if choice.logo:
            lg = Logo(asset="brand-logo", position=kit.logo_position, size_pct=kit.logo_size_pct,
                      opacity=kit.logo_opacity, margin_px=kit.logo_margin_px)
            cap = c.captions
            upd["logo"] = fit_logo(lg, c.layout.face_zone, cap.top_px if cap.enabled else None, logo_aspect(kit.logo) if kit.logo else 1.0, edl.render.width, edl.render.height)
        if choice.outro:
            upd["outro"] = Outro(asset="brand-outro", max_seconds=kit.outro_max_seconds, gap_ms=kit.outro_gap_ms)
        if choice.music:
            upd["music"] = Music(asset="brand-music")
        if choice.captions and kit.captions:
            upd["captions"] = c.captions.model_copy(update=kit.captions)
        clips.append(c.model_copy(update=upd) if upd else c)
    return EDL.model_validate({**edl.model_dump(exclude_none=True),
                               "assets": [a.model_dump(exclude_defaults=True) for a in [*assets, *new_assets]],
                               "clips": [c.model_dump(exclude_none=True) for c in clips]})


# --- FFmpeg -----------------------------------------------------------------------------------------------

def _run(cmd: list[str], what: str) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-1:] or ["unknown error"]
        raise BrandError(f"{what} failed: {tail[0][:200]}")


def probe_media(path: str | Path) -> dict:
    """{"duration", "width", "height", "has_audio", "has_video"} from ffprobe (the file is never trusted)."""
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration",
                        "-of", "json", str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        raise BrandError("That file could not be read as media.")
    d = json.loads(r.stdout or "{}")
    streams = d.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    try:
        dur = float(d.get("format", {}).get("duration") or 0)
    except ValueError:
        dur = 0.0
    return {"duration": dur, "width": int((video or {}).get("width") or 0), "height": int((video or {}).get("height") or 0),
            "has_video": video is not None, "has_audio": any(s.get("codec_type") == "audio" for s in streams)}


def append_outro(main: str | Path, outro: str | Path, dest: str | Path, *, width: int, height: int,
                 fps: int = CANVAS_FPS, max_seconds: float = 6.0, gap_ms: int = 0) -> Path:
    """``main`` followed by the first ``max_seconds`` of ``outro`` (scaled to fit the canvas, black bars if the shape
    differs), re-encoded as one H.264/AAC file. A silent ending gets silence, so the audio stays one stream. ``gap_ms`` > 0 puts a dip between them so the
    end of the clip does not look cut off: the clip fades out (0.2 s), ``gap_ms`` of black and silence, the ending fades in (0.2 s). 0 is a straight cut."""
    main, outro, dest = Path(main), Path(outro), Path(dest)
    m, o = probe_media(main), probe_media(outro)
    if not o["has_video"]:
        raise BrandError("The ending video has no picture.")
    take = min(max_seconds, o["duration"] or max_seconds)
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(main)]
    if m["has_audio"]:
        pass
    else:
        cmd += ["-f", "lavfi", "-t", f"{m['duration']:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    cmd += ["-t", f"{take:.3f}", "-i", str(outro)]
    if not o["has_audio"]:
        cmd += ["-f", "lavfi", "-t", f"{take:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    a_main = 0 if m["has_audio"] else 1
    i_outro = 1 if m["has_audio"] else 2
    a_outro = i_outro if o["has_audio"] else i_outro + 1
    norm = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps={fps},format=yuv420p"
    fc = (f"[0:v]{norm}[v0];[{i_outro}:v]{norm}[v1];"
          f"[{a_main}:a]aresample=48000,aformat=channel_layouts=stereo[a0];[{a_outro}:a]aresample=48000,aformat=channel_layouts=stereo[a1];"
          f"[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]")
    if gap_ms > 0:
        gap = gap_ms / 1000
        fo = round(min(0.2, max(m["duration"], 0.5) / 4), 3)              # the clip's last moments fade out ...
        fi = round(min(0.2, take / 4), 3)                                  # ... and the ending's first moments fade in
        fc = (f"[0:v]{norm},fade=t=out:st={max(m['duration'] - fo, 0):.3f}:d={fo}[v0];[{i_outro}:v]{norm},fade=t=in:st=0:d={fi}[v1];"
              f"color=c=black:s={width}x{height}:r={fps}:d={gap:.3f},format=yuv420p,setsar=1[vg];"
              f"[{a_main}:a]aresample=48000,aformat=channel_layouts=stereo,afade=t=out:st={max(m['duration'] - fo, 0):.3f}:d={fo}[a0];"
              f"[{a_outro}:a]aresample=48000,aformat=channel_layouts=stereo,afade=t=in:st=0:d={fi}[a1];"
              f"anullsrc=r=48000:cl=stereo,atrim=0:{gap:.3f}[ag];"
              f"[v0][a0][vg][ag][v1][a1]concat=n=3:v=1:a=1[v][a]")
    cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(dest)]
    _run(cmd, "Adding the ending video")
    return dest


def extract_cover(video: str | Path, dest: str | Path) -> Path:
    """Frame 0 of the finished clip, as a PNG."""
    dest = Path(dest)
    _run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-frames:v", "1", str(dest)], "Saving the cover image")
    return dest
