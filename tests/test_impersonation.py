"""A superadmin can look at another account exactly as its owner sees it, to judge the quality of their jobs: silent for the owner, read-only, time-limited, audited."""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from tests.mailhelp import sign_up
from tests.test_ops import client
from tests.test_service import upload, video_bytes, wait  # noqa: F401


def setup_people(tmp_path, monkeypatch, video_bytes):
    c, a, app = client(tmp_path, monkeypatch, admins="boss@example.com")
    boss, helper, maya = TestClient(app), TestClient(app), TestClient(app)
    sign_up(boss, "boss@example.com")
    sign_up(helper, "helper@example.com")
    sign_up(maya, "maya@example.com")
    assert boss.put("/api/admin/settings", json={"values": {"extra_admins": "helper@example.com"}}).status_code == 200
    jid = wait(maya, upload(maya, video_bytes).json()["id"])["id"]
    return app, a, boss, helper, maya, a.by_email("maya@example.com").id, jid


def test_a_superadmin_sees_the_account_as_its_owner_does_and_the_owner_is_told_nothing(tmp_path, monkeypatch, video_bytes):
    app, a, boss, helper, maya, mid, jid = setup_people(tmp_path, monkeypatch, video_bytes)
    assert jid not in {j["id"] for j in boss.get("/api/jobs").json()}                              # your own list, before
    mails = len(app.state.outbox.sent)
    assert boss.post("/api/admin/impersonate", json={"user_id": mid}).json()["email"] == "maya@example.com"
    assert jid in {j["id"] for j in boss.get("/api/jobs").json()}                                  # now hers
    assert boss.get(f"/api/jobs/{jid}").status_code == 200 and boss.get(f"/api/jobs/{jid}/clips/c1/video").status_code in (200, 302)
    cfg = boss.get("/api/config").json()
    assert cfg["viewing_as"] == "maya@example.com" and cfg["email"] == "maya@example.com" and cfg["admin"] is False
    assert len(app.state.outbox.sent) == mails                                                    # silent: no mail to her, none to anyone
    assert maya.get("/api/config").json()["viewing_as"] is None and jid in {j["id"] for j in maya.get("/api/jobs").json()}      # her own session is untouched


def test_while_viewing_nothing_can_be_changed_and_her_social_connection_is_not_touched(tmp_path, monkeypatch, video_bytes):
    app, a, boss, helper, maya, mid, jid = setup_people(tmp_path, monkeypatch, video_bytes)
    boss.post("/api/admin/impersonate", json={"user_id": mid})
    before = a.get(mid).credits
    assert upload(boss, video_bytes).status_code == 403
    assert boss.delete(f"/api/jobs/{jid}").status_code == 403
    assert boss.put("/api/me/notifications", json={"job_done": True}).status_code == 403
    assert boss.request("DELETE", "/api/account", json={"email": "maya@example.com"}).status_code == 403
    assert boss.post(f"/api/jobs/{jid}/retry", json={}).status_code == 403
    assert boss.get("/api/sociobird").status_code == 403 and boss.get("/api/publish/channels").status_code == 403
    assert app.state.store.get(jid) is not None and a.get(mid).credits == before and a.by_email("maya@example.com") is not None
    assert boss.get("/api/admin/overview").status_code == 404                                      # the operator pages are hers to not have while viewing


def test_only_a_superadmin_can_view_and_never_another_superadmin_or_yourself(tmp_path, monkeypatch, video_bytes):
    app, a, boss, helper, maya, mid, jid = setup_people(tmp_path, monkeypatch, video_bytes)
    assert helper.post("/api/admin/impersonate", json={"user_id": mid}).status_code == 404          # an ordinary operator cannot
    assert maya.post("/api/admin/impersonate", json={"user_id": mid}).status_code == 404
    assert boss.post("/api/admin/impersonate", json={"user_id": a.by_email("boss@example.com").id}).status_code == 422
    assert boss.post("/api/admin/impersonate", json={"user_id": "../../etc"}).status_code == 422
    assert boss.post("/api/admin/impersonate", json={"user_id": "0" * 32}).status_code == 404
    assert helper.get("/api/admin/audit").status_code == 404 and maya.get("/api/admin/audit").status_code == 404


def test_stopping_returns_to_your_own_view_and_start_and_stop_are_in_the_audit_log(tmp_path, monkeypatch, video_bytes):
    app, a, boss, helper, maya, mid, jid = setup_people(tmp_path, monkeypatch, video_bytes)
    boss.post("/api/admin/impersonate", json={"user_id": mid})
    assert boss.get("/api/admin/audit").status_code == 200                                           # the log is readable while viewing (the real person is checked)
    assert boss.delete("/api/admin/impersonate").json() == {"ok": True}
    assert boss.get("/api/config").json()["viewing_as"] is None and jid not in {j["id"] for j in boss.get("/api/jobs").json()}
    log = [e for e in boss.get("/api/admin/audit").json()["entries"] if e["action"].startswith("view-as")]
    assert [(e["by"], e["action"], e["target"]) for e in log] == [("boss@example.com", "view-as-stop", "maya@example.com"), ("boss@example.com", "view-as-start", "maya@example.com")]
    assert all(e["at"] for e in log)


def test_it_ends_by_itself_with_the_hour_with_sign_out_and_when_the_account_is_deleted(tmp_path, monkeypatch, video_bytes):
    app, a, boss, helper, maya, mid, jid = setup_people(tmp_path, monkeypatch, video_bytes)
    tok = boss.cookies.get("clipcrow_session")
    a.start_impersonation(tok, mid, minutes=-1)                                                       # already past its hour
    assert boss.get("/api/config").json()["viewing_as"] is None
    boss.post("/api/admin/impersonate", json={"user_id": mid})
    boss.post("/api/auth/logout")
    sign_up(boss, "boss@example.com")                                                                 # a new session
    assert boss.get("/api/config").json()["viewing_as"] is None
    boss.post("/api/admin/impersonate", json={"user_id": mid})
    a.delete(mid)
    assert boss.get("/api/config").json()["viewing_as"] is None and boss.get("/api/jobs").status_code == 200


def test_the_ui_shows_a_banner_with_a_stop_button_and_the_privacy_page_says_staff_may_look():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "clipcrow" / "service" / "static"
    js, legal = (root / "app.js").read_text(encoding="utf-8"), (root / "legal.js").read_text(encoding="utf-8")
    assert "viewing_as" in js and "/admin/impersonate" in js
    assert "read-only" in legal.lower() and "support" in legal.lower()
