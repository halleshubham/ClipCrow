"""Pictures a person supplies for an explainer (screenshots of a news page, a chart, a document).

They are untrusted files, handled like the brand logo: recognised by their first bytes (PNG, JPEG, WebP: no SVG, which can carry script),
small, read back by ffprobe, and RE-ENCODED by ffmpeg to one PNG no bigger than ``MAX_SIDE`` pixels, so what is stored (and later rendered) is what
we made, without metadata. A picture first waits as a DRAFT in ``<data>/pictures/<owner>/<id>.png`` (``owner`` is an account id or ``shared``);
when a job is made the chosen drafts are COPIED into the job (``<job>/pictures/<id>.png``) and deleted, so editing or deleting a draft never
changes a job. Drafts nobody used expire after ``EXPIRE_HOURS``. The model never sees an address or a file name: it sees the pictures and
picks them BY NUMBER.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from ..branding import BrandError, probe_media
from .brand import OWNER, sniff_image

MAX_BYTES = 10 * 1024 * 1024
MIN_SIDE, MAX_INPUT_SIDE, MAX_SIDE = 64, 8000, 2160
MAX_PER_JOB = 6
MAX_DRAFTS = 24                      # unused drafts one owner may hold
EXPIRE_HOURS = 24
PICTURE_ID = re.compile(r"^[0-9a-f]{12}\Z")


class PictureError(ValueError):
    """The message is safe to show a person."""


class PictureStore:
    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir) / "pictures"
        self._lock = threading.Lock()

    def _dir(self, owner: str) -> Path:
        if not OWNER.match(owner):
            raise KeyError(owner)
        return self.root / owner

    def path(self, owner: str, pid: str) -> Path | None:
        if not PICTURE_ID.match(pid):
            return None
        p = self._dir(owner) / f"{pid}.png"
        return p if p.is_file() else None

    def count(self, owner: str) -> int:
        d = self._dir(owner)
        return len(list(d.glob("*.png"))) if d.is_dir() else 0

    def add(self, owner: str, data: bytes) -> dict:
        if not data:
            raise PictureError("That file is empty.")
        if len(data) > MAX_BYTES:
            raise PictureError(f"A picture can be at most {MAX_BYTES // (1024 * 1024)} MB.")
        suffix = sniff_image(data[:16])
        if suffix is None:
            raise PictureError("Upload a PNG, JPEG or WebP picture. SVG files are not accepted.")
        d = self._dir(owner)
        d.mkdir(parents=True, exist_ok=True)
        if self.count(owner) >= MAX_DRAFTS:
            raise PictureError("You have many unused pictures waiting. Start a job or remove some first.")
        with tempfile.TemporaryDirectory(dir=d) as tmp:
            src = Path(tmp) / f"in{suffix}"
            src.write_bytes(data)
            try:
                info = probe_media(src)
            except BrandError:
                raise PictureError("That file could not be read as a picture.") from None
            w, h = info["width"], info["height"]
            if not info["has_video"] or not MIN_SIDE <= min(w, h) or max(w, h) > MAX_INPUT_SIDE:
                raise PictureError(f"A picture must be between {MIN_SIDE} and {MAX_INPUT_SIDE} pixels on each side.")
            pid = uuid.uuid4().hex[:12]
            dest = Path(tmp) / "out.png"
            scale = f"scale='min({MAX_SIDE},iw)':'min({MAX_SIDE},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2"
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-frames:v", "1", "-vf", scale, "-map_metadata", "-1", str(dest)],
                               capture_output=True, text=True, timeout=60)
            if r.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
                raise PictureError("That picture could not be read.")
            with self._lock:
                shutil.move(str(dest), d / f"{pid}.png")
        out = probe_media(d / f"{pid}.png")
        return {"id": pid, "width": out["width"], "height": out["height"]}

    def delete(self, owner: str, pid: str) -> bool:
        p = self.path(owner, pid)
        if p is None:
            return False
        p.unlink(missing_ok=True)
        return True

    def adopt(self, owner: str, ids: list[str], dest: Path) -> list[str]:
        """Copy the chosen drafts into a job's folder and delete them. Returns the ids copied, in the order given."""
        dest.mkdir(parents=True, exist_ok=True)
        got: list[str] = []
        for pid in ids:
            p = self.path(owner, pid)
            if p is None:
                continue
            shutil.copyfile(p, dest / f"{pid}.png")
            got.append(pid)
        for pid in got:
            self.delete(owner, pid)
        return got

    def delete_all(self, owner: str) -> None:
        """Account erasure."""
        shutil.rmtree(self._dir(owner), ignore_errors=True)

    def expire(self, *, hours: float = EXPIRE_HOURS, now: float | None = None) -> int:
        cutoff = (now if now is not None else time.time()) - hours * 3600
        n = 0
        if not self.root.is_dir():
            return 0
        for p in self.root.glob("*/*.png"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    n += 1
            except OSError:
                continue
        return n


def parse_ids(value) -> list[str]:
    """The ``pictures`` option: ids separated by commas, strict hex, no repeats, at most ``MAX_PER_JOB``."""
    ids = [x.strip() for x in str(value or "").split(",") if x.strip()]
    if len(ids) > MAX_PER_JOB:
        raise PictureError(f"Use at most {MAX_PER_JOB} pictures.")
    if len(set(ids)) != len(ids) or any(not PICTURE_ID.match(i) for i in ids):
        raise PictureError("A picture in the list is not valid.")
    return ids
