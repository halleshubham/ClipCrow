"""Razorpay: subscriptions (plans) and payment links (credit packs). Nothing here is tried against the live service from
the build environment (no credentials there): the request shapes follow Razorpay's documented REST API and the tests use
a fake HTTP function and signatures computed the documented way (HMAC-SHA256 of the raw body with the webhook secret).

Trust model:
  * A plan change or a credit top-up happens ONLY from a webhook whose signature verifies against the RAW request body.
    The browser coming back from checkout proves nothing and changes nothing.
  * What a payment is worth comes from OUR catalog (plan id, pack id), never from data in the request; a payment whose
    amount does not match the pack is ignored.
  * Every event is applied once (event id, with a body hash as fallback).
  * The Razorpay key stays on the server. Notes sent to Razorpay carry only our user id and a pack/plan id.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from .accounts import Accounts
from .plans import Catalog

log = logging.getLogger("clipcrow.service")
API = "https://api.razorpay.com/v1"
GRACE = timedelta(days=2)             # a renewal webhook can arrive a little after the period ends


class BillingError(RuntimeError):
    """The payment service refused or could not be reached. The message is safe to show a person."""


class Razorpay:
    def __init__(self, key_id: str, key_secret: str, webhook_secret: str, *,
                 request: Callable[[str, str, dict | None], dict] | None = None):
        if not (key_id and key_secret and webhook_secret):
            raise ValueError("Razorpay needs a key id, a key secret and a webhook secret")
        self.key_id, self._secret, self._hook = key_id, key_secret, webhook_secret
        self._request = request or self._http

    @classmethod
    def from_env(cls) -> Optional["Razorpay"]:
        k, s, w = (os.environ.get(n, "") for n in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET"))
        return cls(k, s, w) if k and s and w else None

    # --- calls ------------------------------------------------------------------------------------
    def _http(self, method: str, path: str, body: dict | None) -> dict:
        auth = base64.b64encode(f"{self.key_id}:{self._secret}".encode()).decode()
        req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:                    # noqa: S310 (fixed https endpoint)
                return json.loads(r.read(2_000_000))
        except urllib.error.HTTPError as e:
            log.warning("razorpay answered %s for %s %s", e.code, method, path)
            raise BillingError("The payment service refused the request.") from None
        except (urllib.error.URLError, TimeoutError, ValueError):
            raise BillingError("The payment service could not be reached. Try again in a minute.") from None

    def create_subscription(self, razorpay_plan_id: str, user_id: str, plan_id: str) -> dict:
        res = self._request("POST", "/subscriptions", {
            "plan_id": razorpay_plan_id, "total_count": 120, "customer_notify": 1,
            "notes": {"user_id": user_id, "plan": plan_id}})
        if not res.get("id") or not res.get("short_url"):
            raise BillingError("The payment service did not return a checkout link.")
        return {"id": res["id"], "url": res["short_url"]}

    def cancel_subscription(self, subscription_id: str, *, at_cycle_end: bool = True) -> None:
        self._request("POST", f"/subscriptions/{subscription_id}/cancel", {"cancel_at_cycle_end": 1 if at_cycle_end else 0})

    def create_payment_link(self, amount_paise: int, description: str, user_id: str, pack_id: str) -> dict:
        res = self._request("POST", "/payment_links", {
            "amount": amount_paise, "currency": "INR", "description": description[:200], "accept_partial": False,
            "reminder_enable": False, "notes": {"user_id": user_id, "pack": pack_id}})
        if not res.get("id") or not res.get("short_url"):
            raise BillingError("The payment service did not return a payment link.")
        return {"id": res["id"], "url": res["short_url"]}

    # --- webhooks ---------------------------------------------------------------------------------
    def verify(self, body: bytes, signature: str) -> bool:
        want = hmac.new(self._hook.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(want, (signature or "").strip())


def _iso(epoch) -> Optional[str]:
    try:
        return (datetime.fromtimestamp(int(epoch), timezone.utc) + GRACE).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def apply_event(event: dict, event_id: str, accounts: Accounts, catalog: Catalog) -> str:
    """Apply one verified webhook event. Returns a short word for the log: applied | duplicate | ignored."""
    if not accounts.first_time(event_id):
        return "duplicate"
    name = event.get("event", "")
    payload = event.get("payload") or {}
    if name.startswith("subscription."):
        sub = (payload.get("subscription") or {}).get("entity") or {}
        sub_id, rz_plan = sub.get("id"), sub.get("plan_id")
        user = accounts.by_subscription(sub_id) if sub_id else None
        if user is None:
            uid = (sub.get("notes") or {}).get("user_id")
            try:
                user = accounts.get(uid) if uid else None
            except KeyError:
                user = None
            if user is not None and user.subscription and user.subscription != sub_id:
                return "ignored"                                        # a different subscription is already attached
        if user is None or not sub_id:
            return "ignored"
        if name in ("subscription.activated", "subscription.charged", "subscription.resumed", "subscription.authenticated"):
            ours = next((p for p in catalog.plans.values() if p.razorpay_plan_id and p.razorpay_plan_id == rz_plan), None)
            if ours is None:
                return "ignored"                                        # a Razorpay plan we do not sell
            if name == "subscription.authenticated":
                return "ignored"                                        # not paid yet
            accounts.set_plan(user.id, ours.id, until=_iso(sub.get("current_end")), subscription=sub_id)
            return "applied"
        if name in ("subscription.halted", "subscription.completed", "subscription.expired"):
            accounts.set_plan(user.id, "free", until=None)
            accounts.clear_subscription(user.id)
            return "applied"
        if name == "subscription.cancelled":
            accounts.clear_subscription(user.id)                        # the plan runs on until plan_until, then lapses
            return "applied"
        return "ignored"
    if name == "payment_link.paid":
        link = (payload.get("payment_link") or {}).get("entity") or {}
        notes = link.get("notes") or {}
        pack = catalog.packs.get(notes.get("pack", ""))
        if pack is None or link.get("status") != "paid" or int(link.get("amount_paid") or 0) != pack.amount_paise:
            return "ignored"                                            # unknown pack, not paid, or the wrong amount
        if not accounts.first_time(f"link:{link.get('id')}"):
            return "duplicate"
        try:
            accounts.get(notes.get("user_id", ""))
        except KeyError:
            return "ignored"
        accounts.add_credits(notes["user_id"], pack.minutes)
        return "applied"
    return "ignored"
