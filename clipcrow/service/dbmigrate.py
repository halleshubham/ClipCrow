"""Copy an existing SQLite accounts file into the configured database (PostgreSQL): ``clipcrow migrate-db``.

Safe to run twice (rows already there are left alone) and read-only on the source. What is copied: people, their still-valid sessions (so nobody is signed out), the
usage ledger (ids kept, so refunds still find their job), webhook ids already handled (so Razorpay events are not applied twice) and SocioBird connections (the
stored values are already encrypted and stay readable ONLY with the same server secret: copy ``clipcrow.secret`` too). Not copied: pending sign-in codes and OAuth
states (they live ten minutes) and old password hashes (there are no passwords any more). The report has counts only: no address, no key.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .accounts import Accounts

TABLES = ("users", "sessions", "usage", "webhook_events", "sociobird_links")
KEYS = {"users": "id", "sessions": "token_hash", "usage": "id", "webhook_events": "id", "sociobird_links": "user_id"}


def migrate(sqlite_path: str | Path, target: Accounts) -> dict:
    """Copy into ``target`` (an ``Accounts`` on the new database). Returns ``{table: rows copied, ...}`` and checks the counts and the total of purchased minutes."""
    src = sqlite3.connect(f"file:{Path(sqlite_path).resolve()}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    report: dict = {}
    try:
        have = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in TABLES:
            if table not in have:
                report[table] = 0
                continue
            cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
            common = [c for c in cols if c in target.db.columns(table)]       # a column the new schema does not have (pw_hash) is left behind
            rows = src.execute(f"SELECT {', '.join(common)} FROM {table}" + (f" WHERE expires_at > {time.time()}" if table == "sessions" else "")).fetchall()
            sql = f"INSERT INTO {table}({', '.join(common)}) VALUES ({', '.join('?' * len(common))}) ON CONFLICT({KEYS[table]}) DO NOTHING"
            copied = 0
            with target.db.tx() as t:
                for r in rows:
                    copied += t.q(sql, tuple(r[c] for c in common)).rowcount
            report[table] = copied
        if target.postgres:                                                   # the next usage id must follow the copied ones
            target._q("SELECT setval(pg_get_serial_sequence('usage', 'id'), COALESCE((SELECT MAX(id) FROM usage), 1), (SELECT COUNT(*) > 0 FROM usage))")
        want = src.execute("SELECT COUNT(*), COALESCE(SUM(credits), 0) FROM users").fetchone() if "users" in have else (0, 0)
    finally:
        src.close()
    got = target._q("SELECT COUNT(*) AS n, COALESCE(SUM(credits), 0) AS c FROM users").fetchone()
    fresh = report["users"] == want[0]                                        # the target was empty: every figure must match exactly
    if got["n"] < want[0] or (fresh and abs(float(got["c"]) - float(want[1])) > 1e-6):
        raise RuntimeError("the copy does not match the source (people or purchased minutes differ); the source was not changed")
    report["people_in_target"] = int(got["n"])
    return report
