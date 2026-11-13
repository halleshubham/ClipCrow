"""Deleting old work. Finished jobs expire after their owner's plan's ``retention_days``; the (large) uploaded original
goes sooner, after ``source_days``. A job that is queued or running is never touched. Without accounts, set
``CLIPCROW_RETENTION_DAYS`` / ``CLIPCROW_SOURCE_DAYS`` (0 = keep for ever)."""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from .jobs import Job, JobStore
from .storage import StorageError, signature

log = logging.getLogger("clipcrow.service")


def _age_days(job: Job, now: datetime) -> float:
    return (now - datetime.fromisoformat(job.created_at)).total_seconds() / 86400


def sweep(store: JobStore, days_for: Callable[[Job], tuple[int, int]], *, now: Optional[datetime] = None,
          storage=None, local_clip_days: int = 0) -> dict:
    """``days_for(job) -> (retention_days, source_days)``; 0 keeps. With object storage the bucket copies are removed
    first (and when that fails nothing local is deleted, so the next pass retries); ``local_clip_days`` frees disk by
    removing a finished clip's local file once an identical copy is in the bucket. Returns what was removed."""
    now = now or datetime.now(timezone.utc)
    out = {"jobs": 0, "sources": 0, "evicted": 0}
    for job in store.list():
        if job.status in ("queued", "running"):
            continue
        keep_days, source_days = days_for(job)
        age = _age_days(job, now)
        if keep_days and age >= keep_days:
            if storage:
                try:
                    storage.delete_job(job.id)
                except StorageError:
                    log.warning("retention: the bucket copies of %s could not be removed; will retry", job.id)
                    continue
            store.delete(job.id)
            out["jobs"] += 1
            continue
        if source_days and age >= source_days and not job.source_removed:
            remote_src = [r for r in job.remote if r.startswith("source.")]
            if storage and remote_src:
                try:
                    for rel in remote_src:
                        storage.delete(job.id, rel)
                except StorageError:
                    log.warning("retention: the stored original of %s could not be removed; will retry", job.id)
                    continue
            src = store.source_path(job.id)
            if src is not None:
                src.unlink(missing_ok=True)

            def mark(j, gone=remote_src):
                j.source_removed = True
                for r in gone:
                    j.remote.pop(r, None)
            store.update(job.id, mark)
            out["sources"] += 1
        if storage and local_clip_days and age >= local_clip_days:
            for rel, sig in job.remote.items():
                p = store.dir(job.id) / rel
                if rel.startswith("out/c") and rel.endswith(".mp4") and p.is_file() and signature(p) == sig:
                    p.unlink()
                    out["evicted"] += 1
    return out


def expires_at(job: Job, retention_days: int) -> Optional[str]:
    if not retention_days:
        return None
    return (datetime.fromisoformat(job.created_at) + timedelta(days=retention_days)).isoformat(timespec="seconds")


class Sweeper:
    """Runs ``sweep`` now and then in a background thread."""

    def __init__(self, store: JobStore, days_for, every_s: float = 3600, *, storage=None, mirror=None, local_clip_days: int = 0,
                 extra=()):
        self.store, self.days_for, self.every_s = store, days_for, every_s
        self.storage, self.mirror, self.local_clip_days = storage, mirror, local_clip_days
        self.extra = list(extra)                               # other periodic chores (e.g. expiring abandoned uploads)
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, name="clipcrow-retention", daemon=True)

    def start(self) -> None:
        self._t.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                for chore in self.extra:
                    chore()
                if self.mirror:
                    self.mirror.reconcile()                       # anything not in the bucket yet (or changed) goes up
                r = sweep(self.store, self.days_for, storage=self.storage, local_clip_days=self.local_clip_days)
                if r["jobs"] or r["sources"] or r["evicted"]:
                    log.info("retention: removed %s job(s), %s original video(s), freed %s local clip(s)", r["jobs"], r["sources"], r["evicted"])
            except Exception:                                  # a bad job directory must not stop the sweeper
                log.exception("retention sweep failed")
            self._stop.wait(self.every_s)


def env_days() -> tuple[int, int]:
    def num(name: str) -> int:
        try:
            return max(0, int(os.environ.get(name, "0")))
        except ValueError:
            return 0
    return num("CLIPCROW_RETENTION_DAYS"), num("CLIPCROW_SOURCE_DAYS")
