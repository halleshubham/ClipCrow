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


# --- untrusted media ------------------------------------------------------------------------------------

def test_only_real_containers_pass_the_media_check(tmp_path):
    from clipcrow import mediaguard
    import subprocess
    ok = tmp_path / "ok.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=d=1", "-y", str(ok)], check=True)
    mediaguard.check_file(ok)
    playlist = tmp_path / "x.mp4"
    playlist.write_text("#EXTM3U\n#EXTINF:5,\nhttp://169.254.169.254/x.ts\n#EXT-X-ENDLIST\n")
    for bad in (playlist, ):
        with pytest.raises(mediaguard.NotMedia):
            mediaguard.check_file(bad)
    assert not mediaguard.looks_like_media(b"ffconcat version 1.0\nfile '/etc/passwd'\n")
    assert not mediaguard.looks_like_media(b"<svg xmlns='http://www.w3.org/2000/svg'/>" * 3)


def test_a_playlist_named_mp4_is_refused_as_a_source_and_as_an_outro(tmp_path):
    from clipcrow.planner.source import probe_source
    from clipcrow.branding import BrandError
    from clipcrow.service.brand import BrandStore
    bad = tmp_path / "source.mp4"
    bad.write_text("#EXTM3U\n#EXTINF:5,\nhttp://127.0.0.1:9/x.ts\n#EXT-X-ENDLIST\n")
    with pytest.raises(ValueError):
        probe_source(bad)
    with pytest.raises(BrandError):
        BrandStore(tmp_path / "brands").put_outro("a" * 32, bad)


# --- bodies are stopped before they are read -------------------------------------------------------------

def test_an_unsigned_post_is_refused_before_its_body_is_read_and_chunked_bodies_are_capped(tmp_path):
    from fastapi.testclient import TestClient
    from clipcrow.service.api import create_app
    from tests.mailhelp import Outbox, sign_up
    from tests.test_service import FakePipeline
    a = Accounts(tmp_path / "d" / "c.db")
    app = create_app(tmp_path / "d", FakePipeline(), accounts=a, mailer=Outbox(), max_upload_mb=1)
    c = TestClient(app)
    r = c.post("/api/jobs", files={"file": ("a.mp4", b"x" * 100)})
    assert r.status_code == 401
    assert c.post("/api/auth/code", json={"email": "not-an-email"}).status_code in (400, 422)       # sign-in itself is not blocked
    sign_up(c, "a@example.com")
    assert c.post("/api/billing/recharge", content=b"{" + b" " * (3 * 1024 * 1024) + b"}", headers={"content-type": "application/json"}).status_code == 413
    def chunks():
        for _ in range(20):
            yield b"x" * (1024 * 1024)
    assert c.post("/api/jobs", content=chunks(), headers={"content-type": "multipart/form-data; boundary=x"}).status_code == 413
