"""Job records as rows: ``jobs(id, owner, status, created_at, version, doc)``. The columns are what is asked every request (who owns it, is it running, newest first);
``doc`` is the job's own JSON as ``Job.model_dump_json`` writes it, so the model stays the source of truth. Same SQL on SQLite and PostgreSQL (``db.py``).

``put`` with ``expect_version`` is the optimistic write that replaces the process lock: a job changed by someone else in between raises ``Conflict`` and the caller
loads it again and re-applies its change (``JobStore.update`` does exactly that).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from .db import IntegrityError
from .docstore import Conflict

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, owner TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, doc TEXT NOT NULL, "
    "updated_at TEXT NOT NULL DEFAULT '')",
    "CREATE INDEX IF NOT EXISTS jobs_owner ON jobs (owner, created_at)",
    "CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status)",
]


class JobRows:
    def __init__(self, db: Any):
        self.db = db
        db.script(SCHEMA)

    def get(self, job_id: str) -> Optional[tuple[str, int]]:
        r = self.db.q("SELECT doc, version FROM jobs WHERE id=?", (job_id,)).fetchone()
        return (r["doc"], int(r["version"])) if r else None

    def exists(self, job_id: str) -> bool:
        return self.db.q("SELECT 1 AS x FROM jobs WHERE id=?", (job_id,)).fetchone() is not None

    def put(self, job_id: str, owner: Optional[str], status: str, created_at: str, text: str, *, expect_version: Optional[int] = None) -> int:
        """Write a job and return its new version. ``expect_version`` 0 means "must not exist yet"; a mismatch raises ``Conflict``."""
        stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        with self.db.tx() as t:
            row = t.q("SELECT version FROM jobs WHERE id=?" + self.db.for_update, (job_id,)).fetchone()
            have = int(row["version"]) if row else 0
            if expect_version is not None and expect_version != have:
                raise Conflict(f"job {job_id} changed")
            if row:
                t.q("UPDATE jobs SET owner=?, status=?, created_at=?, doc=?, version=?, updated_at=? WHERE id=?", (owner, status, created_at, text, have + 1, stamp, job_id))
            else:
                try:
                    t.q("INSERT INTO jobs (id, owner, status, created_at, version, doc, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?)", (job_id, owner, status, created_at, text, stamp))
                except IntegrityError:
                    raise Conflict(f"job {job_id} changed") from None
            return have + 1

    def delete(self, job_id: str) -> None:
        self.db.q("DELETE FROM jobs WHERE id=?", (job_id,))

    def briefs(self) -> list[tuple[str, Optional[str], str, str]]:
        """``(id, owner, status, created_at)`` newest first: one query, no parsing."""
        return [(r["id"], r["owner"], r["status"], r["created_at"])
                for r in self.db.q("SELECT id, owner, status, created_at FROM jobs ORDER BY created_at DESC, id DESC").fetchall()]

    def texts(self) -> list[str]:
        return [r["doc"] for r in self.db.q("SELECT doc FROM jobs ORDER BY created_at DESC, id DESC").fetchall()]

    def ids(self) -> list[str]:
        return [r["id"] for r in self.db.q("SELECT id FROM jobs ORDER BY id").fetchall()]

    def count(self) -> int:
        return int(self.db.q("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"])
