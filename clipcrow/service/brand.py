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
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .. import mediaguard
from ..branding import BrandError, BrandKit, probe_media
from ..edl import CaptionStyle


def _must_be_media(path: Path) -> None:
    try:
        mediaguard.check_file(path)
    except (mediaguard.NotMedia, OSError):
        raise BrandError("That file is not a video or audio file.") from None

MAX_LOGO_BYTES = 3 * 1024 * 1024
MAX_OUTRO_BYTES = 90 * 1024 * 1024
MAX_OUTRO_SECONDS = 20.0
MAX_MUSIC_BYTES = 30 * 1024 * 1024
TRACKS_DIR = Path(__file__).resolve().parent.parent / "tracks"      # the built-in background tracks (made by scripts/make_tracks.py)
TRACK_ID = re.compile(r"^[a-z0-9-]{1,40}\Z")
MIN_MUSIC_SECONDS, MAX_MUSIC_SECONDS = 5.0, 900.0
MAX_LOGO_SIDE = 4096
OWNER = re.compile(r"^([0-9a-f]{32}|shared)\Z")
BRAND_ID = re.compile(r"^[0-9a-f]{12}\Z")
MAX_BRANDS = 20
CAPTION_ANIMATIONS = ("pop", "fade", "slide", "karaoke")
SNIFF = ((b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"))
POSITIONS = ("tl", "tr", "bl", "br")
OVERLAY_FITS = ("frame", "placed")
OVERLAY_POSITIONS = ("tl", "tc", "tr", "cl", "cc", "cr", "bl", "bc", "br")
OVERLAY_SHOWS = ("always", "first", "last")
MAX_OVERLAY_BYTES = 8 * 1024 * 1024
MAX_OVERLAY_SIDE = 4096
BACKGROUNDS = ("frame", "color")
_HEX = re.compile(r"^#[0-9a-fA-F]{6}\Z")

DEFAULTS = {
    "logo_position": "tr", "logo_size_pct": 18, "logo_opacity": 0.9, "logo_margin_px": 60,
    # the brand's PNG overlay (a frame over the whole picture, or a graphic placed in it)
    "overlay_fit": "frame", "overlay_position": "br", "overlay_size_pct": 30, "overlay_opacity": 1.0, "overlay_margin_px": 40, "overlay_show": "always", "overlay_seconds": 5.0,
    "default_overlay": False,
    "cover_ms": 250, "cover_background": "frame", "cover_color": "#FFFFFF", "cover_accent": "#FFD54A",
    "cover_bg_color": "#111111", "outro_max_seconds": 6.0, "outro_gap_ms": 400,
    # what a new job ticks by default
    "default_cover": False, "default_logo": False, "default_outro": False, "default_music": False, "music_track": "",
    # who speaks (the lower third of every clip) and how this brand's captions look; "" / 0 = the app's own default
    "speaker_name": "", "speaker_title": "",
    "caption_color": "", "caption_highlight": "", "caption_font": "", "caption_font_latin": "", "caption_words": 0, "caption_top_px": 0, "caption_animation": "",
    # the "watch the full video" banner
    "banner_text": "", "banner_handle": "", "banner_platform": "youtube", "banner_top_px": 1440, "banner_seconds": 0.0, "default_banner": False,
}
_RANGES = {"logo_size_pct": (5, 40, int), "logo_opacity": (0.1, 1.0, float), "logo_margin_px": (0, 300, int),
           "overlay_size_pct": (5, 100, int), "overlay_opacity": (0.1, 1.0, float), "overlay_margin_px": (0, 300, int), "overlay_seconds": (0.5, 60.0, float),
           "cover_ms": (100, 1500, int), "outro_max_seconds": (1.0, 30.0, float), "outro_gap_ms": (0, 1500, int),
           "caption_words": (0, 8, int), "caption_top_px": (0, 1800, int), "banner_top_px": (0, 1800, int), "banner_seconds": (0.0, 60.0, float)}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_name(name) -> str:
    name = " ".join(str(name or "").split())
    if not 1 <= len(name) <= 40 or any(ord(c) < 32 for c in name):
        raise BrandError("A brand name is 1 to 40 characters.")
    return name


def caption_defaults(s: dict) -> dict:
    """The caption fields this brand sets, under the names of ``edl.CaptionStyle`` (what is left empty stays the app's default)."""
    out: dict = {}
    for key, field in (("caption_color", "color"), ("caption_highlight", "highlight_color"), ("caption_font", "font_family"), ("caption_animation", "animation")):
        if s.get(key):
            out[field] = s[key]
    if s.get("caption_words"):
        out["max_words"] = int(s["caption_words"])
    if s.get("caption_top_px"):
        out["top_px"] = int(s["caption_top_px"])
    return out


def library() -> dict:
    """The built-in tracks: ``{id: {"file", "title", "mood", "seconds"}}`` (only those whose file is there)."""
    try:
        rows = json.loads((TRACKS_DIR / "manifest.json").read_text(encoding="utf-8")).get("tracks", {})
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in rows.items() if TRACK_ID.match(k) and isinstance(v, dict) and (TRACKS_DIR / str(v.get("file", ""))).is_file()}


def track_path(track_id: str) -> Path | None:
    row = library().get(track_id) if TRACK_ID.match(track_id or "") else None
    return TRACKS_DIR / row["file"] if row else None


def sniff_image(head: bytes) -> str | None:
    for magic, suffix in SNIFF:
        if head.startswith(magic):
            return suffix
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return None


def _has_transparent_area(png: Path) -> bool:
    """Does this PNG have a real transparent area (at least 1% of it clearly see-through)? Looked at on a 48x48 copy of its alpha channel (ffmpeg, no extra library)."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(png), "-vf", "format=rgba,scale=48:48:flags=area,format=rgba,alphaextract", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                       capture_output=True, timeout=30)
    if r.returncode != 0 or len(r.stdout) < 48 * 48:
        return False
    px = r.stdout[: 48 * 48]
    return sum(1 for b in px if b < 245) >= len(px) * 0.01


class BrandStore:
    def __init__(self, data_dir: str | Path, docs=None, mode: str = "files"):
        """The records (the list of brands, each brand's settings and overlay facts) live in the files, in both, or in the database only (see ``docstore``). The
        brand's FILES (logo, overlay picture, ending video, music) are always files."""
        self.root = Path(data_dir) / "brand"
        self._lock = threading.RLock()
        self.docs, self.mode = (docs, mode) if docs is not None else (None, "files")

    def _rec_read(self, kind: str, owner: str, key: str, path: Path) -> str | None:
        if self.mode == "db":
            return self.docs.text(kind, owner, key)
        try:
            return path.read_text("utf-8")
        except OSError:
            return None

    def _rec_write(self, kind: str, owner: str, key: str, path: Path, text: str) -> None:
        if self.mode != "db":
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(text, "utf-8")
            os.replace(tmp, path)
        if self.mode != "files":
            self.docs.put(kind, owner, key, text)

    def _rec_delete(self, kind: str, owner: str, key: str, path: Path) -> None:
        if self.mode != "db":
            path.unlink(missing_ok=True)
        if self.mode != "files":
            self.docs.delete(kind, owner, key)

    # --- the brands of an owner ---------------------------------------------------------------------
    # <data>/brand/<owner>/brands.json  {"default": id, "brands": [{"id", "name", "created"}]}  and one folder per brand: <owner>/<id>/ (logo, outro, music, settings).
    # An owner whose files still sit directly in <owner>/ (before there were several brands) gets them moved into a first brand, once.
    def _owner_dir(self, owner: str) -> Path:
        if not OWNER.match(owner):
            raise KeyError(owner)
        return self.root / owner

    def _registry(self, owner: str) -> dict:
        d = self._owner_dir(owner)
        reg = d / "brands.json"
        with self._lock:
            try:
                text = self._rec_read("brand_registry", owner, "registry", reg)
                data = json.loads(text) if text else None
                if isinstance(data, dict) and isinstance(data.get("brands"), list) and data["brands"]:
                    return data
            except ValueError:
                pass
            d.mkdir(parents=True, exist_ok=True)
            bid = uuid.uuid4().hex[:12]
            bd = d / bid
            bd.mkdir()
            for legacy in [d / "settings.json", d / "outro.mp4", d / "music.m4a", *d.glob("logo.*")]:        # what an account had before there were several brands
                if legacy.is_file():
                    shutil.move(str(legacy), bd / legacy.name)
            data = {"default": bid, "brands": [{"id": bid, "name": "My brand", "created": _now()}]}
            self._write_registry(owner, data)
            return data

    def _write_registry(self, owner: str, data: dict) -> None:
        self._rec_write("brand_registry", owner, "registry", self._owner_dir(owner) / "brands.json", json.dumps(data))

    def _dir(self, owner: str, brand: str | None = None) -> Path:
        reg = self._registry(owner)
        bid = brand or reg["default"]
        if not BRAND_ID.match(bid) or not any(b["id"] == bid for b in reg["brands"]):
            raise KeyError(bid)
        return self._owner_dir(owner) / bid

    def default_id(self, owner: str) -> str:
        return self._registry(owner)["default"]

    def has(self, owner: str, brand: str) -> bool:
        return bool(BRAND_ID.match(brand or "")) and any(b["id"] == brand for b in self._registry(owner)["brands"])

    def list(self, owner: str) -> list[dict]:
        reg = self._registry(owner)
        return [{"id": b["id"], "name": b["name"], "default": b["id"] == reg["default"]} for b in reg["brands"]]

    def create(self, owner: str, name: str) -> dict:
        name = _clean_name(name)
        reg = self._registry(owner)
        if len(reg["brands"]) >= MAX_BRANDS:
            raise BrandError(f"You can have at most {MAX_BRANDS} brands.")
        with self._lock:
            bid = uuid.uuid4().hex[:12]
            (self._owner_dir(owner) / bid).mkdir(parents=True)
            reg["brands"].append({"id": bid, "name": name, "created": _now()})
            self._write_registry(owner, reg)
        return {"id": bid, "name": name, "default": False}

    def rename(self, owner: str, brand: str, name: str) -> None:
        name = _clean_name(name)
        reg = self._registry(owner)
        row = next((b for b in reg["brands"] if b["id"] == brand), None)
        if row is None:
            raise KeyError(brand)
        with self._lock:
            row["name"] = name
            self._write_registry(owner, reg)

    def set_default(self, owner: str, brand: str) -> None:
        reg = self._registry(owner)
        if not any(b["id"] == brand for b in reg["brands"]):
            raise KeyError(brand)
        with self._lock:
            reg["default"] = brand
            self._write_registry(owner, reg)

    def remove(self, owner: str, brand: str) -> None:
        """Delete a brand and its files. The last brand cannot go; deleting the default makes the oldest remaining one the default. A job that used it keeps the copies it made."""
        reg = self._registry(owner)
        if not any(b["id"] == brand for b in reg["brands"]):
            raise KeyError(brand)
        if len(reg["brands"]) == 1:
            raise BrandError("You need at least one brand.")
        with self._lock:
            reg["brands"] = [b for b in reg["brands"] if b["id"] != brand]
            if reg["default"] == brand:
                reg["default"] = reg["brands"][0]["id"]
            self._write_registry(owner, reg)
            shutil.rmtree(self._owner_dir(owner) / brand, ignore_errors=True)
            if self.mode != "files":
                self.docs.delete("brand_settings", owner, brand)
                self.docs.delete("brand_overlay", owner, brand)

    # --- settings ---------------------------------------------------------------------------------
    def settings(self, owner: str, brand: str | None = None) -> dict:
        d = self._dir(owner, brand)
        try:
            text = self._rec_read("brand_settings", owner, d.name, d / "settings.json")
            raw = json.loads(text) if text else {}
        except ValueError:
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

    def set_settings(self, owner: str, changes: dict, brand: str | None = None) -> dict:
        cur = self.settings(owner, brand)
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
            elif k == "music_track":
                if not (isinstance(v, str) and (v == "" or v in library())):
                    raise BrandError("Choose one of the listed tracks, or your own upload.")
            elif k in ("speaker_name", "speaker_title"):
                if not isinstance(v, str) or len(v.strip()) > 60 or any(ord(c) < 32 for c in v):
                    raise BrandError("A name or role is at most 60 characters.")
                v = v.strip()
            elif k in ("banner_text", "banner_handle"):
                if not isinstance(v, str) or len(v.strip()) > (60 if k == "banner_text" else 40) or any(ord(c) < 32 for c in v):
                    raise BrandError("The banner words are at most 60 characters and the channel name at most 40.")
                v = v.strip()
            elif k == "banner_platform":
                from ..platform_marks import MARKS
                if v not in MARKS:
                    raise BrandError("Choose one of the listed platforms.")
            elif k in ("caption_color", "caption_highlight"):
                if not (isinstance(v, str) and (v == "" or _HEX.match(v))):
                    raise BrandError("Colours look like #RRGGBB (or empty for the default).")
            elif k in ("caption_font", "caption_font_latin"):
                from .. import fonts
                if not (isinstance(v, str) and (v == "" or v in fonts.KNOWN)):
                    raise BrandError("Choose one of the listed fonts, or the default.")
            elif k == "caption_animation":
                if v not in ("",) + CAPTION_ANIMATIONS:
                    raise BrandError("Choose pop, fade, slide or karaoke (or the default).")
            elif k == "logo_position":
                if v not in POSITIONS:
                    raise BrandError("Choose a corner: tl, tr, bl or br.")
            elif k == "overlay_fit":
                if v not in OVERLAY_FITS:
                    raise BrandError("Choose frame (over the whole picture) or placed (a graphic at one spot).")
                if v == "frame" and self.overlay_path(owner, brand) is not None and not self.overlay_info(owner, brand).get("transparent", True):
                    raise BrandError("Your overlay has no transparent area, so over the whole frame it would hide the video. Use a PNG with transparency, or choose placed.")
            elif k == "overlay_position":
                if v not in OVERLAY_POSITIONS:
                    raise BrandError("Choose one of the nine spots: tl, tc, tr, cl, cc, cr, bl, bc, br.")
            elif k == "overlay_show":
                if v not in OVERLAY_SHOWS:
                    raise BrandError("Choose always, first or last.")
            elif k == "cover_background":
                if v not in BACKGROUNDS:
                    raise BrandError("Choose frame or color.")
            elif not (isinstance(v, str) and _HEX.match(v)):
                raise BrandError("Colours look like #RRGGBB.")
            cur[k] = v
        d = self._dir(owner, brand)
        d.mkdir(parents=True, exist_ok=True)
        self._rec_write("brand_settings", owner, d.name, d / "settings.json", json.dumps(cur))
        return cur

    # --- files ------------------------------------------------------------------------------------
    def logo_path(self, owner: str, brand: str | None = None) -> Path | None:
        return next((p for p in sorted(self._dir(owner, brand).glob("logo.*")) if p.suffix in (".png", ".jpg", ".webp")), None) \
            if self._dir(owner, brand).is_dir() else None

    def overlay_path(self, owner: str, brand: str | None = None) -> Path | None:
        p = self._dir(owner, brand) / "overlay.png"
        return p if p.is_file() else None

    def overlay_info(self, owner: str, brand: str | None = None) -> dict:
        """What we found when the overlay was uploaded: its size, and whether it has a transparent area (a frame without one would hide the video)."""
        d = self._dir(owner, brand)
        try:
            text = self._rec_read("brand_overlay", owner, d.name, d / "overlay.json")
            raw = json.loads(text) if text else {}
        except ValueError:
            return {}
        if not raw:
            return {}
        return {"transparent": bool(raw.get("transparent")), "width": int(raw.get("width") or 0), "height": int(raw.get("height") or 0)} if isinstance(raw, dict) else {}

    def put_overlay(self, owner: str, data: bytes, brand: str | None = None) -> dict:
        """A PNG to lay over the video. Only a real PNG is taken; it is decoded and written again (alpha kept, no metadata, at most 3840 px on the long side), so what is stored is
        what we made. A picture with no transparent area cannot be a whole-frame overlay: the setting goes to ``placed`` for it."""
        if not data:
            raise BrandError("That file is empty.")
        if len(data) > MAX_OVERLAY_BYTES:
            raise BrandError(f"The overlay can be at most {MAX_OVERLAY_BYTES // (1024 * 1024)} MB.")
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise BrandError("Upload a PNG (with a transparent background) for the overlay.")
        d = self._dir(owner, brand)
        d.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=d) as tmp:
            src = Path(tmp) / "in.png"
            src.write_bytes(data)
            info = probe_media(src)
            if not info["has_video"] or not 16 <= info["width"] <= MAX_OVERLAY_SIDE or not 16 <= info["height"] <= MAX_OVERLAY_SIDE:
                raise BrandError(f"The overlay must be between 16 and {MAX_OVERLAY_SIDE} pixels on each side.")
            out = Path(tmp) / "overlay.png"
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-protocol_whitelist", "file,pipe", "-i", str(src), "-frames:v", "1", "-map_metadata", "-1",
                                "-vf", "scale='min(2160,iw)':'min(3840,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2", "-pix_fmt", "rgba", str(out)],
                               capture_output=True, text=True, timeout=60)
            if r.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
                raise BrandError("That overlay could not be read.")
            transparent = _has_transparent_area(out)
            size = probe_media(out)
            with self._lock:
                shutil.move(str(out), d / "overlay.png")
                self._rec_write("brand_overlay", owner, d.name, d / "overlay.json", json.dumps({"transparent": transparent, "width": size["width"], "height": size["height"]}))
        if not transparent and self.settings(owner, brand)["overlay_fit"] == "frame":
            self.set_settings(owner, {"overlay_fit": "placed"}, brand)
        return self.describe(owner, brand)

    def outro_path(self, owner: str, brand: str | None = None) -> Path | None:
        p = self._dir(owner, brand) / "outro.mp4"
        return p if p.is_file() else None

    def put_logo(self, owner: str, data: bytes, brand: str | None = None) -> dict:
        if not data:
            raise BrandError("That file is empty.")
        if len(data) > MAX_LOGO_BYTES:
            raise BrandError(f"The logo can be at most {MAX_LOGO_BYTES // (1024 * 1024)} MB.")
        suffix = sniff_image(data[:16])
        if suffix is None:
            raise BrandError("Upload a PNG, JPEG or WebP logo. SVG files are not accepted.")
        d = self._dir(owner, brand)
        d.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=d) as tmp:
            probe = Path(tmp) / f"in{suffix}"
            probe.write_bytes(data)
            info = probe_media(probe)
            if not info["has_video"] or not 16 <= info["width"] <= MAX_LOGO_SIDE or not 16 <= info["height"] <= MAX_LOGO_SIDE:
                raise BrandError(f"The logo must be between 16 and {MAX_LOGO_SIDE} pixels on each side.")
            clean = Path(tmp) / "logo.png"                       # what is stored is what we made: decoded and re-encoded, without metadata (the server and Chrome never open the raw upload again)
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-protocol_whitelist", "file,pipe", "-i", str(probe), "-frames:v", "1", "-map_metadata", "-1", str(clean)],
                               capture_output=True, text=True, timeout=60)
            if r.returncode != 0 or not clean.is_file() or clean.stat().st_size == 0:
                raise BrandError("That logo could not be read.")
            with self._lock:
                for old in d.glob("logo.*"):
                    old.unlink()
                shutil.move(str(clean), d / "logo.png")
        return self.describe(owner, brand)

    def uploaded_music(self, owner: str, brand: str | None = None) -> Path | None:
        p = self._dir(owner, brand) / "music.m4a"
        return p if p.is_file() else None

    def music_path(self, owner: str, brand: str | None = None) -> Path | None:
        """The track a job would use: the built-in one the brand chose, else its own upload (``music_track`` "" = the upload)."""
        chosen = self.settings(owner, brand).get("music_track", "")
        return (track_path(chosen) if chosen else None) or self.uploaded_music(owner, brand)

    def put_music(self, owner: str, source: Path, brand: str | None = None) -> dict:
        """``source`` is an uploaded audio file already on disk (the caller capped its size). Re-encoded to one canonical stereo AAC file, without
        its tags or cover art: what is stored is what we made. The person is responsible for the right to use the track."""
        if source.stat().st_size > MAX_MUSIC_BYTES:
            raise BrandError(f"The music can be at most {MAX_MUSIC_BYTES // (1024 * 1024)} MB.")
        _must_be_media(source)
        info = probe_media(source)
        if not info["has_audio"]:
            raise BrandError("That file has no sound.")
        if not MIN_MUSIC_SECONDS <= info["duration"] <= MAX_MUSIC_SECONDS:
            raise BrandError(f"The music must be between {MIN_MUSIC_SECONDS:.0f} seconds and {MAX_MUSIC_SECONDS / 60:.0f} minutes long.")
        d = self._dir(owner, brand)
        d.mkdir(parents=True, exist_ok=True)
        out = d / "music.new.m4a"
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", *mediaguard.LOCAL, "-i", str(source), "-vn", "-map", "0:a:0", "-map_metadata", "-1", "-ac", "2", "-ar", "48000",
                            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)], capture_output=True, text=True, timeout=300)
        if r.returncode != 0 or not out.is_file():
            out.unlink(missing_ok=True)
            raise BrandError("That audio could not be converted. Try an MP3 or an M4A.")
        with self._lock:
            os.replace(out, d / "music.m4a")
        return self.describe(owner, brand)

    def put_outro(self, owner: str, source: Path, brand: str | None = None) -> dict:
        """``source`` is an uploaded file already on disk (the caller capped its size). Re-encoded to 1080x1920/30 fps."""
        if source.stat().st_size > MAX_OUTRO_BYTES:
            raise BrandError(f"The ending video can be at most {MAX_OUTRO_BYTES // (1024 * 1024)} MB.")
        _must_be_media(source)
        info = probe_media(source)
        if not info["has_video"]:
            raise BrandError("That file has no picture.")
        if not 0.5 <= info["duration"] <= MAX_OUTRO_SECONDS:
            raise BrandError(f"The ending video must be between 0.5 and {MAX_OUTRO_SECONDS:.0f} seconds.")
        d = self._dir(owner, brand)
        d.mkdir(parents=True, exist_ok=True)
        out = d / "outro.new.mp4"
        norm = "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=30,format=yuv420p"
        cmd = ["ffmpeg", "-y", "-v", "error", *mediaguard.LOCAL, "-i", str(source)]
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
        return self.describe(owner, brand)

    def delete(self, owner: str, what: str, brand: str | None = None) -> dict:
        d = self._dir(owner, brand)
        with self._lock:
            if what == "logo":
                for p in d.glob("logo.*"):
                    p.unlink()
            elif what == "overlay":
                (d / "overlay.png").unlink(missing_ok=True)
                self._rec_delete("brand_overlay", owner, d.name, d / "overlay.json")
            elif what == "outro":
                (d / "outro.mp4").unlink(missing_ok=True)
            elif what == "music":
                (d / "music.m4a").unlink(missing_ok=True)       # the built-in tracks are not the person's to delete: choose another or none
            else:
                raise BrandError("Unknown item.")
        return self.describe(owner, brand)

    def delete_all(self, owner: str) -> None:
        """Account erasure."""
        shutil.rmtree(self._owner_dir(owner), ignore_errors=True)
        if self.mode != "files":
            for kind in ("brand_registry", "brand_settings", "brand_overlay"):
                self.docs.delete(kind, owner)

    # --- views ------------------------------------------------------------------------------------
    def describe(self, owner: str, brand: str | None = None) -> dict:
        s = self.settings(owner, brand)
        outro = self.outro_path(owner, brand)
        music = self.music_path(owner, brand)
        lib = library()
        chosen = s.get("music_track", "")
        name = lib[chosen]["title"] if chosen in lib else ("Your upload" if music else "")
        reg = self._registry(owner)
        bid = brand or reg["default"]
        return {"id": bid, "name": next((b["name"] for b in reg["brands"] if b["id"] == bid), ""), "is_default": bid == reg["default"], "brands": self.list(owner),
                "logo": self.logo_path(owner, brand) is not None, "overlay": self.overlay_path(owner, brand) is not None, "overlay_info": self.overlay_info(owner, brand),
                "outro": outro is not None, "music": music is not None,
                "music_seconds": round(probe_media(music)["duration"], 1) if music else 0, "music_name": name,
                "uploaded_music": self.uploaded_music(owner, brand) is not None,
                "tracks": [{"id": k, "title": v.get("title", k), "mood": v.get("mood", ""), "seconds": v.get("seconds", 0)} for k, v in sorted(lib.items())],
                "outro_seconds": round(probe_media(outro)["duration"], 1) if outro else 0, "settings": s,
                "fonts": sorted(__import__("clipcrow.fonts", fromlist=["KNOWN"]).KNOWN), "font_list": __import__("clipcrow.fonts", fromlist=["installed"]).installed(), "animations": list(CAPTION_ANIMATIONS),
                "platforms": [{"id": k, "name": v[0]} for k, v in __import__("clipcrow.platform_marks", fromlist=["MARKS"]).MARKS.items()],
                "caption_base": {"color": CaptionStyle().color, "highlight": CaptionStyle().highlight_color, "top_px": CaptionStyle().top_px}}

    def kit(self, owner: str, brand: str | None = None) -> BrandKit:
        s = self.settings(owner, brand)
        return BrandKit(logo=self.logo_path(owner, brand), outro=self.outro_path(owner, brand), music=self.music_path(owner, brand), logo_position=s["logo_position"],
                        overlay=self.overlay_path(owner, brand), overlay_fit=s["overlay_fit"], overlay_position=s["overlay_position"], overlay_size_pct=int(s["overlay_size_pct"]),
                        overlay_opacity=float(s["overlay_opacity"]), overlay_margin_px=int(s["overlay_margin_px"]), overlay_show=s["overlay_show"], overlay_seconds=float(s["overlay_seconds"]),
                        logo_size_pct=s["logo_size_pct"], logo_opacity=float(s["logo_opacity"]), logo_margin_px=s["logo_margin_px"],
                        cover_ms=s["cover_ms"], cover_background=s["cover_background"], cover_color=s["cover_color"],
                        cover_accent=s["cover_accent"], cover_bg_color=s["cover_bg_color"],
                        outro_max_seconds=float(s["outro_max_seconds"]), outro_gap_ms=int(s["outro_gap_ms"]),
                        captions=caption_defaults(s), caption_font_latin=s["caption_font_latin"], speaker_name=s["speaker_name"], speaker_title=s["speaker_title"],
                        banner_text=s["banner_text"], banner_handle=s["banner_handle"], banner_platform=s["banner_platform"],
                        banner_top_px=int(s["banner_top_px"]), banner_seconds=float(s["banner_seconds"]))
