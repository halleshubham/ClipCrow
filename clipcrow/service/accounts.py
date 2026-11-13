"""Users, sessions and the usage ledger, in one SQLite file (``<data>/clipcrow.db``). Standard library only.

Passwords: scrypt with a per-user salt, compared in constant time; never logged. Sessions: a random 256-bit token in an
HttpOnly SameSite=Strict cookie; only its SHA-256 is stored, so a leaked database does not hand out sessions. Sign-in
attempts are rate limited per address and per account. Nothing here sends email: there is no password reset or address
verification yet (see README), so an operator resets a password with ``Accounts.set_password``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,24}\Z")
MIN_PASSWORD = 10
SESSION_DAYS = 30
_SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32)


class AccountError(ValueError):
    """A message written for a person (bad email, weak password, wrong credentials, ...)."""


@dataclass
class User:
    id: str
    email: str
    plan: str
    credits: float                 # purchased minutes
    plan_until: Optional[str]
    subscription: Optional[str]
    created_at: str


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${base64.b64encode(salt).decode()}${base64.b64encode(h).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, want = stored.split("$")
        if scheme != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(got, base64.b64decode(want))
    except (ValueError, TypeError):
        return False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def month_key(when: datetime | None = None) -> str:
    return (when or datetime.now(timezone.utc)).strftime("%Y-%m")


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL, created_at TEXT NOT NULL,
  plan TEXT NOT NULL DEFAULT 'free', plan_until TEXT, subscription TEXT, credits REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  job_id TEXT, minutes REAL NOT NULL, credit_minutes REAL NOT NULL DEFAULT 0, month TEXT NOT NULL,
  kind TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS usage_user_month ON usage(user_id, month);
CREATE TABLE IF NOT EXISTS webhook_events (id TEXT PRIMARY KEY, received_at TEXT NOT NULL);
"""


class Accounts:
    def __init__(self, path: str | Path, *, max_failures: int = 5, window_s: int = 900):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(SCHEMA)
        self._fails: dict[str, list[float]] = {}
        self.max_failures, self.window_s = max_failures, window_s

    # --- plumbing -------------------------------------------------------------------------------
    def _q(self, sql: str, args: tuple = ()):
        with self._lock:
            return self._db.execute(sql, args)

    def transaction(self):
        return _Tx(self)

    @staticmethod
    def _user(row) -> User:
        return User(row["id"], row["email"], row["plan"], row["credits"], row["plan_until"], row["subscription"], row["created_at"])

    # --- users ----------------------------------------------------------------------------------
    def register(self, email: str, password: str) -> User:
        email = (email or "").strip().lower()
        if not EMAIL.match(email):
            raise AccountError("Enter a valid email address.")
        if len(password or "") < MIN_PASSWORD:
            raise AccountError(f"Use a password of at least {MIN_PASSWORD} characters.")
        if len(password) > 200:
            raise AccountError("That password is too long.")
        uid = uuid.uuid4().hex
        try:
            self._q("INSERT INTO users(id, email, pw_hash, created_at) VALUES (?,?,?,?)", (uid, email, hash_password(password), _now()))
        except sqlite3.IntegrityError:
            raise AccountError("An account with that email already exists.") from None
        return self.get(uid)

    def get(self, user_id: str) -> User:
        row = self._q("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise KeyError(user_id)
        return self._user(row)

    def by_subscription(self, sub_id: str) -> Optional[User]:
        row = self._q("SELECT * FROM users WHERE subscription=?", (sub_id,)).fetchone()
        return self._user(row) if row else None

    def authenticate(self, email: str, password: str, *, who: str = "") -> User:
        """Check credentials, with a limit on failures per (address, account) and per account."""
        email = (email or "").strip().lower()
        keys = [f"a:{who}:{email}", f"e:{email}"]
        now = time.time()
        for k in keys:
            self._fails[k] = [t for t in self._fails.get(k, []) if now - t < self.window_s]
        if any(len(self._fails[k]) >= self.max_failures * (3 if k.startswith("e:") else 1) for k in keys):
            raise AccountError("Too many sign-in attempts. Wait a few minutes and try again.")
        row = self._q("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        # verify against a dummy hash for unknown emails so the response time does not reveal which emails exist
        ok = verify_password(password or "", row["pw_hash"] if row else _DUMMY)
        if not (row and ok):
            for k in keys:
                self._fails[k].append(now)
            raise AccountError("That email and password do not match.")
        for k in keys:
            self._fails.pop(k, None)
        return self._user(row)

    def set_password(self, user_id: str, password: str) -> None:
        if len(password or "") < MIN_PASSWORD:
            raise AccountError(f"Use a password of at least {MIN_PASSWORD} characters.")
        self._q("UPDATE users SET pw_hash=? WHERE id=?", (hash_password(password), user_id))
        self._q("DELETE FROM sessions WHERE user_id=?", (user_id,))        # a new password signs everyone out

    def delete(self, user_id: str) -> None:
        self._q("DELETE FROM users WHERE id=?", (user_id,))                # sessions, usage and posts go with it (cascade)

    # --- sessions -------------------------------------------------------------------------------
    def new_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        self._q("INSERT INTO sessions VALUES (?,?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), user_id, _now(),
                                                          time.time() + SESSION_DAYS * 86400))
        self._q("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
        return token

    def user_for_session(self, token: str) -> Optional[User]:
        if not token:
            return None
        row = self._q("SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash=? AND s.expires_at>?",
                      (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        return self._user(row) if row else None

    def end_session(self, token: str) -> None:
        self._q("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256((token or "").encode()).hexdigest(),))

    # --- plan and credits -----------------------------------------------------------------------
    def set_plan(self, user_id: str, plan: str, *, until: Optional[str] = None, subscription: Optional[str] = None) -> None:
        self._q("UPDATE users SET plan=?, plan_until=?, subscription=COALESCE(?, subscription) WHERE id=?",
                (plan, until, subscription, user_id))

    def clear_subscription(self, user_id: str) -> None:
        self._q("UPDATE users SET subscription=NULL WHERE id=?", (user_id,))

    def add_credits(self, user_id: str, minutes: float) -> None:
        self._q("UPDATE users SET credits = credits + ? WHERE id=?", (minutes, user_id))

    # --- usage ----------------------------------------------------------------------------------
    def used_this_month(self, user_id: str, month: str | None = None) -> float:
        row = self._q("SELECT COALESCE(SUM(minutes - credit_minutes), 0) AS m FROM usage WHERE user_id=? AND month=?",
                      (user_id, month or month_key())).fetchone()
        return float(row["m"])

    def reserve(self, user_id: str, job_id: str, minutes: float, allowance: float) -> Optional[float]:
        """Take ``minutes`` from this month's allowance, then from purchased credits. Atomic. Returns the minutes
        left afterwards, or None when there are not enough (nothing is taken)."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                user = self._db.execute("SELECT credits FROM users WHERE id=?", (user_id,)).fetchone()
                used = float(self._db.execute("SELECT COALESCE(SUM(minutes - credit_minutes), 0) AS m FROM usage WHERE user_id=? AND month=?",
                                              (user_id, month_key())).fetchone()["m"])
                left = max(allowance - used, 0.0)
                from_credits = max(minutes - left, 0.0)
                if user is None or from_credits > user["credits"] + 1e-9:
                    self._db.execute("ROLLBACK")
                    return None
                self._db.execute("UPDATE users SET credits = credits - ? WHERE id=?", (from_credits, user_id))
                self._db.execute("INSERT INTO usage(user_id, job_id, minutes, credit_minutes, month, kind, created_at) VALUES (?,?,?,?,?,?,?)",
                                 (user_id, job_id, minutes, from_credits, month_key(), "job", _now()))
                self._db.execute("COMMIT")
                return max(allowance - used - minutes, 0.0) + user["credits"] - from_credits
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def refund(self, user_id: str, job_id: str) -> bool:
        """Give back what a job reserved (it failed or was cancelled). Once per job."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                rows = self._db.execute("SELECT kind, SUM(minutes) AS m, SUM(credit_minutes) AS c, MAX(month) AS mo FROM usage "
                                        "WHERE user_id=? AND job_id=? GROUP BY kind", (user_id, job_id)).fetchall()
                took = next((r for r in rows if r["kind"] == "job"), None)
                if took is None or any(r["kind"] == "refund" for r in rows):
                    self._db.execute("ROLLBACK")
                    return False
                self._db.execute("UPDATE users SET credits = credits + ? WHERE id=?", (took["c"], user_id))
                self._db.execute("INSERT INTO usage(user_id, job_id, minutes, credit_minutes, month, kind, created_at) VALUES (?,?,?,?,?,?,?)",
                                 (user_id, job_id, -took["m"], -took["c"], took["mo"], "refund", _now()))
                self._db.execute("COMMIT")
                return True
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    # --- webhooks (idempotency) -----------------------------------------------------------------
    def first_time(self, event_id: str) -> bool:
        try:
            self._q("INSERT INTO webhook_events VALUES (?,?)", (event_id, _now()))
            return True
        except sqlite3.IntegrityError:
            return False

    def count(self) -> int:
        return int(self._q("SELECT COUNT(*) AS n FROM users").fetchone()["n"])


class _Tx:
    def __init__(self, a: Accounts):
        self.a = a

    def __enter__(self):
        self.a._lock.acquire()
        return self.a

    def __exit__(self, *exc):
        self.a._lock.release()


_DUMMY = hash_password("not-a-real-password", b"\x00" * 16)
