"""The brand kit: a logo, an ending video and how the cover and logo are drawn, kept per account.

Uploads are untrusted files. A logo is a PNG, JPEG or WebP (SVG is refused: it can carry script), recognised by its
first bytes and not by its name, read back by ffprobe, and small. An ending video is checked by ffprobe (a picture,
a short length) and re-encoded to one canonical H.264/AAC MP4, so what is stored is what we made, not what was sent.
Files live in ``<data>/brand/<owner>/``; ``owner`` is an account id (hex) or ``shared`` when there are no accounts.
A job COPIES the files it uses (``branding.apply_brand``), so changing the kit never alters a finished job.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from ..branding import BrandError, BrandKit, probe_media

MAX_LOGO_BYTES = 3 * 1024 * 1024
MAX_OUTRO_BYTES = 90 * 1024 * 1024
MAX_OUTRO_SECONDS = 20.0
MAX_LOGO_SIDE = 4096
OWNER = re.compile(r"^([0-9a-f]{32}|shared)\Z")
SNIFF = ((b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"))
POSITIONS = ("tl", "tr", "bl", "br")
BACKGROUNDS = ("frame", "color")
_HEX = re.compile(r"^#[0-9a-fA-F]{6}\Z")

DEFAULTS = {
    "logo_position": "tr", "logo_size_pct": 18, "logo_opacity": 0.9, "logo_margin_px": 60,
    "cover_ms": 250, "cover_background": "frame", "cover_color": "#FFFFFF", "cover_accent": "#FFD54A",
    "cover_bg_color": "#111111", "outro_max_seconds": 6.0,
    # what a new job ticks by default
    "default_cover": False, "default_logo": False, "default_outro": False,
}
_RANGES = {"logo_size_pct": (5, 40, int), "logo_opacity": (0.1, 1.0, float), "logo_margin_px": (0, 300, int),
           "cover_ms": (100, 1500, int), "outro_max_seconds": (1.0, 30.0, float)}


def sniff_image(head: bytes) -> str | None:
    for magic, suffix in SNIFF:
        if head.startswith(magic):
            return suffix
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return None


class BrandStore:
    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir) / "brand"
        self._lock = threading.Lock()

    def _dir(self, owner: str) -> Path:
        if not OWNER.match(owner):
            raise KeyError(owner)
        return self.root / owner

    # --- settings ---------------------------------------------------------------------------------
    def settings(self, owner: str) -> dict:
        try:
            raw = json.loads((self._dir(owner) / "settings.json").read_text("utf-8"))
        except (OSError, ValueError):
            raw = {}
        out = dict(DEFAULTS)
        for k, default in DEFAULTS.items():
            v = raw.get(k) if isinstance(raw, dict) else None
            if isinstance(default, bool):
                ok = isinstance(v, bool)
            elif isinstance(default, (int, float)):
                ok = isinstance(v, (int, float)) and not isinstance(v, bool)
            else:
                ok = isinstance(v, str)
            if ok:
                out[k] = v
        return out

    def set_settings(self, owner: str, changes: dict) -> dict:
        cur = self.settings(owner)
        for k, v in changes.items():
            if k not in DEFAULTS:
                raise BrandError("Unknown setting.")
            want = type(DEFAULTS[k])
            if want is bool:
                if not isinstance(v, bool):
                    raise BrandError("Use true or false.")
            elif k in _RANGES:
                lo, hi, typ = _RANGES[k]
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
                    raise BrandError(f"{k.replace('_', ' ').capitalize()} must be between {lo} and {hi}.")
                v = typ(v)
            elif k == "logo_position":
                if v not in POSITIONS:
                    raise BrandError("Choose a corner: tl, tr, bl or br.")
            elif k == "cover_background":
                if v not in BACKGROUNDS:
                    raise BrandError("Choose frame or color.")
            elif not (isinstance(v, str) and _HEX.match(v)):
                raise BrandError("Colours look like #RRGGBB.")
            cur[k] = v
        d = self._dir(owner)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "settings.tmp"
        tmp.write_text(json.dumps(cur), "utf-8")
        os.replace(tmp, d / "settings.json")
        return cur

    # --- files ------------------------------------------------------------------------------------
    def logo_path(self, owner: str) -> Path | None:
        return next((p for p in sorted(self._dir(owner).glob("logo.*")) if p.suffix in (".png", ".jpg", ".webp")), None) \
            if self._dir(owner).is_dir() else None

    def outro_path(self, owner: str) -> Path | None:
        p = self._dir(owner) / "outro.mp4"
        return p if p.is_file() else None

    def put_logo(self, owner: str, data: bytes) -> dict:
        if not data:
            raise BrandError("That file is empty.")
        if len(data) > MAX_LOGO_BYTES:
            raise BrandError(f"The logo can be at most {MAX_LOGO_BYTES // (1024 * 1024)} MB.")
        suffix = sniff_image(data[:16])
        if suffix is None:
            raise BrandError("Upload a PNG, JPEG or WebP logo. SVG files are not accepted.")
        d = self._dir(owner)
        d.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=d) as tmp:
            probe = Path(tmp) / f"in{suffix}"
            probe.write_bytes(data)
            info = probe_media(probe)
            if not info["has_video"] or not 16 <= info["width"] <= MAX_LOGO_SIDE or not 16 <= info["height"] <= MAX_LOGO_SIDE:
                raise BrandError(f"The logo must be between 16 and {MAX_LOGO_SIDE} pixels on each side.")
            with self._lock:
                for old in d.glob("logo.*"):
                    old.unlink()
                shutil.move(str(probe), d / f"logo{suffix}")
        return self.describe(owner)

    def put_outro(self, owner: str, source: Path) -> dict:
        """``source`` is an uploaded file already on disk (the caller capped its size). Re-encoded to 1080x1920/30 fps."""
        if source.stat().st_size > MAX_OUTRO_BYTES:
            raise BrandError(f"The ending video can be at most {MAX_OUTRO_BYTES // (1024 * 1024)} MB.")
        info = probe_media(source)
        if not info["has_video"]:
            raise BrandError("That file has no picture.")
        if not 0.5 <= info["duration"] <= MAX_OUTRO_SECONDS:
            raise BrandError(f"The ending video must be between 0.5 and {MAX_OUTRO_SECONDS:.0f} seconds.")
        d = self._dir(owner)
        d.mkdir(parents=True, exist_ok=True)
        out = d / "outro.new.mp4"
        norm = "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=30,format=yuv420p"
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(source)]
        if not info["has_audio"]:
            cmd += ["-f", "lavfi", "-t", f"{info['duration']:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        cmd += ["-vf", norm, "-map", "0:v:0", "-map", "0:a:0" if info["has_audio"] else "1:a:0", "-t", f"{info['duration']:.3f}",
                "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            out.unlink(missing_ok=True)
            raise BrandError("That video could not be converted. Try an MP4.")
        with self._lock:
            os.replace(out, d / "outro.mp4")
        return self.describe(owner)

    def delete(self, owner: str, what: str) -> dict:
        d = self._dir(owner)
        with self._lock:
            if what == "logo":
                for p in d.glob("logo.*"):
                    p.unlink()
            elif what == "outro":
                (d / "outro.mp4").unlink(missing_ok=True)
            else:
                raise BrandError("Unknown item.")
        return self.describe(owner)

    def delete_all(self, owner: str) -> None:
        """Account erasure."""
        shutil.rmtree(self._dir(owner), ignore_errors=True)

    # --- views ------------------------------------------------------------------------------------
    def describe(self, owner: str) -> dict:
        s = self.settings(owner)
        outro = self.outro_path(owner)
        return {"logo": self.logo_path(owner) is not None, "outro": outro is not None,
                "outro_seconds": round(probe_media(outro)["duration"], 1) if outro else 0, "settings": s}

    def kit(self, owner: str) -> BrandKit:
        s = self.settings(owner)
        return BrandKit(logo=self.logo_path(owner), outro=self.outro_path(owner), logo_position=s["logo_position"],
                        logo_size_pct=s["logo_size_pct"], logo_opacity=float(s["logo_opacity"]), logo_margin_px=s["logo_margin_px"],
                        cover_ms=s["cover_ms"], cover_background=s["cover_background"], cover_color=s["cover_color"],
                        cover_accent=s["cover_accent"], cover_bg_color=s["cover_bg_color"],
                        outro_max_seconds=float(s["outro_max_seconds"]))
