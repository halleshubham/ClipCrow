"""Cost limits for each person, the operator's page, health and alerts."""
import threading

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from clipcrow.service import ops
from clipcrow.service.accounts import Accounts
from clipcrow.service.api import create_app
from clipcrow.service.ops import Limits, Snapshot, Watchdog
from clipcrow.service.plans import Catalog, Plan
from tests.mailhelp import Outbox, sign_up
from tests.test_service import FakePipeline, upload, video_bytes, wait  # noqa: F401


def cat():
    return Catalog(plans={"free": Plan("free", "Free", 500, 7, 3), "pro": Plan("pro", "Pro", 5000, 30, 14)})


def client(tmp_path, monkeypatch, limits=None, pipeline=None, admins="", **kw):
    monkeypatch.setenv("CLIPCROW_ADMINS", admins)
    a = Accounts(tmp_path / "data" / "clipcrow.db")
    app = create_app(tmp_path / "data", pipeline or FakePipeline(), accounts=a, mailer=Outbox(), catalog=cat(), limits=limits or Limits(), **kw)
    return TestClient(app), a, app


# --- pure parts -------------------------------------------------------------------------------------

def test_job_refusal_names_the_limit_in_words():
    lim = Limits(active_jobs=2, jobs_per_day=5, job_minutes=60)
    assert ops.job_refusal(lim, active=0, today=0, minutes=30) is None
    assert ops.job_refusal(lim, active=2, today=0, minutes=30)[0] == 429
    assert ops.job_refusal(lim, active=0, today=5, minutes=30)[0] == 429
    code, msg = ops.job_refusal(lim, active=0, today=0, minutes=90)
    assert code == 413 and "60 min" in msg
    assert ops.job_refusal(Limits(0, 0, 0, 0), active=99, today=999, minutes=9999) is None        # 0 switches a limit off


def test_throwaway_mailboxes_are_recognised_including_subdomains(monkeypatch):
    assert ops.is_throwaway("x@mailinator.com") and ops.is_throwaway("x@a.yopmail.com") and not ops.is_throwaway("x@gmail.com")
    monkeypatch.setenv("CLIPCROW_BLOCKED_EMAIL_DOMAINS", "bad.example, other.example")
    assert ops.is_throwaway("x@bad.example") and ops.is_throwaway("x@OTHER.example")


def test_alerts_cover_disk_queue_failures_workers_and_mail():
    assert ops.health_alerts(Snapshot()) == []
    keys = {k for k, _ in ops.health_alerts(Snapshot(disk_free_gb=2, disk_free_pct=4, queued=3, oldest_queued_s=1200, failed_last_hour=6, unserved_tasks=2, mail_failures_hour=4))}
    assert keys == {"disk", "queue", "failures", "workers", "mail"}
    assert not ops.health_alerts(Snapshot(queued=3, running=1, oldest_queued_s=5000))            # something is running: not stuck


def test_the_watchdog_tells_once_an_hour_and_again_when_it_clears():
    now = [0.0]
    sent = []
    snap = [Snapshot(disk_free_gb=1, disk_free_pct=2)]
    w = Watchdog(lambda: snap[0], lambda s, b: sent.append((s, b)), clock=lambda: now[0])
    w.check(); w.check()
    assert len(sent) == 1 and sent[0][0] == "ClipCrow alert: disk"
    now[0] = 3700
    w.check()
    assert len(sent) == 2                                                                           # still wrong an hour later: told again
    snap[0] = Snapshot()
    w.check()
    assert sent[-1][0] == "ClipCrow recovered: disk"
    w.check()
    assert len(sent) == 3
    assert "@" not in " ".join(b for _, b in sent)                                                  # no address in an alert


# --- the API -----------------------------------------------------------------------------------------

def test_health_is_public_and_says_nothing_else(tmp_path, monkeypatch):
    c, _, _ = client(tmp_path, monkeypatch)
    assert c.get("/api/health").json() == {"ok": True}


def test_a_person_cannot_start_more_jobs_than_the_daily_limit_and_nothing_is_taken(tmp_path, monkeypatch, video_bytes):
    c, a, _ = client(tmp_path, monkeypatch, limits=Limits(active_jobs=5, jobs_per_day=1, job_minutes=240, signups_per_day=100))
    sign_up(c)
    assert upload(c, video_bytes).status_code in (200, 201)
    left = c.get("/api/me").json()["left_minutes"]
    r = upload(c, video_bytes)
    assert r.status_code == 429 and "daily limit" in r.json()["detail"]
    assert c.get("/api/me").json()["left_minutes"] == left                                         # a refused job costs no minutes


def test_active_jobs_are_limited_per_person_not_per_server(tmp_path, monkeypatch, video_bytes):
    gate = threading.Event()
    c, a, _ = client(tmp_path, monkeypatch, limits=Limits(active_jobs=1, jobs_per_day=50, job_minutes=240, signups_per_day=100), pipeline=FakePipeline(gate=gate))
    sign_up(c)
    assert upload(c, video_bytes).status_code in (200, 201)
    r = upload(c, video_bytes)
    assert r.status_code == 429 and "running or waiting" in r.json()["detail"]
    other = TestClient(c.app)
    sign_up(other, "b@example.com")
    assert upload(other, video_bytes).status_code in (200, 201)                                     # another person is not affected
    gate.set()


def test_an_operator_is_not_limited(tmp_path, monkeypatch, video_bytes):
    c, a, _ = client(tmp_path, monkeypatch, limits=Limits(active_jobs=5, jobs_per_day=1, job_minutes=240, signups_per_day=100), admins="boss@example.com")
    sign_up(c, "boss@example.com")
    assert upload(c, video_bytes).status_code in (200, 201) and upload(c, video_bytes).status_code in (200, 201)


def test_sign_ups_pause_at_the_daily_cap_but_existing_people_still_sign_in(tmp_path, monkeypatch):
    c, a, app = client(tmp_path, monkeypatch, limits=Limits(signups_per_day=1))
    sign_up(c, "first@example.com")
    assert c.get("/api/config").json()["signup_paused"] is True
    before = len(app.state.outbox.sent)
    r = c.post("/api/auth/code", json={"email": "second@example.com"})
    assert r.status_code == 200 and len(app.state.outbox.sent) == before                           # same answer, no mail, no account
    sign_up(TestClient(app), "first@example.com")                                                  # the existing account is unaffected
    assert a.by_email("second@example.com") is None


def test_a_throwaway_address_is_asked_for_a_permanent_one(tmp_path, monkeypatch):
    c, _, app = client(tmp_path, monkeypatch)
    r = c.post("/api/auth/code", json={"email": "x@mailinator.com"})
    assert r.status_code == 422 and "permanent" in r.json()["detail"] and app.state.outbox.sent == []


def test_the_admin_page_exists_only_for_admins_and_can_adjust_accounts(tmp_path, monkeypatch, video_bytes):
    c, a, app = client(tmp_path, monkeypatch, admins="boss@example.com")
    sign_up(c, "user@example.com")
    assert c.get("/api/admin/overview").status_code == 404 and c.get("/api/me").json()["admin"] is False       # looks like a page that is not there
    uid = a.by_email("user@example.com").id
    boss = TestClient(app)
    sign_up(boss, "boss@example.com")
    assert boss.get("/api/me").json()["admin"] is True
    ov = boss.get("/api/admin/overview").json()
    assert {u["email"] for u in ov["users"]} == {"user@example.com", "boss@example.com"} and ov["mail"] == "outbox" and "jobs" in ov
    assert "password" not in str(ov).lower() and "token" not in str(ov).lower()
    assert boss.post(f"/api/admin/users/{uid}/credits", json={"minutes": 25}).json() == {"credit_minutes": 25.0}
    assert boss.post(f"/api/admin/users/{uid}/plan", json={"plan": "pro"}).json() == {"plan": "pro"}
    assert boss.post(f"/api/admin/users/{uid}/plan", json={"plan": "nope"}).status_code == 422
    assert c.post(f"/api/admin/users/{uid}/credits", json={"minutes": 1}).status_code == 404                  # a non-admin cannot, and gets "not found"
    assert boss.post("/api/admin/users/zzz/credits", json={"minutes": 1}).status_code == 404
    assert boss.post(f"/api/admin/users/{uid}/disabled", json={"disabled": True}).json() == {"disabled": True}
    assert c.get("/api/me").status_code == 401                                                              # their session ended at once
    n = len(app.state.outbox.sent)
    assert TestClient(app).post("/api/auth/code", json={"email": "user@example.com"}).status_code == 200
    assert len(app.state.outbox.sent) == n                                                                  # a disabled account gets no code
    me = a.by_email("boss@example.com")
    assert boss.post(f"/api/admin/users/{me.id}/disabled", json={"disabled": True}).status_code == 422       # not yourself
