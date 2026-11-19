"""Each account connects its own SocioBird (a pasted API key, stored encrypted); nobody posts through someone else's."""
import logging

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from clipcrow.service.accounts import Accounts
from clipcrow.service.api import create_app
from clipcrow.service.plans import Catalog, Plan
from clipcrow.service.publish import Channel, PublishError
from tests.mailhelp import Outbox, sign_up
from tests.test_service import FakePipeline

KEY_A, KEY_B = "sb_key_of_person_A_123456", "sb_key_of_person_B_654321"


class FakeSB:
    def __init__(self, key, channels, refuse=False):
        self.key, self._channels, self.refuse = key, channels, refuse

    def channels(self):
        if self.refuse or self.key not in (KEY_A, KEY_B, "operator-key-value"):
            raise PublishError("SocioBird refused the request (401).")
        return self._channels[self.key]


CHANNELS = {KEY_A: [Channel("a-yt", "A's YouTube", "youtube", "")], KEY_B: [Channel("b-ig", "B's Instagram", "instagram", ""), Channel("b-tt", "B's TikTok", "tiktok", "")],
            "operator-key-value": [Channel("op-yt", "Operator YouTube", "youtube", "")]}


def build(tmp_path, shared=False, operator=False):
    a = Accounts(tmp_path / "data" / "clipcrow.db")
    cat = Catalog(plans={"free": Plan("free", "Free", 100, 7, 3)})
    state = {"refuse": set()}
    factory = lambda key: FakeSB(key, CHANNELS, refuse=key in state["refuse"])
    app = create_app(tmp_path / "data", FakePipeline(), accounts=a, mailer=Outbox(), catalog=cat, sociobird_factory=factory,
                     sociobird=FakeSB("operator-key-value", CHANNELS) if operator else None, sociobird_shared=shared)
    ca, cb = TestClient(app), TestClient(app)
    sign_up(ca, "a@example.com")
    sign_up(cb, "b@example.com")
    return ca, cb, a, app, state


def test_a_person_pastes_their_key_it_is_tried_first_and_stored_encrypted_and_never_shown_again(tmp_path):
    ca, cb, a, app, _ = build(tmp_path)
    assert ca.get("/api/sociobird").json() == {"connected": False, "kind": "", "label": "", "since": "", "last_error": "", "shared": False, "oauth": False}
    assert ca.get("/api/publish/channels").status_code == 409                                    # nothing connected yet
    r = ca.post("/api/sociobird/key", json={"key": f"  {KEY_A}  "})
    assert r.status_code == 200 and r.json()["connected"] is True and r.json()["kind"] == "key" and r.json()["label"] == "1 channel"
    assert KEY_A not in r.text and KEY_A not in ca.get("/api/sociobird").text
    raw = b"".join(f.read_bytes() for f in (tmp_path / "data").glob("clipcrow.db*"))
    assert KEY_A.encode() not in raw                                                              # the database never holds the key itself
    assert a.link_token(a.by_email("a@example.com").id) == KEY_A                                 # but the server can use it
    assert [c["id"] for c in ca.get("/api/publish/channels").json()] == ["a-yt"]


def test_a_key_that_socioBird_refuses_is_not_stored_and_odd_keys_are_refused(tmp_path):
    ca, _, a, _, _ = build(tmp_path)
    r = ca.post("/api/sociobird/key", json={"key": "not-a-real-key-12345"})
    assert r.status_code == 422 and "did not accept that key" in r.json()["detail"] and a.get_link(a.by_email("a@example.com").id) is None
    for bad in ("short", "has a space inside key", "line\nbreak-in-the-key", "x" * 400):
        assert ca.post("/api/sociobird/key", json={"key": bad}).status_code == 422
    assert TestClient(ca.app).post("/api/sociobird/key", json={"key": KEY_A}).status_code == 401


def test_every_person_uses_only_their_own_connection_and_never_the_operators(tmp_path):
    ca, cb, a, app, _ = build(tmp_path, shared=False, operator=True)
    ca.post("/api/sociobird/key", json={"key": KEY_A})
    cb.post("/api/sociobird/key", json={"key": KEY_B})
    assert [c["id"] for c in ca.get("/api/publish/channels").json()] == ["a-yt"]
    assert [c["id"] for c in cb.get("/api/publish/channels").json()] == ["b-ig", "b-tt"]
    cb.delete("/api/sociobird")
    r = cb.get("/api/publish/channels")
    assert r.status_code == 409 and "operator" not in r.text.lower()                              # a person without a connection never falls back to the operator's channels
    assert [c["id"] for c in ca.get("/api/publish/channels").json()] == ["a-yt"]                  # and A is untouched


def test_shared_posting_is_still_there_for_people_without_their_own(tmp_path):
    ca, cb, _, _, _ = build(tmp_path, shared=True, operator=True)
    assert [c["id"] for c in cb.get("/api/publish/channels").json()] == ["op-yt"] and ca.get("/api/sociobird").json()["shared"] is True
    ca.post("/api/sociobird/key", json={"key": KEY_A})
    assert [c["id"] for c in ca.get("/api/publish/channels").json()] == ["a-yt"]                  # their own beats the shared one


def test_a_refused_key_at_use_time_says_reconnect_and_is_remembered(tmp_path):
    ca, _, a, _, state = build(tmp_path)
    ca.post("/api/sociobird/key", json={"key": KEY_A})
    state["refuse"].add(KEY_A)                                                                    # revoked in SocioBird, or the plan dropped API access
    r = ca.get("/api/publish/channels")
    assert r.status_code == 409 and "Reconnect it on your Account page" in r.json()["detail"]
    assert "refused" in ca.get("/api/sociobird").json()["last_error"]
    state["refuse"].clear()
    assert ca.get("/api/publish/channels").status_code == 200 and ca.get("/api/sociobird").json()["last_error"] == ""


def test_disconnect_and_account_deletion_remove_the_stored_key(tmp_path):
    ca, cb, a, _, _ = build(tmp_path)
    ca.post("/api/sociobird/key", json={"key": KEY_A})
    uid = a.by_email("a@example.com").id
    assert ca.delete("/api/sociobird").json()["connected"] is False and a.get_link(uid) is None and ca.get("/api/publish/channels").status_code == 409
    ca.post("/api/sociobird/key", json={"key": KEY_A})
    assert ca.request("DELETE", "/api/account", json={"email": "a@example.com"}).status_code == 200
    assert a._q("SELECT COUNT(*) AS n FROM sociobird_links").fetchone()["n"] == 0                 # it went with the account


def test_a_key_that_cannot_be_opened_with_this_servers_secret_means_connect_again(tmp_path):
    ca, _, a, _, _ = build(tmp_path)
    ca.post("/api/sociobird/key", json={"key": KEY_A})
    a._secret = b"\x01" * 32                                                                      # the secret was lost or changed
    assert a.link_token(a.by_email("a@example.com").id) is None and ca.get("/api/publish/channels").status_code == 409


def test_the_key_is_never_written_to_the_log(tmp_path, caplog):
    ca, _, _, _, state = build(tmp_path)
    with caplog.at_level(logging.DEBUG):
        ca.post("/api/sociobird/key", json={"key": KEY_A})
        state["refuse"].add(KEY_A)
        ca.get("/api/publish/channels")
        ca.post("/api/sociobird/key", json={"key": "wrong-key-for-the-log-12"})
    assert KEY_A not in caplog.text and "wrong-key-for-the-log-12" not in caplog.text


def test_secretbox_round_trip_and_separation():
    from clipcrow.service import secretbox
    s = b"s" * 32
    sealed = secretbox.seal(s, "sociobird", "the-key")
    assert sealed.startswith("v1:") and "the-key" not in sealed and secretbox.open_(s, "sociobird", sealed) == "the-key"
    assert secretbox.open_(s, "other-purpose", sealed) is None and secretbox.open_(b"t" * 32, "sociobird", sealed) is None
    assert secretbox.open_(s, "sociobird", "garbage") is None and secretbox.open_(s, "sociobird", "") is None


# --- connecting with SocioBird's OAuth ------------------------------------------------------------------

TOKEN_A = "pos_oauth_token_for_A_0123456789"


def oauth_app(tmp_path, monkeypatch, exchange=None):
    monkeypatch.setenv("SOCIOBIRD_OAUTH_CLIENT_ID", "pca_client_id_value")
    monkeypatch.setenv("SOCIOBIRD_OAUTH_CLIENT_SECRET", "pcs_the_client_secret_value")
    a = Accounts(tmp_path / "data" / "clipcrow.db")
    cat = Catalog(plans={"free": Plan("free", "Free", 100, 7, 3)})
    seen = {"codes": []}

    def swap(code):
        seen["codes"].append(code)
        if code == "bad":
            raise PublishError("SocioBird did not accept the sign-in.")
        return TOKEN_A
    chans = {TOKEN_A: [Channel("a-yt", "A's YouTube", "youtube", "")]}
    factory = lambda key: FakeSB(key, {**CHANNELS, **chans}) if key != TOKEN_A else type("S", (), {"channels": lambda self: chans[TOKEN_A]})()
    app = create_app(tmp_path / "data", FakePipeline(), accounts=a, mailer=Outbox(), catalog=cat, sociobird_factory=factory, sociobird_oauth_exchange=exchange or swap)
    ca, cb = TestClient(app, follow_redirects=False), TestClient(app, follow_redirects=False)
    sign_up(ca, "a@example.com")
    sign_up(cb, "b@example.com")
    return ca, cb, a, app, seen


def test_oauth_start_gives_an_authorise_address_with_a_random_state_and_no_secret(tmp_path, monkeypatch):
    ca, _, _, _, _ = oauth_app(tmp_path, monkeypatch)
    assert ca.get("/api/sociobird").json()["oauth"] is True
    url = ca.post("/api/sociobird/oauth/start").json()["url"]
    assert url.startswith("https://go.sociobird.app/oauth/authorize?") and "client_id=pca_client_id_value" in url and "response_type=code" in url and "state=" in url
    assert "pcs_" not in url and "secret" not in url
    assert ca.post("/api/sociobird/oauth/start").json()["url"] != url                           # a new state each time


def test_oauth_is_off_until_the_operator_has_registered_the_app(tmp_path, monkeypatch):
    monkeypatch.delenv("SOCIOBIRD_OAUTH_CLIENT_ID", raising=False)
    ca, _, _, _, _ = build(tmp_path)
    assert ca.get("/api/sociobird").json()["oauth"] is False and ca.post("/api/sociobird/oauth/start").status_code == 404


def test_the_callback_connects_the_person_who_started_even_without_a_session_cookie(tmp_path, monkeypatch):
    ca, cb, a, app, seen = oauth_app(tmp_path, monkeypatch)
    state = ca.post("/api/sociobird/oauth/start").json()["url"].split("state=")[1]
    browser = TestClient(app, follow_redirects=False)                                            # coming back from another site: SameSite=Strict sends no cookie
    r = browser.get("/api/sociobird/callback", params={"code": "good-code", "state": state})
    assert r.status_code == 303 and r.headers["location"] == "/dashboard?sociobird=connected#/account"
    link = a.get_link(a.by_email("a@example.com").id)
    assert link["kind"] == "oauth" and link["label"] == "1 channel" and a.get_link(a.by_email("b@example.com").id) is None
    assert a.link_token(a.by_email("a@example.com").id) == TOKEN_A and TOKEN_A not in ca.get("/api/sociobird").text
    assert [c["id"] for c in ca.get("/api/publish/channels").json()] == ["a-yt"] and cb.get("/api/publish/channels").status_code == 409
    again = browser.get("/api/sociobird/callback", params={"code": "good-code", "state": state})
    assert again.headers["location"].endswith("sociobird=failed#/account") and seen["codes"] == ["good-code"]      # a state works once


def test_the_callback_refuses_a_wrong_expired_or_missing_state_and_a_denied_request(tmp_path, monkeypatch):
    ca, _, a, app, seen = oauth_app(tmp_path, monkeypatch)
    browser = TestClient(app, follow_redirects=False)
    for params in ({"code": "c", "state": "made-up"}, {"code": "c"}, {"state": "x"}, {"code": "c", "state": "", "error": "access_denied"}):
        assert browser.get("/api/sociobird/callback", params=params).headers["location"].endswith("sociobird=failed#/account")
    state = ca.post("/api/sociobird/oauth/start").json()["url"].split("state=")[1]
    a._q("UPDATE oauth_states SET expires_at = 1")
    assert browser.get("/api/sociobird/callback", params={"code": "c", "state": state}).headers["location"].endswith("sociobird=failed#/account")
    state = ca.post("/api/sociobird/oauth/start").json()["url"].split("state=")[1]
    assert browser.get("/api/sociobird/callback", params={"code": "bad", "state": state}).headers["location"].endswith("sociobird=failed#/account")
    assert a.get_link(a.by_email("a@example.com").id) is None and "c" not in seen["codes"]


def test_one_person_cannot_attach_their_authorisation_to_another_persons_account(tmp_path, monkeypatch):
    ca, cb, a, app, _ = oauth_app(tmp_path, monkeypatch)
    state_b = cb.post("/api/sociobird/oauth/start").json()["url"].split("state=")[1]             # B started it...
    TestClient(app, follow_redirects=False).get("/api/sociobird/callback", params={"code": "x", "state": state_b})   # ...anyone's browser may finish it, but it lands on B
    assert a.get_link(a.by_email("b@example.com").id) and a.get_link(a.by_email("a@example.com").id) is None


def test_the_client_secret_and_the_token_never_reach_a_response_or_the_log(tmp_path, monkeypatch, caplog):
    ca, _, a, app, _ = oauth_app(tmp_path, monkeypatch)
    with caplog.at_level(logging.DEBUG):
        state = ca.post("/api/sociobird/oauth/start").json()["url"].split("state=")[1]
        r = TestClient(app, follow_redirects=False).get("/api/sociobird/callback", params={"code": "good", "state": state})
        s = ca.get("/api/sociobird").text + r.text + str(r.headers)
    for secret in ("pcs_the_client_secret_value", TOKEN_A):
        assert secret not in s and secret not in caplog.text


def test_the_real_exchange_posts_the_code_and_the_secret_to_the_token_address(tmp_path, monkeypatch):
    import io
    import json
    import urllib.request
    monkeypatch.setenv("SOCIOBIRD_OAUTH_CLIENT_ID", "pca_client_id_value")
    monkeypatch.setenv("SOCIOBIRD_OAUTH_CLIENT_SECRET", "pcs_the_client_secret_value")
    seen = {}

    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_open(req, timeout=0):
        seen["url"], seen["body"], seen["method"] = req.full_url, json.loads(req.data), req.get_method()
        return R(json.dumps({"access_token": TOKEN_A, "token_type": "bearer"}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    a = Accounts(tmp_path / "data" / "clipcrow.db")
    app = create_app(tmp_path / "data", FakePipeline(), accounts=a, mailer=Outbox(), catalog=Catalog(plans={"free": Plan("free", "Free", 100, 7, 3)}),
                     sociobird_factory=lambda key: type("S", (), {"channels": lambda self: []})())
    ca = TestClient(app, follow_redirects=False)
    sign_up(ca, "a@example.com")
    state = ca.post("/api/sociobird/oauth/start").json()["url"].split("state=")[1]
    r = ca.get("/api/sociobird/callback", params={"code": "the-code", "state": state})
    assert r.headers["location"].endswith("sociobird=connected#/account")
    assert seen["url"] == "https://go.sociobird.app/api/oauth/token" and seen["method"] == "POST"
    assert seen["body"] == {"grant_type": "authorization_code", "code": "the-code", "client_id": "pca_client_id_value", "client_secret": "pcs_the_client_secret_value"}
