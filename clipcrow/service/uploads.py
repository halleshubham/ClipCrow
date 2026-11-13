"""Direct uploads in flight: one small JSON record per upload, ``<data>/uploads/<id>.json``.

The id of an upload IS the id of the job it will become (``jobs.JOB_ID``), so the file lands at
``<prefix>/jobs/<id>/source.<ext>`` in the bucket and everything that already works on that prefix (mirroring, retention,
purge on delete) works on it. A record only says what was announced and what S3 multipart upload carries it."""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict

from .jobs import JOB_ID, now

OPEN_LIMIT = 3                      # unfinished uploads one person (or one server without accounts) may have
EXPIRE_HOURS = 24


class Upload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    owner: Optional[str] = None
    filename: str
    suffix: str                     # ".mp4", checked against VIDEO_SUFFIXES
    size: int                       # bytes announced; the finished object must be exactly this
    part_size: int
    parts: int
    s3_upload_id: str
    created_at: str

    @property
    def rel(self) -> str:
        return f"source{self.suffix}"


class UploadStore:
    def __init__(self, root: str | Path):
        self.root = Path(root) / "uploads"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, upload_id: str) -> Path:
        if not JOB_ID.match(upload_id):
            raise KeyError(upload_id)
        return self.root / f"{upload_id}.json"

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:12]

    def save(self, u: Upload) -> None:
        with self._lock:
            tmp = self._path(u.id).with_suffix(".tmp")
            tmp.write_text(u.model_dump_json(), encoding="utf-8")
            os.replace(tmp, self._path(u.id))

    def get(self, upload_id: str) -> Upload:
        try:
            return Upload.model_validate(json.loads(self._path(upload_id).read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError) as e:
            raise KeyError(upload_id) from e

    def delete(self, upload_id: str) -> None:
        try:
            self._path(upload_id).unlink(missing_ok=True)
        except KeyError:
            pass

    def list(self) -> list[Upload]:
        out = []
        for p in self.root.glob("*.json"):
            try:
                out.append(self.get(p.stem))
            except KeyError:
                continue
        return out

    def open_for(self, owner: Optional[str]) -> int:
        return sum(1 for u in self.list() if u.owner == owner)

    def expired(self, *, hours: int = EXPIRE_HOURS, at: Optional[datetime] = None) -> list[Upload]:
        cutoff = (at or datetime.now(timezone.utc)) - timedelta(hours=hours)
        return [u for u in self.list() if datetime.fromisoformat(u.created_at) < cutoff]


def new_upload(store: UploadStore, **kw) -> Upload:
    return Upload(created_at=now(), **kw)
