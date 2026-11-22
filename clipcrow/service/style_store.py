"""Saved styles, kept per account: one small JSON record each at ``<data>/styles/<owner>/<style id>.json``.

A record holds the NUMBERS measured from a reference and the bounded settings derived from them (``styles.StyleSettings``).
The reference video itself is never stored: the caller measures it in a temporary folder and throws it away.
``owner`` is an account id (hex) or ``shared`` when there are no accounts; ids are strict hex and end in ``\\Z``.
A job COPIES the settings it uses, so editing or deleting a style never changes a job that already started.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..styles import StyleError, StyleSettings

OWNER = re.compile(r"^([0-9a-f]{32}|shared)\Z")
STYLE_ID = re.compile(r"^[0-9a-f]{12}\Z")
MAX_STYLES = 10


class StyleRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str = Field(min_length=1, max_length=60)
    status: Literal["analyzing", "ready", "failed"] = "analyzing"
    error: str = Field(default="", max_length=300)
    created_at: str
    measured: dict = Field(default_factory=dict)
    settings: StyleSettings = Field(default_factory=StyleSettings)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_name(name: str) -> str:
    name = " ".join(str(name or "").split())[:60]
    if not name:
        raise StyleError("Give the style a name.")
    return name


class StyleStore:
    def __init__(self, data_dir: str | Path, docs=None, mode: str = "files"):
        """``docs``/``mode``: where the records live (see ``docstore``): the files (default), both (files are read), or the database only."""
        self.root = Path(data_dir) / "styles"
        self._lock = threading.RLock()
        self.docs, self.mode = (docs, mode) if docs is not None else (None, "files")

    def _dir(self, owner: str) -> Path:
        if not OWNER.match(owner):
            raise KeyError(owner)
        return self.root / owner

    def _path(self, owner: str, style_id: str) -> Path:
        if not STYLE_ID.match(style_id):
            raise KeyError(style_id)
        return self._dir(owner) / f"{style_id}.json"

    def _write(self, owner: str, rec: StyleRecord) -> None:
        self._dir(owner)                                    # validates the owner
        text = rec.model_dump_json()
        if self.mode != "db":
            d = self._dir(owner)
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / f"{rec.id}.tmp"
            tmp.write_text(text, "utf-8")
            os.replace(tmp, self._path(owner, rec.id))
        if self.mode != "files":
            self.docs.put("style", owner, rec.id, text)

    def list(self, owner: str) -> list[StyleRecord]:
        d = self._dir(owner)
        out = []
        if self.mode == "db":
            texts = [t for _, t in self.docs.items("style", owner)]
        else:
            texts = []
            for p in sorted(d.glob("*.json")) if d.is_dir() else []:
                try:
                    texts.append(p.read_text("utf-8"))
                except OSError:
                    continue
        for t in texts:
            try:
                out.append(StyleRecord.model_validate_json(t))
            except ValueError:
                continue                               # a damaged record is skipped, not fatal
        return sorted(out, key=lambda r: r.created_at, reverse=True)

    def get(self, owner: str, style_id: str) -> StyleRecord:
        self._path(owner, style_id)                         # validates both ids
        try:
            if self.mode == "db":
                text = self.docs.text("style", owner, style_id)
                if text is None:
                    raise KeyError(style_id)
            else:
                text = self._path(owner, style_id).read_text("utf-8")
            return StyleRecord.model_validate_json(text)
        except (OSError, ValueError):
            raise KeyError(style_id)

    def create(self, owner: str, name: str) -> StyleRecord:
        """A new record in ``analyzing`` state. At most MAX_STYLES per owner, and one analysis at a time."""
        name = clean_name(name)
        with self._lock:
            have = self.list(owner)
            if len(have) >= MAX_STYLES:
                raise StyleError(f"You can keep {MAX_STYLES} styles. Delete one first.")
            if any(r.status == "analyzing" for r in have):
                raise StyleError("A reference is still being measured. Wait for it to finish.")
            rec = StyleRecord(id=uuid.uuid4().hex[:12], name=name, created_at=_now())
            self._write(owner, rec)
            return rec

    def finish(self, owner: str, style_id: str, measured: dict, settings: StyleSettings) -> StyleRecord:
        with self._lock:
            rec = self.get(owner, style_id).model_copy(update={"status": "ready", "error": "", "measured": measured,
                                                                "settings": settings})
            self._write(owner, rec)
            return rec

    def fail(self, owner: str, style_id: str, message: str) -> None:
        with self._lock:
            try:
                rec = self.get(owner, style_id)
            except KeyError:
                return                                  # deleted while it was being measured
            self._write(owner, rec.model_copy(update={"status": "failed", "error": message[:300]}))

    def update(self, owner: str, style_id: str, *, name: Optional[str] = None,
               settings: Optional[StyleSettings] = None) -> StyleRecord:
        with self._lock:
            rec = self.get(owner, style_id)
            if rec.status == "analyzing":
                raise StyleError("Wait until the measuring has finished.")
            changes: dict = {}
            if name is not None:
                changes["name"] = clean_name(name)
            if settings is not None:
                changes["settings"] = settings.check()
            rec = rec.model_copy(update=changes)
            self._write(owner, rec)
            return rec

    def delete(self, owner: str, style_id: str) -> None:
        with self._lock:
            self._path(owner, style_id)                      # validates both ids
            if self.mode != "db":
                self._path(owner, style_id).unlink(missing_ok=True)
            if self.mode != "files":
                self.docs.delete("style", owner, style_id)

    def delete_all(self, owner: str) -> None:
        """Account erasure."""
        shutil.rmtree(self._dir(owner), ignore_errors=True)
        if self.mode != "files":
            self.docs.delete("style", owner)

    def stale_analyzing(self) -> list[tuple[str, StyleRecord]]:
        """Records left ``analyzing`` by a restart: nothing is measuring them any more."""
        out = []
        owners = [o for o in self.docs.owners("style") if OWNER.match(o)] if self.mode == "db" else \
            [d.name for d in self.root.iterdir() if OWNER.match(d.name)] if self.root.is_dir() else []
        for o in owners:
            out += [(o, r) for r in self.list(o) if r.status == "analyzing"]
        return out
