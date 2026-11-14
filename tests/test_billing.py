"""Razorpay: signatures, webhooks (forged, repeated, tampered), checkout links, cancelling."""
import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from clipcrow.service.accounts import Accounts
from clipcrow.service.api import create_app
from clipcrow.service.billing import BillingError, Razorpay, apply_event
from clipcrow.service.plans import Catalog, CreditPack, Plan
from tests.test_service import FakePipeline, upload, video_bytes, wait  # noqa: F401

PW = "correct horse battery"
SECRET = "whsec_test_secret"
PRO_RZ = "plan_RZ123"


def catalog():
    return Catalog(plans={"free": Plan("free", "Free", 10, 7, 3),
                          "pro": Plan("pro", "Pro", 100, 30, 14, razorpay_plan_id=PRO_RZ, price_label="₹ test / month")},
                   packs={"p50": CreditPack("p50", 50, 29900, "50 extra minutes")})


class FakeRZ(Razorpay):
    """The real signature code with a fake network."""

    def __init__(self, fail=False):
        super().__init__("rzp_test_key", "key_secret_value", SECRET, request=self._fake)
        self.calls, self.fail = [], fail

    def _fake(self, method, path, body):
        self.calls.append((method, path, body))
        if self.fail:
            raise BillingError("The payment service could not be reached. Try again in a minute.")
        if path == "/subscriptions":
            return {"id": "sub_1", "short_url": "https://rzp.io/i/sub1"}
        if path == "/payment_links":
            return {"id": "plink_1", "short_url": "https://rzp.io/i/link1"}
        return {}


def sign(body: bytes, secret=SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def env(tmp_path):
    rz = FakeRZ()
    a = Accounts(tmp_path / "data" / "clipcrow.db")
    app = create_app(tmp_path / "data", FakePipeline(), accounts=a, catalog=catalog(), razorpay=rz)
    c = TestClient(app)
    assert c.post("/api/auth/register", json={"email": "a@example.com", "password": PW}).status_code == 200
    return c, a, rz


def hook(c, event, *, event_id="evt_1", secret=SECRET, body=None):
    raw = body if body is not None else json.dumps(event).encode()
    return c.post("/api/billing/razorpay/webhook", content=raw,
                  headers={"x-razorpay-signature": sign(raw, secret), "x-razorpay-event-id": event_id, "content-type": "application/json"})


def sub_event(name, uid, sub_id="sub_1", plan=PRO_RZ, end=None, notes=True):
    end = end or int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())
    ent = {"id": sub_id, "plan_id": plan, "status": "active", "current_end": end}
    if notes:
        ent["notes"] = {"user_id": uid}
    return {"event": name, "payload": {"subscription": {"entity": ent}}}


def link_event(uid, pack="p50", amount=29900, status="paid", link_id="plink_1"):
    return {"event": "payment_link.paid", "payload": {"payment_link": {"entity": {
        "id": link_id, "status": status, "amount_paid": amount, "notes": {"user_id": uid, "pack": pack}}}}}


# --- signatures ---------------------------------------------------------------------------------------

def test_the_signature_is_hmac_sha256_of_the_raw_body():
    rz = FakeRZ()
    body = b'{"event":"x"}'
    assert rz.verify(body, hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest())
    assert not rz.verify(body + b" ", sign(body)) and not rz.verify(body, "") and not rz.verify(body, sign(body, "other"))
    assert not rz.verify(body, sign(body).upper())                                    # hex digests are compared exactly


def test_razorpay_needs_all_three_secrets():
    for args in (("", "s", "w"), ("k", "", "w"), ("k", "s", "")):
        with pytest.raises(ValueError):
            Razorpay(*args)


def test_the_basic_auth_header_is_built_from_the_key_pair_only_for_razorpay(monkeypatch):
    seen = {}

    class R:
        def read(self, n):
            return b'{"id": "sub_9", "short_url": "https://x"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(req, timeout):
        seen.update(url=req.full_url, auth=req.get_header("Authorization"), body=req.data)
        return R()
    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    rz = Razorpay("rzp_key", "the_secret", "hook")
    out = rz.create_subscription(PRO_RZ, "user1", "pro")
    assert seen["url"] == "https://api.razorpay.com/v1/subscriptions" and out["url"] == "https://x"
    assert base64.b64decode(seen["auth"].split()[1]).decode() == "rzp_key:the_secret"
    sent = json.loads(seen["body"])
    assert sent["notes"] == {"user_id": "user1", "plan": "pro"} and "the_secret" not in seen["body"].decode()


# --- the webhook endpoint -----------------------------------------------------------------------------------------

def test_an_unsigned_or_wrongly_signed_webhook_changes_nothing(env):
    c, a, _ = env
    uid = a.authenticate("a@example.com", PW).id
    raw = json.dumps(sub_event("subscription.activated", uid)).encode()
    assert c.post("/api/billing/razorpay/webhook", content=raw).status_code == 400
    assert hook(c, None, body=raw, secret="wrong").status_code == 400
    assert a.get(uid).plan == "free"
    assert hook(c, None, body=b"not json").status_code == 400                           # signed junk is refused too
    assert c.post("/api/billing/razorpay/webhook", content=b"x" * 2_000_000, headers={"x-razorpay-signature": "x"}).status_code in (400, 413)


def test_a_paid_subscription_moves_the_user_to_the_plan_with_a_grace_period(env):
    c, a, _ = env
    uid = a.authenticate("a@example.com", PW).id
    end = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())
    assert hook(c, sub_event("subscription.activated", uid, end=end)).json() == {"status": "applied"}
    u = a.get(uid)
    assert (u.plan, u.subscription) == ("pro", "sub_1")
    assert u.plan_until > (datetime.now(timezone.utc) + timedelta(days=31)).isoformat()    # period end + 2 days of grace
    me = c.get("/api/me").json()
    assert me["plan"]["id"] == "pro" and me["left_minutes"] == 100 and me["subscribed"] is True
    later = int((datetime.now(timezone.utc) + timedelta(days=60)).timestamp())
    assert hook(c, sub_event("subscription.charged", uid, end=later), event_id="evt_2").json() == {"status": "applied"}
    assert a.get(uid).plan_until > u.plan_until                                       # a renewal extends it


def test_the_same_event_twice_is_applied_once(env):
    c, a, _ = env
    uid = a.authenticate("a@example.com", PW).id
    ev = link_event(uid)
    assert hook(c, ev, event_id="evt_x").json() == {"status": "applied"}
    assert hook(c, ev, event_id="evt_x").json() == {"status": "duplicate"}
    assert hook(c, ev, event_id="evt_y").json() == {"status": "duplicate"}               # a new event id for the SAME payment link
    assert a.get(uid).credits == 50


def test_credits_come_from_our_catalog_and_only_when_the_amount_matches(env):
    c, a, _ = env
    uid = a.authenticate("a@example.com", PW).id
    for i, ev in enumerate((link_event(uid, amount=100), link_event(uid, status="created"), link_event(uid, pack="unknown"),
                            link_event("no-such-user"), link_event(uid, pack="p50", amount=29900, link_id="l9"))):
        hook(c, ev, event_id=f"evt_{i}")
    assert a.get(uid).credits == 50                                                      # only the last, honest one counted


def test_events_for_other_plans_unknown_users_and_unpaid_states_are_ignored(env):
    c, a, _ = env
    uid = a.authenticate("a@example.com", PW).id
    assert hook(c, sub_event("subscription.activated", uid, plan="plan_other"), event_id="e1").json()["status"] == "ignored"
    assert hook(c, sub_event("subscription.activated", "ghost"), event_id="e2").json()["status"] == "ignored"
    assert hook(c, sub_event("subscription.authenticated", uid), event_id="e3").json()["status"] == "ignored"     # not paid yet
    assert hook(c, {"event": "payment.captured", "payload": {}}, event_id="e4").json()["status"] == "ignored"
    assert a.get(uid).plan == "free"


def test_a_subscription_cannot_be_hijacked_onto_another_account(env):
    c, a, _ = env
    mine = a.authenticate("a@example.com", PW).id
    hook(c, sub_event("subscription.activated", mine, sub_id="sub_mine"), event_id="e1")
    other = a.register("b@example.com", PW).id
    a.set_plan(other, "free")
    a._q("UPDATE users SET subscription='sub_other' WHERE id=?", (other,))
    # an event that names user B but carries user A's subscription id applies to A (the id is the key), never to B
    hook(c, sub_event("subscription.charged", other, sub_id="sub_mine"), event_id="e2")
    assert a.get(other).plan == "free" and a.get(mine).plan == "pro"


def test_ending_states_drop_to_free_cancelling_keeps_the_paid_period(env):
    c, a, _ = env
    uid = a.authenticate("a@example.com", PW).id
    hook(c, sub_event("subscription.activated", uid), event_id="e1")
    hook(c, sub_event("subscription.cancelled", uid), event_id="e2")
    u = a.get(uid)
    assert u.subscription is None and u.plan == "pro" and u.plan_until                  # paid through the end of the period
    hook(c, sub_event("subscription.activated", uid), event_id="e3")
    hook(c, sub_event("subscription.halted", uid), event_id="e4")
    u = a.get(uid)
    assert (u.plan, u.subscription, u.plan_until) == ("free", None, None)               # payments failed: free at once


# --- checkout, packs, cancelling --------------------------------------------------------------------------------------------

def test_subscribing_returns_a_hosted_checkout_link_and_changes_nothing_by_itself(env):
    c, a, rz = env
    uid = a.authenticate("a@example.com", PW).id
    info = c.get("/api/billing").json()
    assert info["enabled"] and info["current"] == "free" and [p["id"] for p in info["plans"] if p["can_subscribe"]] == ["pro"]
    r = c.post("/api/billing/subscribe", json={"plan": "pro"})
    assert r.status_code == 200 and r.json() == {"url": "https://rzp.io/i/sub1"}
    assert rz.calls[-1] == ("POST", "/subscriptions", {"plan_id": PRO_RZ, "total_count": 120, "customer_notify": 1,
                                                       "notes": {"user_id": uid, "plan": "pro"}})
    assert a.get(uid).plan == "free"                                                  # only the signed webhook upgrades
    for plan in ("free", "nope", ""):
        assert c.post("/api/billing/subscribe", json={"plan": plan}).status_code == 422


def test_a_subscriber_cannot_start_a_second_subscription_and_can_cancel(env):
    c, a, rz = env
    uid = a.authenticate("a@example.com", PW).id
    hook(c, sub_event("subscription.activated", uid), event_id="e1")
    assert c.post("/api/billing/subscribe", json={"plan": "pro"}).status_code == 409
    r = c.post("/api/billing/cancel")
    assert r.status_code == 200 and rz.calls[-1] == ("POST", "/subscriptions/sub_1/cancel", {"cancel_at_cycle_end": 1})
    assert "until the end of the period" in r.json()["note"]


def test_buying_minutes_uses_the_catalog_price_and_not_a_client_amount(env):
    c, a, rz = env
    uid = a.authenticate("a@example.com", PW).id
    r = c.post("/api/billing/credits", json={"pack": "p50", "amount": 1})
    assert r.status_code == 200 and r.json()["url"] == "https://rzp.io/i/link1"
    method, path, body = rz.calls[-1]
    assert (path, body["amount"], body["notes"]) == ("/payment_links", 29900, {"user_id": uid, "pack": "p50"})
    assert c.post("/api/billing/credits", json={"pack": "nope"}).status_code == 422
    assert a.get(uid).credits == 0                                                    # paying is what adds minutes, not asking


def test_payment_service_errors_are_a_502_with_a_plain_message(tmp_path):
    rz = FakeRZ(fail=True)
    a = Accounts(tmp_path / "d" / "x.db")
    c = TestClient(create_app(tmp_path / "d", FakePipeline(), accounts=a, catalog=catalog(), razorpay=rz))
    c.post("/api/auth/register", json={"email": "a@example.com", "password": PW})
    r = c.post("/api/billing/subscribe", json={"plan": "pro"})
    assert r.status_code == 424 and "could not be reached" in r.json()["detail"]


def test_billing_endpoints_need_a_session_and_the_webhook_needs_billing(tmp_path):
    rz = FakeRZ()
    a = Accounts(tmp_path / "d" / "x.db")
    c = TestClient(create_app(tmp_path / "d", FakePipeline(), accounts=a, catalog=catalog(), razorpay=rz))
    for path in ("/api/billing/subscribe", "/api/billing/credits", "/api/billing/cancel"):
        assert c.post(path, json={}).status_code == 401
    assert c.get("/api/billing").status_code == 401
    c2 = TestClient(create_app(tmp_path / "e", FakePipeline(), accounts=Accounts(tmp_path / "e" / "x.db"), catalog=catalog()))
    assert c2.post("/api/billing/razorpay/webhook", content=b"{}").status_code == 404
    assert c2.get("/api/config").json()["billing"] is False


# --- deleting an account stops the charges first --------------------------------------------------------------------------------

def test_deleting_an_account_cancels_the_subscription_first_and_stops_if_that_fails(tmp_path):
    rz = FakeRZ()
    a = Accounts(tmp_path / "d" / "x.db")
    c = TestClient(create_app(tmp_path / "d", FakePipeline(), accounts=a, catalog=catalog(), razorpay=rz))
    c.post("/api/auth/register", json={"email": "a@example.com", "password": PW})
    uid = a.authenticate("a@example.com", PW).id
    hook(c, sub_event("subscription.activated", uid), event_id="e1")
    rz.fail = True
    r = c.request("DELETE", "/api/account", json={"password": PW})
    assert r.status_code == 424 and a.count() == 1                                     # nothing was deleted
    rz.fail = False
    assert c.request("DELETE", "/api/account", json={"password": PW}).status_code == 200
    assert rz.calls[-1] == ("POST", "/subscriptions/sub_1/cancel", {"cancel_at_cycle_end": 0}) and a.count() == 0


def test_apply_event_is_a_plain_function_too(tmp_path):
    a = Accounts(tmp_path / "x.db")
    u = a.register("a@example.com", PW)
    assert apply_event(link_event(u.id), "e1", a, catalog()) == "applied"
    assert apply_event(link_event(u.id), "e1", a, catalog()) == "duplicate"
