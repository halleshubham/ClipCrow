"""Fixes from the security review: a failed credit never loses a paid recharge, odd signature bytes are a clean refusal, a damaged secret file stops the service,
child processes never see the service's keys."""
import hashlib
import hmac
import json

import pytest

pytest.importorskip("fastapi")

from clipcrow.render import child_env
from clipcrow.service.accounts import Accounts
from clipcrow.service.billing import apply_event
from tests.test_billing import SECRET, FakeRZ, link_event
from tests.test_payg import payg_catalog


def test_a_failed_credit_lets_the_providers_retry_apply_instead_of_answering_duplicate(tmp_path, monkeypatch):
    a = Accounts(tmp_path / "c.db")
    u = a.ensure_user("a@example.com")
    ev = link_event(u.id, pack="custom", amount=30000)
    boom = {"on": True}
    real = a.add_credits

    def flaky(uid, m):
        if boom["on"]:
            raise RuntimeError("database hiccup")
        return real(uid, m)
    monkeypatch.setattr(a, "add_credits", flaky)
    with pytest.raises(RuntimeError):
        apply_event(ev, "e1", a, payg_catalog())
    boom["on"] = False
    assert apply_event(ev, "e1", a, payg_catalog()) == "applied"          # the retry is applied, the money is not lost
    assert apply_event(ev, "e1", a, payg_catalog()) == "duplicate"


def test_a_non_ascii_signature_is_refused_not_an_error():
    rz = FakeRZ()
    assert rz.verify(b"{}", "é" * 10) is False
    good = hmac.new(SECRET.encode(), b"{}", hashlib.sha256).hexdigest()
    assert rz.verify(b"{}", good) is True


@pytest.mark.parametrize("content", ["", "abcd", "zz" * 32])
def test_a_damaged_secret_file_stops_the_service_instead_of_signing_with_a_known_key(tmp_path, monkeypatch, content):
    monkeypatch.delenv("CLIPCROW_SECRET_KEY", raising=False)
    f = tmp_path / "clipcrow.secret"
    f.write_text(content)
    with pytest.raises(RuntimeError, match="damaged"):
        Accounts(tmp_path / "c.db", secret_file=f)._key()


def test_child_processes_do_not_inherit_the_service_secrets(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "CLIPCROW_WORKER_TOKEN", "RAZORPAY_KEY_SECRET", "DATABASE_URL", "CLIPCROW_R2_SECRET_ACCESS_KEY", "RESEND_API_KEY"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("CLIPCROW_HF_BIN", "hyperframes")
    env = child_env()
    assert not any(k in env for k in ("ANTHROPIC_API_KEY", "CLIPCROW_WORKER_TOKEN", "RAZORPAY_KEY_SECRET", "DATABASE_URL", "CLIPCROW_R2_SECRET_ACCESS_KEY", "RESEND_API_KEY"))
    assert env["CLIPCROW_HF_BIN"] == "hyperframes" and "PATH" in env
