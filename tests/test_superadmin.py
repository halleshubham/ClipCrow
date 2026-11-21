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
    r = boss.post(f"/api/admin/users/{uid}/credits", json={"minutes": -(have + 5)})
    assert r.status_code == 422 and "only" in r.json()["detail"] and a.get(uid).credits == have
    assert boss.post(f"/api/admin/users/{uid}/credits", json={"minutes": 0}).status_code == 422


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
    assert r.status_code == 403 and "signup_bonus_minutes" in r.json()["detail"]
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
