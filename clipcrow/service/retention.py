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

log = logging.getLogger("clipcrow.service")


def _age_days(job: Job, now: datetime) -> float:
    return (now - datetime.fromisoformat(job.created_at)).total_seconds() / 86400


def sweep(store: JobStore, days_for: Callable[[Job], tuple[int, int]], *, now: Optional[datetime] = None) -> dict:
    """``days_for(job) -> (retention_days, source_days)``; 0 keeps. Returns what was removed."""
    now = now or datetime.now(timezone.utc)
    out = {"jobs": 0, "sources": 0}
    for job in store.list():
        if job.status in ("queued", "running"):
            continue
        keep_days, source_days = days_for(job)
        age = _age_days(job, now)
        if keep_days and age >= keep_days:
            store.delete(job.id)
            out["jobs"] += 1
            continue
        if source_days and age >= source_days and not job.source_removed:
            src = store.source_path(job.id)
            if src is not None:
                src.unlink(missing_ok=True)
            store.update(job.id, lambda j: setattr(j, "source_removed", True))
            out["sources"] += 1
    return out


def expires_at(job: Job, retention_days: int) -> Optional[str]:
    if not retention_days:
        return None
    return (datetime.fromisoformat(job.created_at) + timedelta(days=retention_days)).isoformat(timespec="seconds")


class Sweeper:
    """Runs ``sweep`` now and then in a background thread."""

    def __init__(self, store: JobStore, days_for, every_s: float = 3600):
        self.store, self.days_for, self.every_s = store, days_for, every_s
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, name="clipcrow-retention", daemon=True)

    def start(self) -> None:
        self._t.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                r = sweep(self.store, self.days_for)
                if r["jobs"] or r["sources"]:
                    log.info("retention: removed %s job(s) and %s original video(s)", r["jobs"], r["sources"])
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
