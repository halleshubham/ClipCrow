"""Posting finished clips through SocioBird."""
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from clipcrow.edl import load_edl
from clipcrow.service.accounts import Accounts
from clipcrow.service.api import create_app
from clipcrow.service.plans import Catalog, Plan
from clipcrow.service.publish import (Channel, PublishError, SocioBird, caption_html, settings_for, validate_when)
from tests.test_service import FakePipeline, upload, video_bytes, wait  # noqa: F401

PW = "correct horse battery"
CHANNELS = [{"id": "ch-yt", "name": "My YouTube", "identifier": "youtube"}, {"id": "ch-ig", "name": "My Insta", "identifier": "instagram"},
            {"id": "ch-tt", "name": "My TikTok", "identifier": "tiktok"}, {"id": "ch-x", "name": "My X", "identifier": "x"},
            {"id": "ch-off", "name": "Off", "identifier": "youtube", "disabled": True}]


# --- captions and settings --------------------------------------------------------------------------------

def test_captions_are_escaped_plain_text_wrapped_in_paragraphs():
    out = caption_html("<script>alert(1)</script> & more\n\n  second line \x00\x07", "youtube")
    assert out == "<p>&lt;script&gt;alert(1)&lt;/script&gt; &amp; more</p><p>second line</p>"
    assert "<script" not in out and caption_html("", "tiktok") == "<p></p>"
    assert len(caption_html("x" * 5000, "tiktok")) == len("<p>") + 2000 + len("</p>")          # TikTok's limit is 2000
    assert caption_html("शीर्षक", "instagram") == "<p>शीर्षक</p>"


def test_settings_are_exactly_what_each_platforms_schema_requires():
    yt = settings_for("youtube", title="  My title  ", privacy="unlisted", made_for_kids=True)
    assert yt == {"__type": "youtube", "title": "My title", "type": "unlisted", "selfDeclaredMadeForKids": "yes"}
    assert settings_for("instagram", title="x")["post_type"] == "post"
    tt = settings_for("tiktok", title="t" * 200, privacy="private", ai_generated=True)
    assert tt["title"] == "t" * 90 and tt["privacy_level"] == "SELF_ONLY" and tt["video_made_with_ai"] is True
    assert tt["content_posting_method"] == "DIRECT_POST"                                             # UPLOAD would not publish
    assert {"privacy_level", "duet", "stitch", "comment", "autoAddMusic", "brand_content_toggle", "brand_organic_toggle",
            "content_posting_method"} <= set(tt)                                                       # all required by the schema
    assert settings_for("tiktok", title="x")["video_made_with_ai"] is False
    assert settings_for("tiktok", title="x", privacy="public")["privacy_level"] == "PUBLIC_TO_EVERYONE"
    assert len(settings_for("youtube", title="y" * 300)["title"]) == 100


@pytest.mark.parametrize("platform,kw,text", [("youtube", {"title": "a"}, "at least 2"), ("youtube", {"title": "ab", "privacy": "x"}, "public, unlisted"),
                                              ("x", {"title": "ab"}, "not supported")])
def test_bad_settings_are_refused_with_a_message(platform, kw, text):
    with pytest.raises(PublishError, match=text):
        settings_for(platform, **kw)


def test_the_schedule_time_must_be_a_few_minutes_to_a_year_ahead():
    assert validate_when(None) is None and validate_when("") is None
    ok = validate_when((datetime.now(timezone.utc) + timedelta(hours=3)).isoformat())
    assert ok.tzinfo is not None
    assert validate_when((datetime.now(timezone.utc) + timedelta(hours=3)).replace(tzinfo=None).isoformat()).tzinfo is not None   # naive means UTC
    for bad in ("tomorrow", (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
                (datetime.now(timezone.utc) + timedelta(days=400)).isoformat(), (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()):
        with pytest.raises(PublishError):
            validate_when(bad)


# --- the SocioBird client -----------------------------------------------------------------------------------------

def test_the_client_filters_channels_and_builds_the_requests_it_documents(tmp_path):
    calls = []

    def fake(method, path, **kw):
        calls.append((method, path, kw))
        if path.endswith("integrations"):
            return CHANNELS
        if path.endswith("upload"):
            return {"id": "m1", "path": "https://cdn.example/m1.mp4"}
        return [{"postId": "p1", "integration": "ch-yt"}]
    sb = SocioBird("https://api.sociobird.example/", "KEY", request=fake)
    chans = sb.channels()
    assert [(c.id, c.platform) for c in chans] == [("ch-yt", "youtube"), ("ch-ig", "instagram"), ("ch-tt", "tiktok")]   # x and disabled are out
    f = tmp_path / "c.mp4"
    f.write_bytes(b"0" * 1000)
    media = sb.upload(f)
    assert media == {"id": "m1", "path": "https://cdn.example/m1.mp4"}
    sb.post(chans[0], "<p>hi</p>", media, {"__type": "youtube", "type": "public"}, when=None)
    sb.post(chans[1], "<p>hi</p>", media, {"__type": "instagram", "post_type": "post"}, when=datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
    now_body, later_body = calls[-2][2]["json_body"], calls[-1][2]["json_body"]
    assert now_body["type"] == "now" and later_body["type"] == "schedule" and later_body["date"] == "2030-01-02T03:04:05.000Z"
    assert later_body["posts"] == [{"integration": {"id": "ch-ig"}, "value": [{"content": "<p>hi</p>", "image": [media]}],
                                    "settings": {"__type": "instagram", "post_type": "post"}}]


def test_the_client_sends_the_key_only_in_the_authorization_header_over_https(monkeypatch, tmp_path):
    seen = {}

    class R:
        def read(self, n):
            return b'{"id": "m", "path": "p"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(req, timeout):
        seen.update(url=req.full_url, auth=req.get_header("Authorization"), ctype=req.get_header("Content-type"), body=req.data)
        return R()
    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    sb = SocioBird("https://api.sociobird.example", "SECRETKEY")
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"VIDEO")
    sb.upload(f)
    assert seen["url"] == "https://api.sociobird.example/public/v1/upload" and seen["auth"] == "SECRETKEY"
    assert seen["ctype"].startswith("multipart/form-data; boundary=") and b'filename="clip.mp4"' in seen["body"] and b"VIDEO" in seen["body"]
    assert b"SECRETKEY" not in seen["body"]
    for url in ("http://api.sociobird.example", "ftp://x", ""):
        with pytest.raises(ValueError):
            SocioBird(url, "k")
    with pytest.raises(ValueError):
        SocioBird("https://x.example", "")


def test_the_client_refuses_a_huge_video_and_a_reply_without_a_media_id(tmp_path, monkeypatch):
    f = tmp_path / "big.mp4"
    f.write_bytes(b"0")
    monkeypatch.setattr("clipcrow.service.publish.MAX_UPLOAD", 0)
    with pytest.raises(PublishError, match="too large"):
        SocioBird("https://x.example", "k", request=lambda *a, **k: {}).upload(f)
    monkeypatch.setattr("clipcrow.service.publish.MAX_UPLOAD", 10 ** 9)
    with pytest.raises(PublishError, match="did not accept"):
        SocioBird("https://x.example", "k", request=lambda *a, **k: {"nothing": 1}).upload(f)


# --- the API -------------------------------------------------------------------------------------------------------------

class FakeSB(SocioBird):
    def __init__(self, fail_upload=None, fail_channel=None):
        super().__init__("https://api.sociobird.example", "KEY", request=lambda *a, **k: {})
        self.uploaded, self.posted = [], []
        self.fail_upload, self.fail_channel = fail_upload, fail_channel

    def channels(self):
        return [Channel(c["id"], c["name"], c["identifier"]) for c in CHANNELS if c["identifier"] in ("youtube", "instagram", "tiktok") and not c.get("disabled")]

    def upload(self, path):
        if self.fail_upload:
            raise self.fail_upload
        self.uploaded.append(path.name)
        return {"id": "m1", "path": "https://cdn.example/m1.mp4"}

    def post(self, channel, content, media, settings, *, when):
        if self.fail_channel == channel.id:
            raise PublishError("SocioBird refused the request (422).")
        self.posted.append((channel.id, content, settings, when))
        return "p-" + channel.id


def setup(tmp_path, video_bytes, sb=None, **kw):
    sb = sb or FakeSB()
    app = create_app(tmp_path / "data", FakePipeline(), sociobird=sb, **kw)
    c = TestClient(app)
    job = wait(c, upload(c, video_bytes).json()["id"])
    return c, sb, job["id"], job["clips"][0]["id"], app


def posts_when_done(c, jid, cid, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        rows = c.get(f"/api/jobs/{jid}/clips/{cid}/posts").json()
        if rows and all(r["status"] not in ("queued",) for r in rows):
            return rows
        time.sleep(0.05)
    raise AssertionError(rows)


def test_a_clip_is_uploaded_once_and_posted_to_each_chosen_channel(tmp_path, video_bytes):
    c, sb, jid, cid, _ = setup(tmp_path, video_bytes)
    assert c.get("/api/config").json()["publishing"] is True
    assert [x["platform"] for x in c.get("/api/publish/channels").json()] == ["youtube", "instagram", "tiktok"]
    r = c.post(f"/api/jobs/{jid}/clips/{cid}/publish", json={"channels": ["ch-yt", "ch-tt"], "caption": "Look <b>here</b>\nsecond", "privacy": "public"})
    assert r.status_code == 202 and [x["status"] for x in r.json()] == ["queued", "queued"]
    rows = posts_when_done(c, jid, cid)
    assert [(x["platform"], x["status"]) for x in rows] == [("youtube", "sent"), ("tiktok", "sent")]
    assert sb.uploaded == [f"{cid}.mp4"]                                              # one upload, two posts
    (yt_ch, yt_body, yt_set, yt_when), (tt_ch, tt_body, tt_set, _) = sb.posted
    assert yt_body == "<p>Look &lt;b&gt;here&lt;/b&gt;</p><p>second</p>" and yt_set["title"] and yt_when is None
    assert tt_set["video_made_with_ai"] is False and tt_set["content_posting_method"] == "DIRECT_POST"


def test_one_failing_channel_does_not_stop_the_others_and_shows_a_plain_reason(tmp_path, video_bytes):
    c, sb, jid, cid, _ = setup(tmp_path, video_bytes, FakeSB(fail_channel="ch-ig"))
    c.post(f"/api/jobs/{jid}/clips/{cid}/publish", json={"channels": ["ch-yt", "ch-ig", "ch-tt"]})
    rows = {r["platform"]: r for r in posts_when_done(c, jid, cid)}
    assert (rows["youtube"]["status"], rows["instagram"]["status"], rows["tiktok"]["status"]) == ("sent", "failed", "sent")
    assert rows["instagram"]["detail"] == "SocioBird refused the request (422)."


def test_a_failed_upload_fails_every_post_and_unexpected_errors_stay_generic(tmp_path, video_bytes):
    c, sb, jid, cid, _ = setup(tmp_path, video_bytes, FakeSB(fail_upload=PublishError("SocioBird could not be reached.")))
    c.post(f"/api/jobs/{jid}/clips/{cid}/publish", json={"channels": ["ch-yt", "ch-tt"]})
    assert {(r["status"], r["detail"]) for r in posts_when_done(c, jid, cid)} == {("failed", "SocioBird could not be reached.")}
    c2, sb2, jid2, cid2, _ = setup(tmp_path / "b", video_bytes, FakeSB(fail_upload=RuntimeError("/srv/secret key=abc")))
    c2.post(f"/api/jobs/{jid2}/clips/{cid2}/publish", json={"channels": ["ch-yt"]})
    rows = posts_when_done(c2, jid2, cid2)
    assert rows[0]["detail"] == "The video could not be uploaded." and "secret" not in json.dumps(rows)


def test_scheduling_records_the_time_and_a_bad_request_queues_nothing(tmp_path, video_bytes):
    c, sb, jid, cid, _ = setup(tmp_path, video_bytes)
    when = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0)
    r = c.post(f"/api/jobs/{jid}/clips/{cid}/publish", json={"channels": ["ch-ig"], "when": when.isoformat()})
    assert r.status_code == 202
    row = posts_when_done(c, jid, cid)[0]
    assert row["status"] == "scheduled" and row["when"] == when.isoformat(timespec="seconds").replace("+00:00", "+00:00")
    assert sb.posted[0][3] == when
    for body, code in (({"channels": []}, 422), ({"channels": ["ch-yt", "ch-yt"]}, 422), ({"channels": ["ch-x"]}, 422),
                       ({"channels": ["nope"]}, 422), ({"channels": ["ch-yt"], "when": "yesterday"}, 422),
                       ({"channels": ["ch-yt"], "title": "a"}, 422), ({"channels": ["ch-yt"], "privacy": "everyone"}, 422),
                       ({"channels": ["ch-yt"] * 7}, 422)):
        assert c.post(f"/api/jobs/{jid}/clips/{cid}/publish", json=body).status_code == code, body
    assert len(c.get(f"/api/jobs/{jid}/clips/{cid}/posts").json()) == 1                  # only the good one was ever recorded


def ai_edl(app, jid):
    """Make the job's plan include an AI-generated picture, as explainer mode would."""
    p = app.state.store.edl_path(jid)
    d = json.loads(p.read_text())
    d["assets"] = d.get("assets", []) + [{"id": "g1", "kind": "image", "origin": "ai_image", "ai": "generated", "prompt": "a jar", "uri": "assets/g1.png"}]
    p.write_text(json.dumps(d))


def test_ai_generated_media_needs_a_confirmation_and_sets_tiktoks_own_label(tmp_path, video_bytes):
    c, sb, jid, cid, app = setup(tmp_path, video_bytes)
    ai_edl(app, jid)
    r = c.post(f"/api/jobs/{jid}/clips/{cid}/publish", json={"channels": ["ch-tt"]})
    assert r.status_code == 422 and "AI-generated" in r.json()["detail"]
    r = c.post(f"/api/jobs/{jid}/clips/{cid}/publish", json={"channels": ["ch-tt", "ch-yt"], "ai_confirmed": True})
    assert r.status_code == 202
    posts_when_done(c, jid, cid)
    by = {ch: s for ch, _, s, _ in sb.posted}
    assert by["ch-tt"]["video_made_with_ai"] is True


def test_posting_needs_a_finished_clip_a_login_and_a_configured_server(tmp_path, video_bytes):
    c, sb, jid, cid, app = setup(tmp_path, video_bytes)
    assert c.post(f"/api/jobs/{jid}/clips/c99/publish", json={"channels": ["ch-yt"]}).status_code == 404
    assert c.post(f"/api/jobs/zzzzzzzzzzzz/clips/{cid}/publish", json={"channels": ["ch-yt"]}).status_code == 404
    app.state.store.clip_path(jid, cid).unlink()
    assert c.post(f"/api/jobs/{jid}/clips/{cid}/publish", json={"channels": ["ch-yt"]}).status_code == 409
    plain = TestClient(create_app(tmp_path / "other", FakePipeline()))
    assert plain.get("/api/config").json()["publishing"] is False
    assert plain.get("/api/publish/channels").status_code == 404
    locked = TestClient(create_app(tmp_path / "locked", FakePipeline(), token="tok", sociobird=FakeSB()))
    assert locked.get("/api/publish/channels").status_code == 401


def test_with_accounts_posting_is_off_unless_the_operator_shares_the_channels_and_people_only_see_their_own(tmp_path, video_bytes):
    cat = Catalog(plans={"free": Plan("free", "Free", 100, 7, 3)})

    def build(root, shared):
        a = Accounts(root / "data" / "x.db")
        app = create_app(root / "data", FakePipeline(), accounts=a, catalog=cat, sociobird=FakeSB(), sociobird_shared=shared)
        c = TestClient(app)
        c.post("/api/auth/register", json={"email": "a@example.com", "password": PW})
        return c, app
    c, app = build(tmp_path / "off", False)
    assert c.get("/api/config").json()["publishing"] is False and c.get("/api/publish/channels").status_code == 404
    c, app = build(tmp_path / "on", True)
    assert c.get("/api/config").json()["publishing"] is True
    jid = wait(c, upload(c, video_bytes).json()["id"])["id"]
    cid = c.get(f"/api/jobs/{jid}").json()["clips"][0]["id"]
    assert c.post(f"/api/jobs/{jid}/clips/{cid}/publish", json={"channels": ["ch-yt"]}).status_code == 202
    posts_when_done(c, jid, cid)
    other = TestClient(app)
    other.post("/api/auth/register", json={"email": "b@example.com", "password": PW})
    assert other.post(f"/api/jobs/{jid}/clips/{cid}/publish", json={"channels": ["ch-yt"]}).status_code == 404
    assert other.get(f"/api/jobs/{jid}/clips/{cid}/posts").status_code == 404


def test_the_post_log_goes_with_the_job(tmp_path, video_bytes):
    c, sb, jid, cid, app = setup(tmp_path, video_bytes)
    c.post(f"/api/jobs/{jid}/clips/{cid}/publish", json={"channels": ["ch-yt"]})
    posts_when_done(c, jid, cid)
    assert (tmp_path / "data" / "jobs" / jid / "out" / "posts.json").is_file()
    assert c.delete(f"/api/jobs/{jid}").status_code == 204
    assert not (tmp_path / "data" / "jobs" / jid).exists()


def test_requests_carry_a_named_user_agent_because_cloudflare_refuses_urllibs(monkeypatch):
    import io
    from clipcrow.service import publish
    seen = {}

    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake(req, timeout=0):
        seen["ua"] = req.get_header("User-agent")
        seen["auth"] = req.get_header("Authorization")
        return R(b"[]")
    monkeypatch.setattr(publish.urllib.request, "urlopen", fake)
    assert publish.SocioBird("https://example.test/api", "k").channels() == []
    assert seen["ua"] == publish.USER_AGENT and "urllib" not in seen["ua"].lower() and seen["auth"] == "k"


def test_an_answer_that_is_a_web_page_names_the_address_setting(monkeypatch):
    import io
    from clipcrow.service import publish

    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(publish.urllib.request, "urlopen", lambda req, timeout=0: R(b"<!DOCTYPE html><html></html>"))
    with pytest.raises(publish.PublishError, match="SOCIOBIRD_API_URL"):
        publish.SocioBird("https://example.test", "k").channels()
