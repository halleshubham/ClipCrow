"""A superadmin (an address in CLIPCROW_ADMINS) manages people's extra minutes, with a ledger; the free minutes for a new account are set only by a superadmin, on the page."""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from tests.mailhelp import sign_up
from tests.test_ops import client


def _people(tmp_path, monkeypatch):
    c, a, app = client(tmp_path, monkeypatch, admins="boss@example.com")
    boss, helper, user = TestClient(app), TestClient(app), TestClient(app)
    sign_up(boss, "boss@example.com")
    sign_up(helper, "helper@example.com")
    sign_up(user, "user@example.com")
    assert boss.put("/api/admin/settings", json={"values": {"extra_admins": "helper@example.com"}}).status_code == 200
    return boss, helper, user, a


def test_a_superadmin_adds_and_removes_minutes_and_every_change_is_in_the_ledger(tmp_path, monkeypatch):
    boss, helper, user, a = _people(tmp_path, monkeypatch)
    uid = a.by_email("user@example.com").id
    base = a.get(uid).credits
    assert boss.post(f"/api/admin/users/{uid}/credits", json={"minutes": 60, "note": "paid by UPI ref 1"}).json() == {"credit_minutes": round(base + 60, 1)}
    assert boss.post(f"/api/admin/users/{uid}/credits", json={"minutes": -10, "note": "added twice"}).json()["credit_minutes"] == round(base + 50, 1)
    h = boss.get(f"/api/admin/users/{uid}/credits").json()
    assert [(r["delta"], r["note"], r["by"]) for r in h["history"]] == [(-10.0, "added twice", "boss@example.com"), (60.0, "paid by UPI ref 1", "boss@example.com")]
    assert h["balance"] == round(base + 50, 1)


def test_removing_more_than_the_balance_is_refused_and_zero_is_refused(tmp_path, monkeypatch):
    boss, _, _, a = _people(tmp_path, monkeypatch)
    uid = a.by_email("user@example.com").id
    have = a.get(uid).credits
    r = boss.post(f"/api/admin/users/{uid}/credits", json={"minutes": -(have + 5), "note": "too much"})
    assert r.status_code == 422 and "only" in r.json()["detail"] and a.get(uid).credits == have
    assert boss.post(f"/api/admin/users/{uid}/credits", json={"minutes": 0, "note": "nothing"}).status_code == 422


def test_an_ordinary_operator_can_look_at_the_ledger_but_not_change_money(tmp_path, monkeypatch):
    boss, helper, user, a = _people(tmp_path, monkeypatch)
    uid = a.by_email("user@example.com").id
    r = helper.post(f"/api/admin/users/{uid}/credits", json={"minutes": 5})
    assert r.status_code == 403 and "superadmin" in r.json()["detail"].lower()
    assert helper.get(f"/api/admin/users/{uid}/credits").status_code == 200
    assert user.get(f"/api/admin/users/{uid}/credits").status_code == 404           # a plain user does not even see that it exists
    assert helper.get("/api/admin/overview").json()["superadmin"] is False and boss.get("/api/admin/overview").json()["superadmin"] is True


def test_the_free_minutes_are_a_superadmin_only_setting_and_the_environment_cannot_set_them(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPCROW_SIGNUP_BONUS_MINUTES", "999")
    boss, helper, _, _ = _people(tmp_path, monkeypatch)
    row = lambda c: next(s for s in c.get("/api/admin/settings").json()["settings"] if s["key"] == "signup_bonus_minutes")
    assert row(boss)["value"] != 999 and row(boss)["locked"] is False
    assert row(helper)["locked"] is True and row(helper)["superadmin"] is True
    r = helper.put("/api/admin/settings", json={"values": {"signup_bonus_minutes": 50}})
    assert r.status_code == 403 and "superadmin" in r.json()["detail"].lower()
    assert helper.delete("/api/admin/settings/signup_bonus_minutes").status_code == 403
    assert boss.put("/api/admin/settings", json={"values": {"signup_bonus_minutes": 25}}).status_code == 200
    assert row(boss)["value"] == 25
    assert boss.put("/api/admin/settings", json={"values": {"signup_bonus_minutes": 0}}).status_code == 200      # 0 turns the free trial off


def test_a_new_account_gets_the_bonus_the_superadmin_set(tmp_path, monkeypatch):
    from clipcrow.service.accounts import Accounts
    from clipcrow.service.api import create_app
    from tests.mailhelp import Outbox
    from tests.test_payg import payg_catalog
    from tests.test_service import FakePipeline
    monkeypatch.setenv("CLIPCROW_ADMINS", "boss@example.com")
    a = Accounts(tmp_path / "d" / "c.db")
    app = create_app(tmp_path / "d", FakePipeline(), accounts=a, mailer=Outbox(), catalog=payg_catalog(signup_bonus_minutes=10.0))
    boss = TestClient(app)
    sign_up(boss, "boss@example.com")
    assert boss.put("/api/admin/settings", json={"values": {"signup_bonus_minutes": 7}}).status_code == 200
    sign_up(TestClient(app), "newbie@example.com")
    assert a.by_email("newbie@example.com").credits == 7.0


# --- superadmin hygiene: least privilege, a reason for every money change, one audit trail, alerts, shorter sessions -------------------

def test_only_a_superadmin_changes_plans_and_settings_and_every_setting_is_locked_for_other_operators(tmp_path, monkeypatch):
    boss, helper, _, a = _people(tmp_path, monkeypatch)
    uid = a.by_email("user@example.com").id
    assert helper.post(f"/api/admin/users/{uid}/plan", json={"plan": "free"}).status_code == 403
    assert helper.put("/api/admin/settings", json={"values": {"max_active_jobs": 9}}).status_code == 403              # not only money settings: all of them
    assert helper.delete("/api/admin/settings/max_active_jobs").status_code == 403
    rows = helper.get("/api/admin/settings").json()["settings"]
    assert rows and all(r["locked"] for r in rows) and not any(r["locked"] for r in boss.get("/api/admin/settings").json()["settings"])
    assert boss.post(f"/api/admin/users/{uid}/plan", json={"plan": "free"}).status_code == 200


def test_a_money_change_needs_a_reason_and_is_capped(tmp_path, monkeypatch):
    boss, _, _, a = _people(tmp_path, monkeypatch)
    uid = a.by_email("user@example.com").id
    assert boss.post(f"/api/admin/users/{uid}/credits", json={"minutes": 5}).status_code == 422                       # no reason
    assert boss.post(f"/api/admin/users/{uid}/credits", json={"minutes": 5, "note": "x"}).status_code == 422          # a reason of a few characters at least
    assert boss.post(f"/api/admin/users/{uid}/credits", json={"minutes": 10001, "note": "too big"}).status_code == 422
    assert boss.post(f"/api/admin/users/{uid}/credits", json={"minutes": 10000, "note": "paid by bank transfer"}).status_code == 200


def test_every_privileged_action_lands_in_one_audit_trail_with_the_address_it_came_from(tmp_path, monkeypatch):
    boss, helper, user, a = _people(tmp_path, monkeypatch)
    uid = a.by_email("user@example.com").id
    boss.post(f"/api/admin/users/{uid}/credits", json={"minutes": 5, "note": "goodwill"})
    boss.post(f"/api/admin/users/{uid}/plan", json={"plan": "free"})
    helper.post(f"/api/admin/users/{uid}/disabled", json={"disabled": True})                                           # an ordinary operator may disable, and it is recorded
    helper.post(f"/api/admin/users/{uid}/disabled", json={"disabled": False})
    boss.put("/api/admin/settings", json={"values": {"max_active_jobs": 4}})
    boss.delete("/api/admin/settings/max_active_jobs")
    boss.post("/api/admin/jobs/claim", json={"email": "boss@example.com"})
    entries = boss.get("/api/admin/audit").json()["entries"]
    seen = {e["action"] for e in entries}
    assert {"credits", "plan", "disable", "enable", "settings", "settings-reset", "claim-jobs"} <= seen
    by = {e["action"]: e for e in entries}
    assert by["disable"]["by"] == "helper@example.com" and by["credits"]["by"] == "boss@example.com"
    assert "goodwill" in by["credits"]["note"] and "ip " in by["credits"]["note"] and by["credits"]["target"] == "user@example.com"
    assert helper.get("/api/admin/audit").status_code == 404                                                           # the trail is for superadmins only


def test_sensitive_changes_alert_the_operator_address_and_a_superadmin_session_is_shorter(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPCROW_ALERT_EMAIL", "ops@example.com")
    boss, helper, user, a = _people(tmp_path, monkeypatch)
    app = boss.app
    uid = a.by_email("user@example.com").id
    n = len(app.state.outbox.sent)
    boss.post(f"/api/admin/users/{uid}/credits", json={"minutes": 20, "note": "small gift"})
    assert len(app.state.outbox.sent) == n                                                                              # a small change is not a page
    boss.post(f"/api/admin/users/{uid}/credits", json={"minutes": 1500, "note": "annual plan paid in cash"})
    boss.put("/api/admin/settings", json={"values": {"extra_admins": ""}})
    mails = [m for m in app.state.outbox.sent[n:] if m.to == "ops@example.com"]
    assert any("credit" in m.subject.lower() for m in mails) and any("operators" in m.subject.lower() for m in mails)
    import time
    exp = a.db.q("SELECT s.expires_at FROM sessions s JOIN users u ON u.id=s.user_id WHERE u.email=?", ("boss@example.com",)).fetchone()["expires_at"]
    exp_user = a.db.q("SELECT s.expires_at FROM sessions s JOIN users u ON u.id=s.user_id WHERE u.email=?", ("user@example.com",)).fetchone()["expires_at"]
    assert exp - time.time() < 7.1 * 86400 and exp_user - time.time() > 29 * 86400
