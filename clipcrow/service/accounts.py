"""Users, sessions and the usage ledger: PostgreSQL (``DATABASE_URL``) or, by default, one SQLite file (``<data>/clipcrow.db``); see ``db.py``.

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
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import db as dbm
from .db import IntegrityError

log = logging.getLogger("clipcrow.service")

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


def schema_statements(dialect: str) -> list[str]:
    """The tables, as separate statements. SQLite keeps the old ``pw_hash`` column (a file from before the email-code sign-in may have it); PostgreSQL never had one."""
    auto = "INTEGER PRIMARY KEY AUTOINCREMENT" if dialect == "sqlite" else "BIGSERIAL PRIMARY KEY"
    pw = " pw_hash TEXT NOT NULL DEFAULT ''," if dialect == "sqlite" else ""
    return [
        f"CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL,{pw} created_at TEXT NOT NULL, plan TEXT NOT NULL DEFAULT 'free', "
        "plan_until TEXT, subscription TEXT, credits DOUBLE PRECISION NOT NULL DEFAULT 0, notify_job_done INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0)",
        "CREATE TABLE IF NOT EXISTS login_codes (email TEXT PRIMARY KEY, code_hash TEXT NOT NULL, expires_at DOUBLE PRECISION NOT NULL, tries INTEGER NOT NULL DEFAULT 0, "
        "created_at DOUBLE PRECISION NOT NULL)",
        "CREATE TABLE IF NOT EXISTS sociobird_links (user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, kind TEXT NOT NULL, token_enc TEXT NOT NULL, "
        "label TEXT NOT NULL DEFAULT '', connected_at TEXT NOT NULL, last_ok_at TEXT, last_error TEXT NOT NULL DEFAULT '')",
        "CREATE TABLE IF NOT EXISTS oauth_states (state_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, expires_at DOUBLE PRECISION NOT NULL)",
        "CREATE TABLE IF NOT EXISTS login_sends (email TEXT NOT NULL, sent_at DOUBLE PRECISION NOT NULL)",
        "CREATE INDEX IF NOT EXISTS login_sends_email ON login_sends(email, sent_at)",
        "CREATE INDEX IF NOT EXISTS login_sends_at ON login_sends(sent_at)",
        "CREATE INDEX IF NOT EXISTS users_created ON users(created_at)",
        "CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, created_at TEXT NOT NULL, "
        "expires_at DOUBLE PRECISION NOT NULL)",
        "CREATE INDEX IF NOT EXISTS sessions_expires ON sessions(expires_at)",
        "CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id)",
        f"CREATE TABLE IF NOT EXISTS usage (id {auto}, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, job_id TEXT, minutes DOUBLE PRECISION NOT NULL, "
        "credit_minutes DOUBLE PRECISION NOT NULL DEFAULT 0, month TEXT NOT NULL, kind TEXT NOT NULL, created_at TEXT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS usage_user_month ON usage(user_id, month)",
        f"CREATE TABLE IF NOT EXISTS credit_log (id {auto}, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, delta DOUBLE PRECISION NOT NULL, "
        "balance_after DOUBLE PRECISION NOT NULL, by_email TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS credit_log_user ON credit_log(user_id, id)",
        # a superadmin viewing another account (read-only, time-limited); it ends with the session, or when either account is deleted
        "CREATE TABLE IF NOT EXISTS impersonation (token_hash TEXT PRIMARY KEY REFERENCES sessions(token_hash) ON DELETE CASCADE, "
        "target_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, until DOUBLE PRECISION NOT NULL)",
        f"CREATE TABLE IF NOT EXISTS admin_audit (id {auto}, at TEXT NOT NULL, admin_email TEXT NOT NULL, action TEXT NOT NULL, target_email TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '')",
        "CREATE TABLE IF NOT EXISTS webhook_events (id TEXT PRIMARY KEY, received_at TEXT NOT NULL)",
    ]


class Accounts:
    def __init__(self, target: str | Path, *, max_failures: int = 5, window_s: int = 900, secret: bytes | None = None, schema: str | None = None,
                 secret_file: str | Path | None = None, pool_size: int | None = None):
        """``target``: a ``postgresql://`` address, or the path of a SQLite file. ``secret_file`` is where the server secret lives when the database is not a
        file next to it (PostgreSQL); ``schema`` gives a PostgreSQL database a private schema (tests)."""
        self.target = str(target)
        self.postgres = dbm.is_postgres(target)
        self.path = None if self.postgres else Path(target)
        self._secret_file = Path(secret_file) if secret_file else (self.path.with_suffix(".secret") if self.path else None)
        self.db = dbm.connect(target, schema=schema, pool_size=pool_size or int(os.environ.get("CLIPCROW_DB_POOL", "10") or 10))
        cols = self.db.columns("login_sends")
        if cols and "sent_at" not in cols:                                                       # an early development shape (column "at"): only short-lived rate-limit rows
            self.db.script(["DROP TABLE login_sends"])
        self.db.script(schema_statements(self.db.dialect))
        self.db.add_column("users", "notify_job_done", "INTEGER NOT NULL DEFAULT 0")             # an older database
        self.db.add_column("users", "disabled", "INTEGER NOT NULL DEFAULT 0")
        self._secret = secret or self._load_secret()
        self._lock = threading.RLock()
        self._fails: dict[str, list[float]] = {}
        self.max_failures, self.window_s = max_failures, window_s

    @property
    def dialect(self) -> str:
        return self.db.dialect

    def describe(self) -> str:
        return dbm.describe(self.target)

    def _load_secret(self) -> bytes:
        """The key that signs login codes and seals stored credentials: CLIPCROW_SECRET_KEY, else a random one kept in a file (mode 600), made once."""
        env = os.environ.get("CLIPCROW_SECRET_KEY", "").strip()
        if env:
            return hashlib.sha256(env.encode()).digest()
        if self._secret_file is None:
            raise RuntimeError("set CLIPCROW_SECRET_KEY, or give a secret file")
        f = self._secret_file
        if f.is_file():
            try:
                key = bytes.fromhex(f.read_text().strip())
            except ValueError:
                key = b""
            if len(key) != 32:                                         # empty or cut short (a crash while it was written): never sign with a known key
                raise RuntimeError(f"{f.name} is damaged; restore it from a backup (deleting it would lock everyone out of stored SocioBird links)")
            return key
        f.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as h:
            h.write(key.hex())
            h.flush()
            os.fsync(h.fileno())
        return key

    # --- plumbing -------------------------------------------------------------------------------
    def _q(self, sql: str, args: tuple = ()):
        return self.db.q(sql, args)

    def transaction(self):
        return _Tx(self)

    def close(self) -> None:
        self.db.close()

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
        self._q("INSERT INTO users(id, email, created_at) VALUES (?,?,?) ON CONFLICT(email) DO NOTHING", (uid, email, _now()))      # a parallel request may have made it
        return self._user(self._q("SELECT * FROM users WHERE email=?", (email,)).fetchone())

    def by_email(self, email: str) -> Optional[User]:
        row = self._q("SELECT * FROM users WHERE email=?", ((email or "").strip().lower(),)).fetchone()
        return self._user(row) if row else None

    # --- a person's own SocioBird connection (the credential is stored encrypted; see secretbox.py) -------------
    def set_link(self, user_id: str, kind: str, token: str, label: str = "") -> None:
        from . import secretbox
        self._q("INSERT INTO sociobird_links(user_id, kind, token_enc, label, connected_at, last_ok_at, last_error) VALUES (?,?,?,?,?,?, '') "
                "ON CONFLICT(user_id) DO UPDATE SET kind=excluded.kind, token_enc=excluded.token_enc, label=excluded.label, connected_at=excluded.connected_at, "
                "last_ok_at=excluded.last_ok_at, last_error=''", (user_id, kind, secretbox.seal(self._secret, "sociobird", token), label[:80], _now(), _now()))

    def get_link(self, user_id: str) -> Optional[dict]:
        """The connection without its credential: ``{kind, label, connected_at, last_ok_at, last_error}``."""
        row = self._q("SELECT kind, label, connected_at, last_ok_at, last_error FROM sociobird_links WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def link_token(self, user_id: str) -> Optional[str]:
        """The plaintext credential, for the one call that uses it; None when there is none or it cannot be opened with this server's secret."""
        from . import secretbox
        row = self._q("SELECT token_enc FROM sociobird_links WHERE user_id=?", (user_id,)).fetchone()
        return secretbox.open_(self._secret, "sociobird", row["token_enc"]) if row else None

    def mark_link(self, user_id: str, ok: bool, error: str = "") -> None:
        if ok:
            self._q("UPDATE sociobird_links SET last_ok_at=?, last_error='' WHERE user_id=?", (_now(), user_id))
        else:
            self._q("UPDATE sociobird_links SET last_error=? WHERE user_id=?", (error[:200], user_id))

    def new_oauth_state(self, user_id: str) -> str:
        """A random single-use value for one SocioBird authorisation, tied to THIS person and valid 10 minutes. Only its hash is stored."""
        state = secrets.token_urlsafe(32)
        self._q("DELETE FROM oauth_states WHERE expires_at < ?", (time.time(),))
        self._q("DELETE FROM oauth_states WHERE user_id=?", (user_id,))                      # one pending authorisation per person
        self._q("INSERT INTO oauth_states VALUES (?,?,?)", (hashlib.sha256(state.encode()).hexdigest(), user_id, time.time() + 600))
        return state

    def take_oauth_state(self, state: str) -> Optional[str]:
        """The person this state was made for, and the state is used up; None for an unknown, used or expired one. (The browser comes back from another site, so
        the session cookie may not be sent: the state alone says who started this, and it cannot be used for anyone else.)"""
        h = hashlib.sha256((state or "").encode()).hexdigest()
        row = self._q("DELETE FROM oauth_states WHERE state_hash=? RETURNING user_id, expires_at", (h,)).fetchone()         # one statement: two callbacks cannot both use it
        return row["user_id"] if row and row["expires_at"] > time.time() else None

    def delete_link(self, user_id: str) -> None:
        self._q("DELETE FROM sociobird_links WHERE user_id=?", (user_id,))

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
        with self.db.tx() as t:
            t.q("DELETE FROM login_sends WHERE sent_at < ?", (now - 86400,))
            sends = [r["sent_at"] for r in t.q("SELECT sent_at FROM login_sends WHERE email=?", (email,)).fetchall()]
            found = t.q("SELECT disabled FROM users WHERE email=?", (email,)).fetchone()
            exists = found is not None
            code = f"{secrets.randbelow(10 ** 6):06d}"
            mac = self._mac(email, code)                                   # the same work whether or not a mail will go out
            if (not exists and not allow_signup) or (exists and found["disabled"]) or any(now - x < CODE_WAIT_S for x in sends) \
                    or sum(1 for x in sends if now - x < 3600) >= CODE_PER_HOUR or len(sends) >= CODE_PER_DAY:
                return None
            t.q("INSERT INTO login_codes(email, code_hash, expires_at, tries, created_at) VALUES (?,?,?,0,?) ON CONFLICT(email) DO UPDATE SET "
                "code_hash=excluded.code_hash, expires_at=excluded.expires_at, tries=0, created_at=excluded.created_at", (email, mac, now + CODE_MINUTES * 60, now))
            t.q("INSERT INTO login_sends(email, sent_at) VALUES (?,?)", (email, now))
        return code

    def verify_code(self, email: str, code: str, *, who: str = "", allow_signup: bool = True) -> User:
        """Check the code; on success the code is used up and the account exists (made now when new). Every failure says the same thing."""
        email = (email or "").strip().lower()
        wrong = AccountError("That code is not right or has expired. Ask for a new one.")
        self._throttle(f"vip:{who}", 60, 900)
        code = re.sub(r"\s+", "", code or "")
        if not EMAIL.match(email) or not re.fullmatch(r"\d{6}", code):
            raise wrong
        outcome = None
        with self.db.tx() as t:
            row = t.q("SELECT * FROM login_codes WHERE email=?" + self.db.for_update, (email,)).fetchone()
            good = bool(row) and row["expires_at"] > time.time() and row["tries"] < CODE_TRIES
            ok = hmac.compare_digest(self._mac(email, code), row["code_hash"] if row else self._mac(email, "000000")) and good
            if not ok:
                if row:
                    if row["tries"] + 1 >= CODE_TRIES:
                        t.q("DELETE FROM login_codes WHERE email=?", (email,))              # too many wrong tries: the code is dead
                    else:
                        t.q("UPDATE login_codes SET tries=tries+1 WHERE email=?", (email,))
                outcome = wrong
            else:
                t.q("DELETE FROM login_codes WHERE email=?", (email,))                      # single use
        if outcome is not None:
            raise outcome
        existing = self.by_email(email)
        if (existing is None and not allow_signup) or (existing is not None and existing.disabled):
            raise wrong
        return existing or self.ensure_user(email)

    def sign_out_everywhere(self, user_id: str) -> None:
        self._q("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def delete(self, user_id: str) -> None:
        self._q("DELETE FROM users WHERE id=?", (user_id,))                # sessions, usage and posts go with it (cascade)

    # --- sessions -------------------------------------------------------------------------------
    def new_session(self, user_id: str, days: Optional[float] = None) -> str:
        token = secrets.token_urlsafe(32)
        self._q("INSERT INTO sessions VALUES (?,?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), user_id, _now(),
                                                          time.time() + (days or SESSION_DAYS) * 86400))
        self._q("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
        return token

    def user_for_session(self, token: str) -> Optional[User]:
        if not token:
            return None
        row = self._q("SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.disabled=0",
                      (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        return self._user(row) if row else None

    # --- a superadmin looking at another account (view-only; see api.authed) ---------------------------------------
    def start_impersonation(self, token: str, target_id: str, minutes: int = 60) -> None:
        h = hashlib.sha256(token.encode()).hexdigest()
        with self.db.tx() as t:
            t.q("DELETE FROM impersonation WHERE token_hash=?", (h,))
            t.q("INSERT INTO impersonation(token_hash, target_id, until) VALUES (?,?,?)", (h, target_id, time.time() + minutes * 60))

    def stop_impersonation(self, token: str) -> None:
        self._q("DELETE FROM impersonation WHERE token_hash=?", (hashlib.sha256((token or "").encode()).hexdigest(),))

    def impersonation_target(self, token: str) -> Optional[User]:
        """The account this session is viewing, while the time allowed lasts; None otherwise (an expired one is removed)."""
        if not token:
            return None
        h = hashlib.sha256(token.encode()).hexdigest()
        row = self._q("SELECT u.*, i.until AS _until FROM impersonation i JOIN users u ON u.id = i.target_id WHERE i.token_hash=?", (h,)).fetchone()
        if row is None:
            return None
        if float(row["_until"]) <= time.time():
            self._q("DELETE FROM impersonation WHERE token_hash=?", (h,))
            return None
        return self._user(row)

    def audit(self, admin_email: str, action: str, target_email: str = "", note: str = "") -> None:
        self._q("INSERT INTO admin_audit(at, admin_email, action, target_email, note) VALUES (?,?,?,?,?)", (_now(), admin_email[:200], action[:40], target_email[:200], note[:200]))

    def audit_list(self, limit: int = 100) -> list[dict]:
        rows = self._q("SELECT at, admin_email, action, target_email, note FROM admin_audit ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [{"at": r["at"], "by": r["admin_email"], "action": r["action"], "target": r["target_email"], "note": r["note"]} for r in rows]

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
        """Take ``minutes`` from this month's allowance, then from purchased credits. Atomic (the person's row is locked, so two jobs started at once cannot both
        spend the same minutes). Returns the minutes left afterwards, or None when there are not enough (nothing is taken)."""
        with self.db.tx() as t:
            user = t.q("SELECT credits FROM users WHERE id=?" + self.db.for_update, (user_id,)).fetchone()
            if user is None:
                return None
            used = float(t.q("SELECT COALESCE(SUM(minutes - credit_minutes), 0) AS m FROM usage WHERE user_id=? AND month=?", (user_id, month_key())).fetchone()["m"])
            left = max(allowance - used, 0.0)
            from_credits = max(minutes - left, 0.0)
            if from_credits > user["credits"] + 1e-9:
                return None
            t.q("UPDATE users SET credits = credits - ? WHERE id=?", (from_credits, user_id))
            t.q("INSERT INTO usage(user_id, job_id, minutes, credit_minutes, month, kind, created_at) VALUES (?,?,?,?,?,?,?)",
                (user_id, job_id, minutes, from_credits, month_key(), "job", _now()))
            return max(allowance - used - minutes, 0.0) + user["credits"] - from_credits

    def refund(self, user_id: str, job_id: str) -> bool:
        """Give back what a job reserved (it failed or was cancelled). Once per job."""
        with self.db.tx() as t:
            t.q("SELECT credits FROM users WHERE id=?" + self.db.for_update, (user_id,))          # lock the person's row: two refunds of one job cannot both pass
            rows = t.q("SELECT kind, SUM(minutes) AS m, SUM(credit_minutes) AS c, MAX(month) AS mo FROM usage WHERE user_id=? AND job_id=? GROUP BY kind", (user_id, job_id)).fetchall()
            took = next((r for r in rows if r["kind"] == "job"), None)
            back = next((r for r in rows if r["kind"] == "refund"), None)
            if took is None:
                return False
            # what is still held for this job: every reserve minus every refund (a failed job that was run again reserved twice, and must be able to refund the second time)
            held_m = float(took["m"]) + (float(back["m"]) if back else 0.0)
            held_c = float(took["c"]) + (float(back["c"]) if back else 0.0)
            if held_m <= 1e-9:
                return False
            t.q("UPDATE users SET credits = credits + ? WHERE id=?", (held_c, user_id))
            t.q("INSERT INTO usage(user_id, job_id, minutes, credit_minutes, month, kind, created_at) VALUES (?,?,?,?,?,?,?)",
                (user_id, job_id, -held_m, -held_c, took["mo"], "refund", _now()))
            return True

    def adjust_credits(self, user_id: str, delta: float, by: str, note: str = "") -> float:
        """A superadmin adds or removes extra minutes by hand. Atomic, never below zero, and every change is written to a ledger with who made it and why.
        Returns the new balance. Raises AccountError when the account does not exist or when a removal is more than the balance."""
        with self.db.tx() as t:
            row = t.q("SELECT credits FROM users WHERE id=?" + self.db.for_update, (user_id,)).fetchone()
            if row is None:
                raise AccountError("No such account.")
            new = round(float(row["credits"]) + float(delta), 6)
            if new < -1e-9:
                raise AccountError(f"That removes {abs(delta):g} minutes, but the account has only {float(row['credits']):g} extra minutes.")
            new = max(new, 0.0)
            t.q("UPDATE users SET credits=? WHERE id=?", (new, user_id))
            t.q("INSERT INTO credit_log(user_id, delta, balance_after, by_email, note, created_at) VALUES (?,?,?,?,?,?)", (user_id, float(delta), new, by[:200], note[:200], _now()))
            return new

    def credit_history(self, user_id: str, limit: int = 50) -> list[dict]:
        rows = self._q("SELECT delta, balance_after, by_email, note, created_at FROM credit_log WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, int(limit))).fetchall()
        return [{"delta": r["delta"], "balance": r["balance_after"], "by": r["by_email"], "note": r["note"], "at": r["created_at"]} for r in rows]

    # --- webhooks (idempotency) -----------------------------------------------------------------
    def first_time(self, event_id: str) -> bool:
        return self._q("INSERT INTO webhook_events VALUES (?,?) ON CONFLICT(id) DO NOTHING", (event_id, _now())).rowcount == 1

    def forget(self, event_id: str) -> None:
        """Take back a webhook marker when applying the event failed, so the payment provider's retry is applied instead of answered "duplicate"."""
        self._q("DELETE FROM webhook_events WHERE id=?", (event_id,))

    def backup(self, folder: str | Path, keep: int = 7) -> Optional[Path]:
        """SQLite only: a consistent copy of the database file (SQLite's online backup, safe while it is in use), once a day, the newest ``keep`` kept. Returns the
        file made, or None when today's copy already exists. PostgreSQL is backed up by the database service (see docs/deploy.md): nothing is done here."""
        if self.postgres:
            return None
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"clipcrow-{datetime.now(timezone.utc).strftime('%Y%m%d')}.db"
        made = None
        if not dest.exists():
            tmp = dest.with_suffix(".tmp")
            self.db.backup_to(tmp)
            tmp.replace(dest)
            made = dest
        for old in sorted(folder.glob("clipcrow-*.db"))[:-keep]:
            old.unlink(missing_ok=True)
        return made

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

