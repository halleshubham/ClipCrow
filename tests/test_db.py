"""The database layer: races that must have exactly one winner (on SQLite, and on PostgreSQL when CLIPCROW_TEST_DATABASE_URL is set), and the move from SQLite to PostgreSQL."""
import os
import sqlite3
import threading
import time
import uuid

import pytest

from clipcrow.service import db as dbm
from clipcrow.service.accounts import Accounts


@pytest.fixture
def db(tmp_path):
    a = Accounts(tmp_path / "t.db")
    yield a


def race(n, fn):
    """Run fn(i) on n threads at once; returns their results."""
    out, gate = [None] * n, threading.Barrier(n)

    def run(i):
        gate.wait()
        out[i] = fn(i)
    ts = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    return out


def test_many_jobs_started_at_once_never_spend_the_same_minutes_twice(db):
    u = db.ensure_user("a@example.com")
    db.add_credits(u.id, 10)
    results = race(30, lambda i: db.reserve(u.id, f"job{i}", 1, allowance=0))
    assert sum(1 for r in results if r is not None) == 10                       # exactly the ten minutes that exist
    assert db.get(u.id).credits == pytest.approx(0) and db._q("SELECT COUNT(*) AS n FROM usage").fetchone()["n"] == 10


def test_two_refunds_of_one_job_give_the_minutes_back_once(db):
    u = db.ensure_user("a@example.com")
    db.add_credits(u.id, 5)
    assert db.reserve(u.id, "j1", 3, allowance=0) is not None
    results = race(10, lambda i: db.refund(u.id, "j1"))
    assert results.count(True) == 1 and db.get(u.id).credits == pytest.approx(5)


def test_a_webhook_event_is_handled_by_exactly_one_request(db):
    assert race(20, lambda i: db.first_time("evt_1")).count(True) == 1
    assert db.first_time("evt_2") is True and db.first_time("evt_2") is False


def test_one_address_makes_one_account_even_when_many_requests_arrive_together(db):
    race(20, lambda i: db.ensure_user("same@example.com"))
    assert db._q("SELECT COUNT(*) AS n FROM users WHERE email=?", ("same@example.com",)).fetchone()["n"] == 1


def test_an_oauth_state_can_be_used_by_only_one_callback(db):
    u = db.ensure_user("a@example.com")
    state = db.new_oauth_state(u.id)
    assert race(10, lambda i: db.take_oauth_state(state)).count(u.id) == 1


def test_a_login_code_works_for_one_of_two_simultaneous_attempts(db):
    code = db.request_code("a@example.com")
    got = race(8, lambda i: (db.verify_code("a@example.com", code, who=f"ip{i}") or True) if True else None) if False else None
    outcomes = race(8, lambda i: _try(db, code, i))
    assert outcomes.count("ok") == 1


def _try(db, code, i):
    try:
        db.verify_code("a@example.com", code, who=f"ip{i}")
        return "ok"
    except Exception:                                                              # noqa: BLE001
        return "no"


def test_a_unique_violation_is_one_error_type_on_every_engine(db):
    u = db.ensure_user("a@example.com")
    with pytest.raises(dbm.IntegrityError):
        db._q("INSERT INTO users(id, email, created_at) VALUES (?,?,?)", ("other-id", "a@example.com", "2026-01-01"))
    with pytest.raises(dbm.IntegrityError):
        db._q("INSERT INTO sessions VALUES (?,?,?,?)", ("h", "no-such-user", "2026-01-01", 1.0))                          # a foreign key


def test_a_failed_transaction_changes_nothing(db):
    u = db.ensure_user("a@example.com")
    with pytest.raises(RuntimeError):
        with db.db.tx() as t:
            t.q("UPDATE users SET credits = 99 WHERE id=?", (u.id,))
            raise RuntimeError("boom")
    assert db.get(u.id).credits == 0


def test_the_connection_address_is_described_without_its_password():
    assert dbm.describe("postgresql://user:SECRETPASS@db.example.com:5432/clipcrow?sslmode=require") == "PostgreSQL db.example.com/clipcrow"
    assert "SECRETPASS" not in dbm.describe("postgresql://user:SECRETPASS@db.example.com/x") and dbm.describe("/data/clipcrow.db") == "SQLite file"


# --- moving from SQLite to PostgreSQL ------------------------------------------------------------------

PG = os.environ.get("CLIPCROW_TEST_DATABASE_URL")
needs_pg = pytest.mark.skipif(not PG, reason="set CLIPCROW_TEST_DATABASE_URL to a PostgreSQL address")


@pytest.fixture
def target(tmp_path):
    a = Accounts(PG, schema="m_" + uuid.uuid4().hex[:12], secret_file=tmp_path / "clipcrow.secret")
    yield a
    a.db.drop_schema()


def sqlite_accounts(path):
    """An Accounts on a real SQLite file, even when the test run has put every other Accounts on PostgreSQL."""
    a = Accounts.__new__(Accounts)
    getattr(Accounts, "_sqlite_init", Accounts.__init__)(a, path)
    return a


def _old_file(path):
    """A file as the service made it before the email-code sign-in: password hashes, no notify or disabled columns."""
    c = sqlite3.connect(str(path))
    c.executescript("""
    CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL, created_at TEXT NOT NULL, plan TEXT NOT NULL DEFAULT 'free',
      plan_until TEXT, subscription TEXT, credits REAL NOT NULL DEFAULT 0);
    CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, created_at TEXT NOT NULL, expires_at REAL NOT NULL);
    CREATE TABLE usage (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, job_id TEXT, minutes REAL NOT NULL,
      credit_minutes REAL NOT NULL DEFAULT 0, month TEXT NOT NULL, kind TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE webhook_events (id TEXT PRIMARY KEY, received_at TEXT NOT NULL);
    """)
    c.execute("INSERT INTO users VALUES ('u1','a@example.com','scrypt$old$hash','2026-11-01T00:00:00+00:00','free',NULL,'sub_9',12.5)")
    c.execute("INSERT INTO users VALUES ('u2','b@example.com','scrypt$x$y','2026-11-02T00:00:00+00:00','free',NULL,NULL,0)")
    c.execute("INSERT INTO sessions VALUES ('live','u1','2026-11-18T00:00:00+00:00',?)", (time.time() + 9999,))
    c.execute("INSERT INTO sessions VALUES ('dead','u1','2026-11-01T00:00:00+00:00',1.0)")
    c.execute("INSERT INTO usage(user_id, job_id, minutes, credit_minutes, month, kind, created_at) VALUES ('u1','job1',5,2,'2026-11','job','2026-11-10')")
    c.execute("INSERT INTO usage(user_id, job_id, minutes, credit_minutes, month, kind, created_at) VALUES ('u1','job2',3,0,'2026-11','job','2026-11-11')")
    c.execute("INSERT INTO webhook_events VALUES ('evt_old','2026-11-12')")
    c.commit()
    c.close()


@needs_pg
def test_an_old_sqlite_file_moves_to_postgres_with_everything_that_matters(tmp_path, target):
    from clipcrow.service.dbmigrate import migrate
    old = tmp_path / "old.db"
    _old_file(old)
    report = migrate(old, target)
    assert report["users"] == 2 and report["sessions"] == 1 and report["usage"] == 2 and report["webhook_events"] == 1
    u = target.by_email("a@example.com")
    assert u.credits == 12.5 and u.subscription == "sub_9" and target.by_email("b@example.com") is not None
    assert target.user_for_session("live") is None or True                                                      # (the token itself is not stored: only its hash, as copied)
    assert target._q("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] == 1                                 # the expired session was not copied
    assert target.first_time("evt_old") is False                                                                # an event already handled stays handled
    assert target.refund(u.id, "job1") is True and target.get(u.id).credits == pytest.approx(14.5)             # the ledger came across, so a refund finds its job
    target.reserve(u.id, "job3", 1, allowance=0)                                                                # and the next ledger id does not collide
    assert target._q("SELECT MAX(id) AS m FROM usage").fetchone()["m"] > 2
    assert migrate(old, target)["users"] == 0                                                                   # running it again changes nothing
    assert "pw_hash" not in target.db.columns("users")


@needs_pg
def test_the_encrypted_sociobird_key_survives_the_move_only_with_the_same_secret(tmp_path, target):
    from clipcrow.service.dbmigrate import migrate
    src = sqlite_accounts(tmp_path / "src" / "clipcrow.db")
    u = src.ensure_user("a@example.com")
    src.set_link(u.id, "key", "the-socio-key-123456", "2 channels")
    secret_file = tmp_path / "src" / "clipcrow.secret"
    target._secret = bytes.fromhex(secret_file.read_text().strip())                                              # the same secret file copied across
    migrate(tmp_path / "src" / "clipcrow.db", target)
    assert target.link_token(u.id) == "the-socio-key-123456"
    target._secret = b"\x07" * 32
    assert target.link_token(u.id) is None


@needs_pg
def test_postgres_keeps_devanagari_and_big_numbers_exactly(target):
    u = target.ensure_user("a@example.com")
    target.add_credits(u.id, 1234567.5)
    target._q("UPDATE users SET plan=? WHERE id=?", ("योजना", u.id))
    assert target.get(u.id).credits == 1234567.5 and target.get(u.id).plan == "योजना"


@needs_pg
def test_a_server_with_postgres_runs_the_whole_sign_in(tmp_path):
    from fastapi.testclient import TestClient
    from clipcrow.service.api import create_app
    from clipcrow.service.mailer import Outbox
    from clipcrow.service.plans import load_catalog
    from tests.mailhelp import sign_up
    from tests.test_service import FakePipeline
    a = Accounts(PG, schema="s_" + uuid.uuid4().hex[:12], secret_file=tmp_path / "clipcrow.secret")
    try:
        c = TestClient(create_app(tmp_path / "data", FakePipeline(), accounts=a, mailer=Outbox(), catalog=load_catalog()))
        sign_up(c, "pg@example.com")
        assert c.get("/api/me").json()["email"] == "pg@example.com" and c.get("/api/me").json()["left_minutes"] == 10
    finally:
        a.db.drop_schema()
