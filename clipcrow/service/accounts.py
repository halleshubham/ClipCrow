"""Users, sessions and the usage ledger, in one SQLite file (``<data>/clipcrow.db``). Standard library only.

Sign-in is by an emailed one-time code only (docs/email-task.md): there are no passwords. A code is 6 digits from ``secrets``, valid 10 minutes,
single use, dead after 5 wrong tries, and stored only as an HMAC (keyed with a server secret kept next to the database), so a copied table is
useless. Asking for a code and signing up are the same step: an address that proves it can read the mail gets an account. Sessions: a random
256-bit token in an HttpOnly SameSite=Strict cookie; only its SHA-256 is stored. Requests for codes and wrong guesses are rate limited per
address and per IP; the answer never says whether an address has an account.
"""
from __future__ import annotations

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
SESSION_DAYS = 30
CODE_MINUTES = 10                  # a code is valid this long
CODE_TRIES = 5                     # wrong guesses before the code is dead
CODE_WAIT_S = 60                   # between two mails to one address
CODE_PER_HOUR, CODE_PER_DAY = 5, 15


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
    notify_job_done: bool = False
    disabled: bool = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def month_key(when: datetime | None = None) -> str:
    return (when or datetime.now(timezone.utc)).strftime("%Y-%m")


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
  plan TEXT NOT NULL DEFAULT 'free', plan_until TEXT, subscription TEXT, credits REAL NOT NULL DEFAULT 0,
  notify_job_done INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS login_codes (
  email TEXT PRIMARY KEY, code_hash TEXT NOT NULL, expires_at REAL NOT NULL, tries INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS login_sends (email TEXT NOT NULL, at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS login_sends_email ON login_sends(email, at);
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
    def __init__(self, path: str | Path, *, max_failures: int = 5, window_s: int = 900, secret: bytes | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(SCHEMA)
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(users)")}
            if "notify_job_done" not in cols:                                       # an older database
                self._db.execute("ALTER TABLE users ADD COLUMN notify_job_done INTEGER NOT NULL DEFAULT 0")
            if "disabled" not in cols:
                self._db.execute("ALTER TABLE users ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0")
        self._secret = secret or self._load_secret()
        self._fails: dict[str, list[float]] = {}
        self.max_failures, self.window_s = max_failures, window_s

    def _load_secret(self) -> bytes:
        """The key that signs login codes: CLIPCROW_SECRET_KEY, else a random one kept next to the database (mode 600), made once."""
        import os
        env = os.environ.get("CLIPCROW_SECRET_KEY", "").strip()
        if env:
            return hashlib.sha256(env.encode()).digest()
        f = self.path.with_suffix(".secret")
        if f.is_file():
            return bytes.fromhex(f.read_text().strip())
        key = secrets.token_bytes(32)
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as h:
            h.write(key.hex())
        return key

    # --- plumbing -------------------------------------------------------------------------------
    def _q(self, sql: str, args: tuple = ()):
        with self._lock:
            return self._db.execute(sql, args)

    def transaction(self):
        return _Tx(self)

    @staticmethod
    def _user(row) -> User:
        return User(row["id"], row["email"], row["plan"], row["credits"], row["plan_until"], row["subscription"], row["created_at"], bool(row["notify_job_done"]), bool(row["disabled"]))

    # --- users ----------------------------------------------------------------------------------
    def ensure_user(self, email: str) -> User:
        """The account for this address, made when it does not exist yet (used once the address has proved itself with a code, and by tests)."""
        email = (email or "").strip().lower()
        if not EMAIL.match(email):
            raise AccountError("Enter a valid email address.")
        row = self._q("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if row:
            return self._user(row)
        uid = uuid.uuid4().hex
        try:
            self._q("INSERT INTO users(id, email, created_at) VALUES (?,?,?)", (uid, email, _now()))
        except sqlite3.IntegrityError:
            pass                                                           # made a moment ago by a parallel request
        return self._user(self._q("SELECT * FROM users WHERE email=?", (email,)).fetchone())

    def by_email(self, email: str) -> Optional[User]:
        row = self._q("SELECT * FROM users WHERE email=?", ((email or "").strip().lower(),)).fetchone()
        return self._user(row) if row else None

    def signups_today(self) -> int:
        return int(self._q("SELECT COUNT(*) AS n FROM users WHERE created_at >= ?", (datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00"),)).fetchone()["n"])

    def set_disabled(self, user_id: str, disabled: bool) -> None:
        self._q("UPDATE users SET disabled=? WHERE id=?", (1 if disabled else 0, user_id))
        if disabled:
            self._q("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def list_users(self) -> list[dict]:
        """For the operator's page: one row per account with this month's use and its job count. No secrets are in the table."""
        rows = self._q("SELECT u.*, (SELECT COALESCE(SUM(minutes - credit_minutes), 0) FROM usage x WHERE x.user_id=u.id AND x.month=?) AS used, "
                       "(SELECT COUNT(*) FROM usage x WHERE x.user_id=u.id AND x.kind='job') AS jobs FROM users u ORDER BY u.created_at DESC", (month_key(),)).fetchall()
        return [{"id": r["id"], "email": r["email"], "plan": r["plan"], "credits": r["credits"], "used_minutes": round(float(r["used"]), 1), "jobs": r["jobs"],
                 "created_at": r["created_at"], "disabled": bool(r["disabled"]), "subscribed": bool(r["subscription"])} for r in rows]

    def set_notify(self, user_id: str, job_done: bool) -> None:
        self._q("UPDATE users SET notify_job_done=? WHERE id=?", (1 if job_done else 0, user_id))

    def get(self, user_id: str) -> User:
        row = self._q("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise KeyError(user_id)
        return self._user(row)

    def by_subscription(self, sub_id: str) -> Optional[User]:
        row = self._q("SELECT * FROM users WHERE subscription=?", (sub_id,)).fetchone()
        return self._user(row) if row else None

    # --- sign-in by emailed code -----------------------------------------------------------------
    def _mac(self, email: str, code: str) -> str:
        return hmac.new(self._secret, f"{email}\x00{code}".encode(), hashlib.sha256).hexdigest()

    def _throttle(self, key: str, limit: int, window: float) -> None:
        now = time.time()
        hits = [t for t in self._fails.get(key, []) if now - t < window]
        if len(hits) >= limit:
            self._fails[key] = hits
            raise AccountError("Too many attempts. Wait a few minutes and try again.")
        hits.append(now)
        self._fails[key] = hits

    def request_code(self, email: str, *, who: str = "", allow_signup: bool = True) -> Optional[str]:
        """Start a sign-in. Returns the code to mail, or None when nothing should be mailed (no account and sign-up is closed, or the address was
        asked too recently); the caller answers the same way in every case. Raises AccountError only for a bad address or a flood."""
        email = (email or "").strip().lower()
        if not EMAIL.match(email):
            raise AccountError("Enter a valid email address.")
        self._throttle(f"ip:{who}", 20, 3600)                              # per IP: a flood of requests
        self._throttle("all", 600, 3600)                                   # global brake: cannot spend the mail quota or get the domain flagged
        now = time.time()
        with self._lock:
            self._db.execute("DELETE FROM login_sends WHERE at < ?", (now - 86400,))
            sends = [r["at"] for r in self._db.execute("SELECT at FROM login_sends WHERE email=?", (email,)).fetchall()]
            found = self._db.execute("SELECT disabled FROM users WHERE email=?", (email,)).fetchone()
            exists = found is not None
            code = f"{secrets.randbelow(10 ** 6):06d}"
            mac = self._mac(email, code)                                   # the same work whether or not a mail will go out
            if (not exists and not allow_signup) or (exists and found["disabled"]) or any(now - t < CODE_WAIT_S for t in sends) \
                    or sum(1 for t in sends if now - t < 3600) >= CODE_PER_HOUR or len(sends) >= CODE_PER_DAY:
                return None
            self._db.execute("INSERT OR REPLACE INTO login_codes(email, code_hash, expires_at, tries, created_at) VALUES (?,?,?,0,?)",
                             (email, mac, now + CODE_MINUTES * 60, now))
            self._db.execute("INSERT INTO login_sends(email, at) VALUES (?,?)", (email, now))
        return code

    def verify_code(self, email: str, code: str, *, who: str = "", allow_signup: bool = True) -> User:
        """Check the code; on success the code is used up and the account exists (made now when new). Every failure says the same thing."""
        email = (email or "").strip().lower()
        wrong = AccountError("That code is not right or has expired. Ask for a new one.")
        self._throttle(f"vip:{who}", 60, 900)
        code = re.sub(r"\s+", "", code or "")
        if not EMAIL.match(email) or not re.fullmatch(r"\d{6}", code):
            raise wrong
        with self._lock:
            row = self._db.execute("SELECT * FROM login_codes WHERE email=?", (email,)).fetchone()
            good = bool(row) and row["expires_at"] > time.time() and row["tries"] < CODE_TRIES
            ok = hmac.compare_digest(self._mac(email, code), row["code_hash"] if row else self._mac(email, "000000")) and good
            if not ok:
                if row:
                    if row["tries"] + 1 >= CODE_TRIES:
                        self._db.execute("DELETE FROM login_codes WHERE email=?", (email,))     # too many wrong tries: the code is dead
                    else:
                        self._db.execute("UPDATE login_codes SET tries=tries+1 WHERE email=?", (email,))
                raise wrong
            self._db.execute("DELETE FROM login_codes WHERE email=?", (email,))                 # single use
        existing = self.by_email(email)
        if (existing is None and not allow_signup) or (existing is not None and existing.disabled):
            raise wrong
        return existing or self.ensure_user(email)

    def sign_out_everywhere(self, user_id: str) -> None:
        self._q("DELETE FROM sessions WHERE user_id=?", (user_id,))

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
        row = self._q("SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.disabled=0",
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

    def backup(self, folder: str | Path, keep: int = 7) -> Optional[Path]:
        """A consistent copy of the database (SQLite's online backup, safe while it is in use), once a day, the newest ``keep`` kept. Returns the file
        made, or None when today's copy already exists."""
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"clipcrow-{datetime.now(timezone.utc).strftime('%Y%m%d')}.db"
        made = None
        if not dest.exists():
            made = self._copy_to(dest)
        for old in sorted(folder.glob("clipcrow-*.db"))[:-keep]:
            old.unlink(missing_ok=True)
        return made

    def _copy_to(self, dest: Path) -> Path:
        tmp = dest.with_suffix(".tmp")
        out = sqlite3.connect(str(tmp))
        try:
            with self._lock:
                self._db.backup(out)
        finally:
            out.close()
        tmp.replace(dest)
        return dest

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

