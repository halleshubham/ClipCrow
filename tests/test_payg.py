"""One plan: pay as you go. A prepaid balance in minutes, recharged with any amount, a bonus for big recharges, free minutes once."""
import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from clipcrow.service.accounts import Accounts
from clipcrow.service.api import create_app
from clipcrow.service.plans import Catalog, Payg, Plan, load_catalog
from tests.mailhelp import Outbox, sign_up
from tests.test_billing import FakeRZ, hook, link_event
from tests.test_service import FakePipeline, upload, video_bytes, wait  # noqa: F401


def payg_catalog(**kw):
    return Catalog(plans={"free": Plan("free", "Pay as you go", 0, 30, 7)}, payg=Payg(**kw))


@pytest.fixture
def env(tmp_path):
    rz = FakeRZ()
    a = Accounts(tmp_path / "data" / "clipcrow.db")
    app = create_app(tmp_path / "data", FakePipeline(), accounts=a, mailer=Outbox(), catalog=payg_catalog(), razorpay=rz, public_url="https://clipcrow.example")
    c = TestClient(app)
    sign_up(c, "a@example.com")
    return c, a, rz, app


# --- the arithmetic ---------------------------------------------------------------------------------

def test_minutes_come_from_the_amount_paid_with_a_bonus_for_big_recharges():
    g = Payg()                                                      # Rs 8 a minute; +5% from Rs 2000, +10% from Rs 5000
    assert g.minutes_for(30_000) == 37.5 and g.minutes_for(10_000) == 12.5
    assert g.minutes_for(200_000) == 262.5 and g.minutes_for(500_000) == 687.5
    assert g.minutes_for(199_900) == 249.8                          # just under the tier: no bonus
    assert g.minutes_for(10_050) == 12.5                            # rounded down to a tenth of a minute
    assert Payg(price_paise_per_minute=1500).minutes_for(30_000) == 20.0


def test_the_public_view_has_rupees_and_quick_recharges_inside_the_limits():
    v = Payg().view()
    assert v["price_per_minute"] == 8.0 and v["min_recharge"] == 100 and v["signup_bonus_minutes"] == 10 and v["min_job_minutes"] == 3
    assert [q["rupees"] for q in v["quick"]] == [300, 500, 1000, 2000, 5000] and v["quick"][3]["minutes"] == 262.5
    assert "key" not in json.dumps(v).lower()


def test_the_default_catalog_is_one_pay_as_you_go_plan_and_a_bad_payg_is_refused(tmp_path):
    c = load_catalog()
    assert list(c.plans) == ["free"] and c.plan("free").monthly_minutes == 0 and c.payg and c.payg.price_paise_per_minute == 800
    bad = tmp_path / "p.json"
    bad.write_text(json.dumps({"plans": [{"id": "free", "name": "x", "monthly_minutes": 0, "retention_days": 1, "source_days": 1}],
                               "payg": {"price_paise_per_minute": 0}}))
    with pytest.raises(ValueError, match="payg"):
        load_catalog(bad)


# --- recharging ----------------------------------------------------------------------------------------

def test_a_recharge_makes_a_payment_link_for_exactly_the_amount_asked(env):
    c, a, rz, _ = env
    r = c.post("/api/billing/recharge", json={"rupees": 500})
    assert r.status_code == 200 and r.json() == {"url": "https://rzp.io/i/link1", "minutes": 62.5}
    method, path, body = rz.calls[-1]
    assert path == "/payment_links" and body["amount"] == 50_000 and body["notes"]["pack"] == "custom" and "minutes" not in body["notes"]
    assert c.post("/api/billing/recharge", json={"rupees": 99}).status_code == 422                # below the minimum
    assert c.post("/api/billing/recharge", json={"rupees": 20001}).status_code == 422             # above the maximum
    assert c.post("/api/billing/recharge", json={"rupees": 0}).status_code == 422
    assert TestClient(c.app).post("/api/billing/recharge", json={"rupees": 500}).status_code == 401


def test_the_minutes_are_added_only_by_the_verified_payment_and_only_for_what_was_paid(env):
    c, a, _, app = env
    uid = a.by_email("a@example.com").id
    start = a.get(uid).credits                                                                    # the free minutes
    assert hook(c, link_event(uid, pack="custom", amount=200_000), event_id="e1").json()["status"] == "applied"
    assert a.get(uid).credits == pytest.approx(start + 262.5)                                     # Rs 2000 + 5%
    assert hook(c, link_event(uid, pack="custom", amount=200_000), event_id="e2").json()["status"] == "duplicate"   # the same link again
    assert a.get(uid).credits == pytest.approx(start + 262.5)
    assert hook(c, link_event(uid, pack="custom", amount=5_000, link_id="l2"), event_id="e3").json()["status"] == "ignored"     # below the minimum
    assert hook(c, link_event(uid, pack="custom", amount=200_000, status="created", link_id="l3"), event_id="e4").json()["status"] == "ignored"
    forged = link_event(uid, pack="custom", amount=10_000, link_id="l4")
    forged["payload"]["payment_link"]["entity"]["notes"]["minutes"] = 99999                        # a number in the notes is never read
    hook(c, forged, event_id="e5")
    assert a.get(uid).credits == pytest.approx(start + 262.5 + 12.5)
    assert [m.kind for m in app.state.outbox.sent if m.kind.startswith("billing")] == ["billing-paid", "billing-paid"]


def test_a_new_account_gets_the_free_minutes_once_and_the_welcome_mail_says_so(env):
    c, a, _, app = env
    uid = a.by_email("a@example.com").id
    assert a.get(uid).credits == 10.0 and c.get("/api/me").json()["left_minutes"] == 10.0
    sign_up(TestClient(app), "a@example.com")                                                      # signing in again adds nothing
    assert a.get(uid).credits == 10.0
    mail = next(m for m in app.state.outbox.sent if m.kind == "welcome")
    assert "10 free minutes" in mail.text and "never expire" in mail.text and "Free plan" not in mail.text


def test_a_job_takes_one_minute_of_balance_per_minute_of_video_and_a_short_balance_says_how_to_recharge(env, video_bytes):
    c, a, _, _ = env
    uid = a.by_email("a@example.com").id
    left = c.get("/api/me").json()["left_minutes"]
    wait(c, upload(c, video_bytes).json()["id"])
    assert c.get("/api/me").json()["left_minutes"] < left                                          # a minute (rounded up) was taken
    a._q("UPDATE users SET credits = 0 WHERE id=?", (uid,))
    r = upload(c, video_bytes)
    assert r.status_code == 402 and "Recharge on your Account page" in r.json()["detail"] and "plan" not in r.json()["detail"].lower()


def test_the_price_is_public_and_on_the_config(env):
    c, _, _, app = env
    anon = TestClient(app)
    p = anon.get("/api/pricing").json()
    assert p["payg"]["price_per_minute"] == 8.0 and p["plan"]["name"] == "Pay as you go"
    assert anon.get("/api/config").json()["pricing"]["price_per_minute"] == 8.0
    assert c.get("/api/billing").json()["payg"]["quick"]


# --- the price is configurable, short jobs have a minimum charge, and the margin can be read per job ---------------------------

def test_the_price_the_minimum_and_the_bonus_come_from_the_environment_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPCROW_PRICE_PER_MINUTE", "6.5")
    monkeypatch.setenv("CLIPCROW_MIN_JOB_MINUTES", "5")
    monkeypatch.setenv("CLIPCROW_SIGNUP_BONUS_MINUTES", "2")                     # ignored: only a superadmin sets the free minutes, on the settings page
    g = load_catalog().payg
    assert g.price_paise_per_minute == 650 and g.min_job_minutes == 5 and g.signup_bonus_minutes == Payg().signup_bonus_minutes
    monkeypatch.setenv("CLIPCROW_MIN_JOB_MINUTES", "0")
    with pytest.raises(ValueError, match="payg"):
        load_catalog()


def test_a_short_video_is_charged_the_minimum_and_a_longer_one_its_real_length(tmp_path, video_bytes):
    a = Accounts(tmp_path / "data" / "clipcrow.db")
    app = create_app(tmp_path / "data", FakePipeline(), accounts=a, mailer=Outbox(), catalog=payg_catalog(min_job_minutes=30))
    c = TestClient(app)
    sign_up(c, "a@example.com")
    uid = a.by_email("a@example.com").id
    refused = upload(c, video_bytes)                                                              # the 10 free minutes do not cover a 30 minute minimum
    assert refused.status_code == 402 and "Recharge" in refused.json()["detail"]
    a.add_credits(uid, 100)
    before = a.get(uid).credits
    jid = wait(c, upload(c, video_bytes).json()["id"])["id"]
    assert c.get(f"/api/jobs/{jid}").json()["reserved_minutes"] == 30 and a.get(uid).credits == pytest.approx(before - 30)
    assert c.get("/api/pricing").json()["payg"]["min_job_minutes"] == 30


def test_the_default_minimum_never_charges_less_than_the_real_length(env, video_bytes):
    c, a, _, _ = env
    jid = wait(c, upload(c, video_bytes).json()["id"])["id"]
    job = c.get(f"/api/jobs/{jid}").json()
    assert job["reserved_minutes"] >= 3 and job["reserved_minutes"] >= -(-job["source"]["duration"] // 60)


def test_the_margin_maths_and_the_suggested_price_and_minimum():
    from clipcrow.service.economics import economics, suggest_min_minutes, suggest_price
    e = economics(revenue=480.0, cost=190.0, markup_target=100)                                      # a 60 minute job at Rs 8
    assert e["markup_percent"] == 153 and e["profit_share_percent"] == 60 and e["below_target"] is False
    low = economics(revenue=24.0, cost=14.0, markup_target=100)
    assert low["markup_percent"] == 71 and low["below_target"] is True
    assert economics(0, 5, 50)["below_target"] is False and economics(10, 0, 50)["markup_percent"] is None
    assert suggest_price([2.4, 2.6, 2.6, 2.9, 3.3, 3.9], 100) == 7.0                                 # p75 = 3.3, x 2 = 6.6, rounded up to a half rupee
    assert suggest_price([], 100) is None
    assert suggest_min_minutes([5, 5.5, 6, 6.6], 8.0, 100) == 2 and suggest_min_minutes([], 8.0, 100) == 1


def test_the_operator_sees_money_per_job_and_the_target_and_a_user_does_not(tmp_path, monkeypatch, video_bytes):
    monkeypatch.setenv("CLIPCROW_ADMINS", "boss@example.com")
    monkeypatch.setenv("CLIPCROW_MARGIN_PERCENT", "100")
    a = Accounts(tmp_path / "data" / "clipcrow.db")
    app = create_app(tmp_path / "data", FakePipeline(), accounts=a, mailer=Outbox(), catalog=payg_catalog())
    user, boss = TestClient(app), TestClient(app)
    sign_up(user, "user@example.com")
    sign_up(boss, "boss@example.com")
    jid = wait(user, upload(user, video_bytes).json()["id"])["id"]
    assert "economics" not in user.get(f"/api/jobs/{jid}/usage").json()                             # people never see what a job cost us
    econ = boss.get(f"/api/jobs/{jid}/usage")
    if econ.status_code == 200:                                                                      # (the fake pipeline writes a plan)
        e = econ.json().get("economics")
        assert e is None or (e["revenue"] == 24.0 and e["target_markup_percent"] == 100)
    money = boss.get("/api/admin/overview").json()["money"]
    assert money["price_per_minute"] == 8.0 and money["min_job_minutes"] == 3 and money["target_markup_percent"] == 100
    assert {"totals", "jobs", "suggested_price_per_minute", "suggested_min_job_minutes", "note"} <= set(money)
    assert user.get("/api/admin/overview").status_code == 404
