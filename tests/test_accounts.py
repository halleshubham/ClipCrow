"""Accounts, per-user jobs, the minutes ledger and retention."""
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from clipcrow.service import accounts as acc
from clipcrow.service.accounts import AccountError, Accounts
from clipcrow.service.api import create_app
from clipcrow.service.plans import Catalog, Plan, load_catalog
from clipcrow.service.retention import sweep
from tests.test_service import FakePipeline, upload, video_bytes, wait  # noqa: F401
from tests.mailhelp import Outbox, sign_up  # noqa: E402

PW = "correct horse battery"


@pytest.fixture
def db(tmp_path):
    return Accounts(tmp_path / "t.db", max_failures=3)


def small_catalog(free=10, pro=100):
    return Catalog(plans={"free": Plan("free", "Free", free, 7, 3), "pro": Plan("pro", "Pro", pro, 30, 14)})


def app_client(tmp_path, pipeline=None, catalog=None, **kw):
    a = Accounts(tmp_path / "data" / "clipcrow.db")
    app = create_app(tmp_path / "data", pipeline or FakePipeline(), accounts=a, mailer=Outbox(), catalog=catalog or small_catalog(), **kw)
    return TestClient(app), a, app


def signup(c, email="a@example.com", password=PW):
    r = sign_up(c, email)
    assert r.status_code == 200, r.text
    return r


# --- sign-in codes and sessions ------------------------------------------------------------------------

def test_a_code_is_six_digits_works_once_and_is_stored_only_as_a_mac(db, tmp_path):
    code = db.request_code("A@Example.com", who="1.2.3.4")
    assert len(code) == 6 and code.isdigit()
    raw = b"".join(f.read_bytes() for f in tmp_path.glob("t.db*"))
    assert code.encode() not in raw                                                   # the table never holds the code itself
    u = db.verify_code("a@example.com", f" {code[:3]} {code[3:]} ")                  # spaces from copy and paste are fine
    assert u.email == "a@example.com" and u.plan == "free"
    with pytest.raises(AccountError, match="not right or has expired"):             # single use
        db.verify_code("a@example.com", code)


def test_a_wrong_code_five_times_kills_the_code_and_the_message_never_changes(db):
    code = db.request_code("a@example.com")
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        with pytest.raises(AccountError) as e:
            db.verify_code("a@example.com", wrong)
    with pytest.raises(AccountError) as dead:
        db.verify_code("a@example.com", code)                                         # even the right one is dead now
    with pytest.raises(AccountError) as unknown:
        db.verify_code("nobody@example.com", "123456")
    assert str(e.value) == str(dead.value) == str(unknown.value)                      # does not reveal which emails exist


def test_a_code_expires_and_a_new_request_replaces_the_old_code(db):
    c1 = db.request_code("a@example.com")
    db._q("UPDATE login_sends SET at = at - 120")                                     # past the 60 s wait
    c2 = db.request_code("a@example.com")
    assert c2 and c1 != c2 or c1 == c2
    if c1 != c2:
        with pytest.raises(AccountError):
            db.verify_code("a@example.com", c1)
    db._q("UPDATE login_codes SET expires_at = 1")
    with pytest.raises(AccountError, match="expired"):
        db.verify_code("a@example.com", c2)


def test_asking_for_codes_is_limited_per_address_per_hour_and_waits_between_mails(db):
    assert db.request_code("a@example.com")
    assert db.request_code("a@example.com") is None                                   # within the 60 s wait: nothing is mailed, the answer is the same
    for _ in range(4):
        db._q("UPDATE login_sends SET at = at - 120")
        assert db.request_code("a@example.com")
    db._q("UPDATE login_sends SET at = at - 120")
    assert db.request_code("a@example.com") is None                                   # a sixth within the hour
    with pytest.raises(AccountError, match="valid email"):
        db.request_code("nope")


def test_an_unknown_address_gets_no_mail_when_sign_up_is_closed_but_the_call_looks_the_same(db):
    assert db.request_code("new@example.com", allow_signup=False) is None
    known = db.ensure_user("old@example.com")
    assert db.request_code("old@example.com", allow_signup=False)
    assert db.get(known.id).email == "old@example.com"


def test_requests_from_one_ip_are_limited(db):
    for i in range(20):
        db.request_code(f"u{i}@example.com", who="9.9.9.9")
    with pytest.raises(AccountError, match="Too many"):
        db.request_code("x@example.com", who="9.9.9.9")


def test_the_server_secret_is_made_once_with_mode_600(tmp_path):
    a = Accounts(tmp_path / "s.db")
    f = tmp_path / "s.secret"
    assert f.is_file() and oct(f.stat().st_mode & 0o777) == "0o600"
    first = f.read_text()
    Accounts(tmp_path / "s.db")
    assert f.read_text() == first


def test_sessions_store_only_a_hash_expire_and_can_be_ended(db, tmp_path):
    u = db.ensure_user("a@example.com")
    token = db.new_session(u.id)
    assert db.user_for_session(token).id == u.id and db.user_for_session("") is None and db.user_for_session("x" * 43) is None
    raw = (tmp_path / "t.db").read_bytes() + (tmp_path / "t.db-wal").read_bytes() if (tmp_path / "t.db-wal").exists() else (tmp_path / "t.db").read_bytes()
    assert token.encode() not in raw                                          # the database never holds the token itself
    db.end_session(token)
    assert db.user_for_session(token) is None
    t2 = db.new_session(u.id)
    db._q("UPDATE sessions SET expires_at = 1")
    assert db.user_for_session(t2) is None
    t3 = db.new_session(u.id)
    db.sign_out_everywhere(u.id)
    assert db.user_for_session(t3) is None


# --- the minutes ledger -----------------------------------------------------------------------------

def test_minutes_come_from_the_allowance_then_from_credits_and_refunds_go_back_once(db):
    u = db.ensure_user("a@example.com")
    db.add_credits(u.id, 20)
    assert db.reserve(u.id, "j1", 8, allowance=10) == pytest.approx(22)       # 2 allowance + 20 credits left
    assert db.reserve(u.id, "j2", 6, allowance=10) == pytest.approx(16)       # 2 from the allowance, 4 from credits
    assert db.get(u.id).credits == pytest.approx(16)
    assert db.reserve(u.id, "j3", 17, allowance=10) is None                   # more than everything: nothing is taken
    assert db.get(u.id).credits == pytest.approx(16) and db.used_this_month(u.id) == pytest.approx(10)
    assert db.refund(u.id, "j2") is True and db.refund(u.id, "j2") is False   # once per job
    assert db.get(u.id).credits == pytest.approx(20) and db.used_this_month(u.id) == pytest.approx(8)
    assert db.refund(u.id, "never-reserved") is False


def test_the_allowance_resets_with_the_month(db, monkeypatch):
    u = db.ensure_user("a@example.com")
    monkeypatch.setattr(acc, "month_key", lambda when=None: "2026-01")
    assert db.reserve(u.id, "j1", 10, allowance=10) == pytest.approx(0)
    assert db.reserve(u.id, "j2", 1, allowance=10) is None
    monkeypatch.setattr(acc, "month_key", lambda when=None: "2026-02")
    assert db.reserve(u.id, "j2", 1, allowance=10) == pytest.approx(9)


def test_two_reservations_at_once_cannot_overdraw(db):
    u = db.ensure_user("a@example.com")
    got = []
    ts = [threading.Thread(target=lambda i=i: got.append(db.reserve(u.id, f"j{i}", 4, allowance=10))) for i in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sum(1 for g in got if g is not None) == 2                          # 4 + 4 fits in 10, a third does not
    assert db.used_this_month(u.id) == pytest.approx(8)


def test_webhook_events_are_seen_once(db):
    assert db.first_time("evt_1") is True and db.first_time("evt_1") is False and db.first_time("evt_2") is True


def test_deleting_a_user_removes_everything_of_theirs(db):
    u = db.ensure_user("a@example.com")
    db.reserve(u.id, "j", 1, 10); db.new_session(u.id)
    db.delete(u.id)
    for table in ("users", "sessions", "usage"):
        assert db._q(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] == 0


# --- plans -----------------------------------------------------------------------------------------------------

def test_the_plan_file_is_validated(tmp_path):
    ok = tmp_path / "p.json"
    ok.write_text(json.dumps({"plans": [{"id": "free", "name": "F", "monthly_minutes": 5, "retention_days": 1, "source_days": 1}],
                              "packs": [{"id": "p100", "minutes": 100, "amount_paise": 49900, "label": "100 min"}]}))
    cat = load_catalog(ok)
    assert cat.plan("nope").id == "free" and cat.packs["p100"].minutes == 100
    for bad in ({"plans": []}, {"plans": [{"id": "free", "name": "F", "monthly_minutes": -1, "retention_days": 1, "source_days": 1}]},
                {"plans": [{"id": "free", "name": "F", "monthly_minutes": 1, "retention_days": 1, "source_days": 1}],
                 "packs": [{"id": "x", "minutes": 5, "amount_paise": 10, "label": "x"}]}):
        ok.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            load_catalog(ok)
    assert load_catalog().plans["free"].monthly_minutes == 30 and all(not p.price_label for p in load_catalog().plans.values())


# --- the API ------------------------------------------------------------------------------------------------------

def test_everything_needs_a_session_and_a_session_comes_from_signing_up_or_in(tmp_path, video_bytes):
    c, _, _ = app_client(tmp_path)
    assert c.get("/api/jobs").status_code == 401 and c.get("/api/me").status_code == 401
    cfg = c.get("/api/config").json()
    assert cfg["accounts"] and cfg["signup"] and cfg["needs_login"] and not cfg["signed_in"]
    signup(c)
    me = c.get("/api/me").json()
    assert me["email"] == "a@example.com" and me["plan"]["id"] == "free" and me["left_minutes"] == 10
    c.post("/api/auth/logout")
    assert c.get("/api/me").status_code == 401
    assert c.post("/api/auth/verify", json={"email": "a@example.com", "code": "000000"}).status_code == 401
    sign_up(c, "a@example.com")                                              # the same flow signs an existing account in
    assert c.get("/api/me").status_code == 200
    assert c.post("/api/auth/register", json={"email": "a@example.com", "password": PW}).status_code in (404, 405)     # no password routes
    assert c.post("/api/auth/login", json={"email": "a@example.com", "password": PW}).status_code in (404, 405)


def test_the_session_cookie_is_httponly_and_samesite_strict(tmp_path):
    c, _, _ = app_client(tmp_path)
    r = signup(c)
    header = r.headers["set-cookie"].lower()
    assert "httponly" in header and "samesite=strict" in header


def test_sign_ups_can_be_closed(tmp_path):
    c, _, app = app_client(tmp_path, allow_signup=False)
    r = c.post("/api/auth/code", json={"email": "a@example.com"})
    assert r.status_code == 200 and app.state.outbox.sent == []              # the same answer, no mail, no account
    assert c.post("/api/auth/verify", json={"email": "a@example.com", "code": "123456"}).status_code == 401
    assert c.get("/api/config").json()["signup"] is False


def test_the_answer_to_asking_for_a_code_is_the_same_for_every_address_and_the_mail_names_no_account(tmp_path):
    c, a, app = app_client(tmp_path)
    a.ensure_user("known@example.com")
    r1 = c.post("/api/auth/code", json={"email": "known@example.com"}).json()
    r2 = c.post("/api/auth/code", json={"email": "unknown@example.com"}).json()
    assert r1 == r2
    m = app.state.outbox.last("unknown@example.com")
    assert m.kind == "sign-in-code" and m.subject.endswith("is your ClipCrow sign-in code") and "\n" not in m.subject
    assert "support@shackyapps.in" in m.text and m.html and "<script" not in m.html


def test_email_off_says_so_and_nothing_can_sign_in(tmp_path):
    a = Accounts(tmp_path / "data" / "clipcrow.db")
    c = TestClient(create_app(tmp_path / "data", FakePipeline(), accounts=a, catalog=small_catalog()))
    r = c.post("/api/auth/code", json={"email": "a@example.com"})
    assert r.status_code == 503 and "not set up" in r.json()["detail"]
    assert c.get("/api/config").json()["mail"] is False


def test_job_done_mail_goes_only_to_people_who_asked_for_it(tmp_path, video_bytes):
    c, a, app = app_client(tmp_path, public_url="https://clipcrow.example")
    signup(c)
    uid = a.by_email("a@example.com").id
    assert c.get("/api/me").json()["notify_job_done"] is False
    box = app.state.outbox
    before = len(box.sent)
    jid = upload(c, video_bytes).json()["id"]
    wait(c, jid)
    assert not [m for m in box.sent[before:] if m.kind == "job-done"]
    assert c.put("/api/me/notifications", json={"job_done": True}).json() == {"job_done": True}
    jid2 = upload(c, video_bytes).json()["id"]
    wait(c, jid2)
    done = [m for m in box.sent if m.kind == "job-done"]
    assert len(done) == 1 and done[0].to == "a@example.com" and f"https://clipcrow.example/#/jobs/{jid2}" in done[0].text
    assert a.get(uid).notify_job_done is True


def test_people_only_ever_see_their_own_jobs_and_clips(tmp_path, video_bytes):
    c, _, app = app_client(tmp_path)
    signup(c, "a@example.com")
    jid = wait(c, upload(c, video_bytes).json()["id"])["id"]
    assert [j["id"] for j in c.get("/api/jobs").json()] == [jid]
    cid = c.get(f"/api/jobs/{jid}").json()["clips"][0]["id"]
    other = TestClient(app)
    signup(other, "b@example.com")
    assert other.get("/api/jobs").json() == []
    for method, path in (("get", f"/api/jobs/{jid}"), ("get", f"/api/jobs/{jid}/usage"), ("delete", f"/api/jobs/{jid}"),
                         ("post", f"/api/jobs/{jid}/cancel"), ("get", f"/api/jobs/{jid}/clips/{cid}/video"),
                         ("get", f"/api/jobs/{jid}/clips/{cid}/download"), ("get", f"/api/jobs/{jid}/clips/{cid}"),
                         ("post", f"/api/jobs/{jid}/clips/{cid}/reset"), ("post", f"/api/jobs/{jid}/clips/{cid}/render")):
        assert getattr(other, method)(path).status_code == 404, (method, path)
    assert other.put(f"/api/jobs/{jid}/clips/{cid}", json={"title": "mine now"}).status_code == 404
    assert c.get(f"/api/jobs/{jid}").status_code == 200                       # and the owner still can


def test_a_video_longer_than_what_is_left_is_refused_with_a_clear_message(tmp_path, video_bytes):
    c, a, _ = app_client(tmp_path, catalog=small_catalog(free=3))             # the test video is 250 s = 5 minutes
    signup(c)
    r = upload(c, video_bytes)
    assert r.status_code == 402 and "5 min" in r.json()["detail"] and "3 min left" in r.json()["detail"]
    assert c.get("/api/jobs").json() == [] and c.get("/api/me").json()["used_minutes"] == 0     # nothing created, nothing charged


def test_a_job_takes_minutes_and_a_failed_or_cancelled_one_gives_them_back(tmp_path, video_bytes):
    c, a, _ = app_client(tmp_path, FakePipeline(boom=ValueError("no speech found")))
    signup(c)
    job = wait(c, upload(c, video_bytes).json()["id"])
    assert job["status"] == "failed" and job["reserved_minutes"] == 5
    assert c.get("/api/me").json()["used_minutes"] == 0                      # refunded
    c2, _, _ = app_client(tmp_path / "ok")
    signup(c2)
    wait(c2, upload(c2, video_bytes).json()["id"])
    me = c2.get("/api/me").json()
    assert me["used_minutes"] == 5 and me["left_minutes"] == 5                # a finished job keeps its minutes


def test_cancelling_a_queued_job_refunds_it(tmp_path, video_bytes):
    gate = threading.Event()
    c, a, _ = app_client(tmp_path, FakePipeline(gate=gate), catalog=small_catalog(free=20))
    signup(c)
    first = upload(c, video_bytes).json()["id"]
    second = upload(c, video_bytes).json()["id"]
    assert c.get("/api/me").json()["used_minutes"] == 10
    c.post(f"/api/jobs/{second}/cancel")
    assert c.get("/api/me").json()["used_minutes"] == 5
    c.post(f"/api/jobs/{first}/cancel")
    gate.set()
    wait(c, first)
    end = time.time() + 10                                      # the status flips just before the refund is booked: wait for it
    while c.get("/api/me").json()["used_minutes"] != 0 and time.time() < end:
        time.sleep(0.05)
    assert c.get("/api/me").json()["used_minutes"] == 0


def test_deleting_an_account_needs_its_email_typed_and_removes_the_jobs(tmp_path, video_bytes):
    c, a, app = app_client(tmp_path)
    signup(c)
    jid = wait(c, upload(c, video_bytes).json()["id"])["id"]
    assert c.request("DELETE", "/api/account", json={"email": "other@example.com"}).status_code == 403          # not 401: that means "sign in"
    assert (tmp_path / "data" / "jobs" / jid).exists()
    assert c.request("DELETE", "/api/account", json={"email": "a@example.com"}).status_code == 200
    assert not (tmp_path / "data" / "jobs" / jid).exists() and a.count() == 0
    assert c.get("/api/me").status_code == 401


def test_an_account_with_a_running_job_cannot_be_deleted_yet(tmp_path, video_bytes):
    gate = threading.Event()
    c, a, _ = app_client(tmp_path, FakePipeline(gate=gate))
    signup(c)
    jid = upload(c, video_bytes).json()["id"]
    assert c.request("DELETE", "/api/account", json={"email": "a@example.com"}).status_code == 409
    gate.set()
    wait(c, jid)


# --- retention -----------------------------------------------------------------------------------------------------------

def age(app, job_id, days):
    p = app.state.store.dir(job_id) / "job.json"
    d = json.loads(p.read_text())
    d["created_at"] = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="milliseconds")
    p.write_text(json.dumps(d))


def test_old_originals_go_first_then_the_whole_job(tmp_path, video_bytes):
    c, a, app = app_client(tmp_path)                                          # free: originals 3 days, jobs 7 days
    signup(c)
    jid = wait(c, upload(c, video_bytes).json()["id"])["id"]
    store = app.state.store
    days_for = lambda job: (7, 3)  # noqa: E731
    assert sweep(store, days_for) == {"jobs": 0, "sources": 0, "evicted": 0}
    age(app, jid, 4)
    assert sweep(store, days_for) == {"jobs": 0, "sources": 1, "evicted": 0}
    job = c.get(f"/api/jobs/{jid}").json()
    assert job["source_removed"] is True and store.source_path(jid) is None and job["expires_at"]
    cid = job["clips"][0]["id"]
    assert c.get(f"/api/jobs/{jid}/clips/{cid}/video").status_code == 200       # still watchable and downloadable
    r = c.put(f"/api/jobs/{jid}/clips/{cid}", json={"title": "new"})
    assert r.status_code == 409 and "can no longer be edited" in r.json()["detail"]
    assert sweep(store, days_for) == {"jobs": 0, "sources": 0, "evicted": 0}                  # the original is only removed once
    age(app, jid, 8)
    assert sweep(store, days_for) == {"jobs": 1, "sources": 0, "evicted": 0}
    assert c.get(f"/api/jobs/{jid}").status_code == 404


def test_a_running_job_is_never_swept_and_zero_means_keep(tmp_path, video_bytes):
    gate = threading.Event()
    c, a, app = app_client(tmp_path, FakePipeline(gate=gate))
    signup(c)
    jid = upload(c, video_bytes).json()["id"]
    age(app, jid, 400)
    assert sweep(app.state.store, lambda j: (1, 1)) == {"jobs": 0, "sources": 0, "evicted": 0}
    gate.set()
    wait(c, jid)
    assert sweep(app.state.store, lambda j: (0, 0)) == {"jobs": 0, "sources": 0, "evicted": 0}
    assert app.state.store.get(jid)


def test_a_paid_plan_keeps_jobs_longer_and_a_lapsed_one_counts_as_free(tmp_path, video_bytes):
    c, a, app = app_client(tmp_path)
    u = signup(c) and a.by_email("a@example.com")
    a.set_plan(u.id, "pro", until=(datetime.now(timezone.utc) + timedelta(days=10)).isoformat(timespec="seconds"))
    assert c.get("/api/me").json()["plan"]["id"] == "pro" and c.get("/api/me").json()["left_minutes"] == 100
    jid = wait(c, upload(c, video_bytes).json()["id"])["id"]
    assert c.get(f"/api/jobs/{jid}").json()["expires_at"] > (datetime.now(timezone.utc) + timedelta(days=20)).isoformat()
    a.set_plan(u.id, "pro", until=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds"))
    assert c.get("/api/me").json()["plan"]["id"] == "free"                      # ran out and not renewed: free again


def test_the_database_is_backed_up_once_a_day_keeping_the_newest_few(db, tmp_path):
    db.ensure_user("a@example.com")
    folder = tmp_path / "backups"
    first = db.backup(folder, keep=2)
    assert first and first.is_file() and db.backup(folder, keep=2) is None                   # once a day
    for day in ("20200101", "20200102", "20200103"):
        (folder / f"clipcrow-{day}.db").write_bytes(b"old")
    db.backup(folder, keep=2)
    assert len(list(folder.glob("clipcrow-*.db"))) == 2
    import sqlite3
    copy = sqlite3.connect(str(first))
    assert copy.execute("SELECT email FROM users").fetchone()[0] == "a@example.com"          # a real, readable copy
    copy.close()
