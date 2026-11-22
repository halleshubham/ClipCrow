"""Small records in the database instead of files: one table, one place, the same SQL on SQLite and PostgreSQL (``db.py``).

A record is ``(kind, owner, key) -> JSON text`` with a ``version`` that goes up on every write. The text is what the store's own pydantic model writes
(``model_dump_json``), so the models stay the source of truth and a record read back is validated exactly as a file was. Media never goes here: only records.

Each store decides where its records live with ``CLIPCROW_STORE_<NAME>`` (or ``CLIPCROW_STORE`` for all of them):

* ``files``: the files, as before (the default, so deploying this code changes nothing),
* ``both``: written to the files AND the database, read from the files (a trial period: ``clipcrow migrate-db --stores`` and ``--verify`` compare the two),
* ``db``: the database only; the files are left untouched, so going back is changing the word.

Only a server with accounts has a database to share; a single-user install stays on files.
"""
from __future__ import annotations

import os
import random
import re
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from .db import IntegrityError

MODES = ("files", "both", "db")
NAME = re.compile(r"[a-z][a-z_]{0,30}\Z")


class Conflict(Exception):
    """The record was changed by someone else between reading it and writing it back."""


def store_mode(name: str, env: Optional[Mapping[str, str]] = None, *, has_db: bool = True) -> str:
    """Where this store keeps its records: ``CLIPCROW_STORE_<NAME>`` first, then ``CLIPCROW_STORE``, else ``files``. Without a database it is always ``files``."""
    env = os.environ if env is None else env
    if not NAME.match(name):
        raise ValueError("bad store name")
    raw = (env.get(f"CLIPCROW_STORE_{name.upper()}") or env.get("CLIPCROW_STORE") or "files").strip().lower()
    if raw not in MODES:
        raise ValueError(f"CLIPCROW_STORE_{name.upper()} must be one of: {', '.join(MODES)}")
    return raw if has_db else "files"


def schema_statements() -> list[str]:
    return ["CREATE TABLE IF NOT EXISTS docs (kind TEXT NOT NULL, owner TEXT NOT NULL, key TEXT NOT NULL, doc TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, "
            "updated_at TEXT NOT NULL, PRIMARY KEY (kind, owner, key))"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DocStore:
    def __init__(self, db: Any):
        self.db = db
        db.script(schema_statements())

    def get(self, kind: str, owner: str, key: str) -> Optional[tuple[str, int]]:
        """``(text, version)`` or None."""
        r = self.db.q("SELECT doc, version FROM docs WHERE kind=? AND owner=? AND key=?", (kind, owner, key)).fetchone()
        return (r["doc"], int(r["version"])) if r else None

    def text(self, kind: str, owner: str, key: str) -> Optional[str]:
        got = self.get(kind, owner, key)
        return got[0] if got else None

    def put(self, kind: str, owner: str, key: str, text: str, *, expect_version: Optional[int] = None) -> int:
        """Write a record and return its new version. With ``expect_version`` (0 = "must not exist yet") a record changed in between raises ``Conflict``."""
        with self.db.tx() as t:
            row = t.q("SELECT version FROM docs WHERE kind=? AND owner=? AND key=?" + self.db.for_update, (kind, owner, key)).fetchone()
            have = int(row["version"]) if row else 0
            if expect_version is not None and expect_version != have:
                raise Conflict(f"{kind}/{key} changed")
            if row:
                t.q("UPDATE docs SET doc=?, version=?, updated_at=? WHERE kind=? AND owner=? AND key=?", (text, have + 1, _now(), kind, owner, key))
            else:
                try:
                    t.q("INSERT INTO docs (kind, owner, key, doc, version, updated_at) VALUES (?, ?, ?, ?, 1, ?)", (kind, owner, key, text, _now()))
                except IntegrityError:                           # two first writes at once: the other one won
                    raise Conflict(f"{kind}/{key} changed") from None
            return have + 1

    def delete(self, kind: str, owner: str, key: Optional[str] = None) -> None:
        """One record, or (no ``key``) every record of this kind for the owner."""
        if key is None:
            self.db.q("DELETE FROM docs WHERE kind=? AND owner=?", (kind, owner))
        else:
            self.db.q("DELETE FROM docs WHERE kind=? AND owner=? AND key=?", (kind, owner, key))

    def items(self, kind: str, owner: str) -> list[tuple[str, str]]:
        """``(key, text)`` of every record of this kind for the owner, by key."""
        return [(r["key"], r["doc"]) for r in self.db.q("SELECT key, doc FROM docs WHERE kind=? AND owner=? ORDER BY key", (kind, owner)).fetchall()]

    def owners(self, kind: str) -> list[str]:
        return [r["owner"] for r in self.db.q("SELECT DISTINCT owner FROM docs WHERE kind=? ORDER BY owner", (kind,)).fetchall()]

    def count(self, kind: str) -> int:
        return int(self.db.q("SELECT COUNT(*) AS n FROM docs WHERE kind=?", (kind,)).fetchone()["n"])

    def update(self, kind: str, owner: str, key: str, fn, *, retries: int = 60) -> str:
        """Read-modify-write without a process lock: ``fn(old_text_or_None) -> new_text``, retried when someone else wrote in between."""
        for attempt in range(retries):
            if attempt:
                time.sleep(random.random() * min(0.002 * attempt, 0.05))      # writers that just collided do not collide again in step
            got = self.get(kind, owner, key)
            new = fn(got[0] if got else None)
            try:
                self.put(kind, owner, key, new, expect_version=got[1] if got else 0)
                return new
            except Conflict:
                continue
        raise Conflict(f"{kind}/{key} is being changed too often")
