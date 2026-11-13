"""Direct browser-to-bucket uploads: the file goes to R2/S3 in presigned parts, never through the app's own domain."""
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("boto3")
pytest.importorskip("moto")
from fastapi.testclient import TestClient

import clipcrow.service.api as api_mod
from clipcrow.service.accounts import Accounts
from clipcrow.service.api import create_app
from clipcrow.service.plans import Catalog, Plan
from clipcrow.service.storage import S3Storage, StorageError, signature
from tests.test_service import FakePipeline, video_bytes, wait  # noqa: F401

PW = "correct horse battery"
MB = 1024 * 1024


@pytest.fixture(scope="module")
def big_video(tmp_path_factory):
    """A real 12 s video of about 12 MB, so it needs three parts at 5 MiB."""
    p = tmp_path_factory.mktemp("big") / "big.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=12", "-f", "lavfi", "-i", "sine=f=220:d=12",
                    "-shortest", "-c:v", "libx264", "-b:v", "8M", "-maxrate", "8M", "-bufsize", "8M", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                    "-c:a", "aac", str(p)], check=True)
    data = p.read_bytes()
    assert len(data) > 10 * MB
    return data


def make(tmp_path, storage, pipeline=None, **kw):
    app = create_app(tmp_path / "data", pipeline or FakePipeline(), storage=storage, **kw)
    return TestClient(app), app


OPTIONS = {"min_clip": 20, "max_clip": 60, "clips": 2}


def put(url: str, chunk: bytes) -> str:
    """One part, the way a browser sends a Blob slice: no form-encoding content type (urllib would add one, and the S3 test
    server then reads the body as form fields and loses the part number from the URL)."""
    req = urllib.request.Request(url, data=chunk, method="PUT", headers={"Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.headers["ETag"]


def put_parts(start: dict, data: bytes) -> list[dict]:
    """What the browser does: PUT each slice to its presigned URL and keep the ETag the bucket answers with."""
    parts, offset = [], 0
    for p in start["parts"]:
        chunk = data[offset:offset + p["length"]]
        assert len(chunk) == p["length"]
        parts.append({"n": p["n"], "etag": put(p["url"], chunk)})
        offset += p["length"]
    assert offset == len(data)
    return parts


def start(c, data, name="talk.mp4", **kw):
    return c.post("/api/uploads", json={"filename": name, "size": len(data), "rights": True, **kw})


def finish(c, uid, parts, options=None):
    return c.post(f"/api/uploads/{uid}/complete", json={"parts": parts, "options": OPTIONS if options is None else options})


# --- starting ---------------------------------------------------------------------------------------------------

def test_the_config_says_direct_upload_is_on_only_with_storage(tmp_path, storage):
    c, _ = make(tmp_path, storage)
    assert c.get("/api/config").json()["direct_upload"] is True
    plain = TestClient(create_app(tmp_path / "plain", FakePipeline()))
    assert plain.get("/api/config").json()["direct_upload"] is False
    assert plain.post("/api/uploads", json={"filename": "a.mp4", "size": 10, "rights": True}).status_code == 404


def test_the_operator_can_turn_it_off(tmp_path, storage, monkeypatch):
    monkeypatch.setenv("CLIPCROW_DIRECT_UPLOAD", "0")
    c, _ = make(tmp_path, storage)
    assert c.get("/api/config").json()["direct_upload"] is False
    assert start(c, b"x" * 10).status_code == 404
    csp = c.get("/api/config").headers["content-security-policy"]
    assert "connect-src 'self';" in csp and f"media-src 'self' {storage.origin};" in csp      # playback from the bucket stays, uploads to it do not


def test_each_part_url_signs_its_exact_length_so_nobody_can_store_more_than_they_announced(tmp_path, storage, monkeypatch):
    monkeypatch.setattr(api_mod, "PART_SIZE", 5 * MB)
    c, _ = make(tmp_path, storage)
    r = start(c, b"x" * (12 * MB + 7))
    assert r.status_code == 201
    body = r.json()
    assert [p["length"] for p in body["parts"]] == [5 * MB, 5 * MB, 2 * MB + 7] and body["part_size"] == 5 * MB
    for p in body["parts"]:
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(p["url"]).query)
        assert "content-length" in q["X-Amz-SignedHeaders"][0].split(";")                   # the length is part of the signature
        assert q["partNumber"] == [str(p["n"])] and "X-Amz-Signature" in q and p["url"].startswith(storage.origin)
    assert "secret" not in r.text.lower() and "s" != body["id"]


def test_a_small_file_is_one_part_of_exactly_its_size(tmp_path, storage):
    c, _ = make(tmp_path, storage)
    body = start(c, b"x" * 1234).json()
    assert body["part_size"] == 1234 and [p["length"] for p in body["parts"]] == [1234]


@pytest.mark.parametrize("kw,name,size,code,text", [
    ({"rights": False}, "a.mp4", 10, 400, "rights"), ({}, "a.exe", 10, 400, "MP4"), ({}, "a", 10, 400, "MP4"),
    ({}, "a.mp4", 0, 422, ""), ({}, "a.mp4", 10 ** 15, 413, "limit")])
def test_bad_announcements_are_refused(tmp_path, storage, kw, name, size, code, text):
    c, _ = make(tmp_path, storage)
    r = c.post("/api/uploads", json={"filename": name, "size": size, "rights": kw.get("rights", True)})
    assert r.status_code == code and text in r.text


def test_a_server_that_cannot_run_jobs_does_not_accept_uploads(tmp_path, storage):
    c, _ = make(tmp_path, storage, FakePipeline(ready="No Claude credentials on the server"))
    assert start(c, b"x" * 10).status_code == 503


def test_unfinished_uploads_are_limited_and_cancelling_frees_a_slot(tmp_path, storage):
    c, app = make(tmp_path, storage)
    ids = [start(c, b"x" * 10).json()["id"] for _ in range(3)]
    assert start(c, b"x" * 10).status_code == 429
    assert c.delete(f"/api/uploads/{ids[0]}").status_code == 204
    assert start(c, b"x" * 10).status_code == 201
    assert not [u for u in app.state.uploads.list() if u.id == ids[0]]
    assert len(storage.client.list_multipart_uploads(Bucket=storage.bucket).get("Uploads", [])) == 3      # the cancelled one was aborted


# --- the whole thing ---------------------------------------------------------------------------------------------

def test_a_video_goes_to_the_bucket_and_becomes_a_job_whose_id_is_the_uploads(tmp_path, storage, video_bytes):
    c, app = make(tmp_path, storage)
    r = start(c, video_bytes)
    uid = r.json()["id"]
    parts = put_parts(r.json(), video_bytes)
    assert storage.has(uid, "source.mp4") is False                                  # nothing is an object until the parts are put together
    done = finish(c, uid, parts)
    assert done.status_code == 201 and done.json()["id"] == uid and done.json()["source"]["duration"] > 200
    assert storage.size(uid, "source.mp4") == len(video_bytes)
    job = wait(c, uid)
    assert job["status"] == "done" and len(job["clips"]) >= 1
    stored = app.state.store.get(uid)
    local = app.state.store.source_path(uid)
    assert stored.source_pending is None and local is not None and local.read_bytes() == video_bytes   # the runner fetched it
    assert stored.remote["source.mp4"] == signature(local)                          # and the mirror will not upload it again
    assert app.state.uploads.list() == []                                           # the upload record is gone
    assert c.delete(f"/api/jobs/{uid}").status_code == 204 and storage.has(uid, "source.mp4") is False   # purged with the job


def test_a_big_video_goes_up_in_several_parts(tmp_path, storage, big_video, monkeypatch):
    monkeypatch.setattr(api_mod, "PART_SIZE", 5 * MB)
    c, app = make(tmp_path, storage)
    r = start(c, big_video, "big.mp4")
    assert len(r.json()["parts"]) == 3
    done = finish(c, r.json()["id"], put_parts(r.json(), big_video), {"min_clip": 5, "max_clip": 10, "clips": 1})
    assert done.status_code == 201 and 11 < done.json()["source"]["duration"] < 13        # probed from the bucket without downloading it
    wait(c, done.json()["id"])
    assert app.state.store.source_path(done.json()["id"]).read_bytes() == big_video


def test_options_are_checked_before_the_upload_is_put_together_so_a_typo_does_not_cost_the_file(tmp_path, storage, video_bytes):
    c, app = make(tmp_path, storage)
    r = start(c, video_bytes)
    uid, parts = r.json()["id"], put_parts(r.json(), video_bytes)
    bad = finish(c, uid, parts, {"min_clip": 50, "max_clip": 20})
    assert bad.status_code == 422 and "shortest clip" in bad.json()["detail"]
    assert finish(c, uid, parts, {"links": "http://10.0.0.1/"}).status_code == 422
    assert app.state.uploads.get(uid)                                               # still open
    assert finish(c, uid, parts).status_code == 201                                 # and the corrected request works


# --- what must be refused ------------------------------------------------------------------------------------------

def test_a_file_that_is_not_the_announced_size_is_thrown_away(tmp_path, storage, video_bytes):
    c, app = make(tmp_path, storage)
    r = c.post("/api/uploads", json={"filename": "a.mp4", "size": 1000, "rights": True})
    uid = r.json()["id"]
    # the signed length is 1000, but a client that ignores it (moto does not check signatures) puts the real file
    etag = put(r.json()["parts"][0]["url"], video_bytes)
    res = finish(c, uid, [{"n": 1, "etag": etag}])
    assert res.status_code == 422 and "size" in res.json()["detail"]
    assert storage.has(uid, "source.mp4") is False and app.state.uploads.list() == []   # the object and the record are gone


def test_parts_must_be_complete_and_well_formed(tmp_path, storage, monkeypatch):
    monkeypatch.setattr(api_mod, "PART_SIZE", 5 * MB)
    c, app = make(tmp_path, storage)
    r = start(c, b"x" * (6 * MB))
    uid, parts = r.json()["id"], put_parts(r.json(), b"x" * (6 * MB))
    assert finish(c, uid, parts[:1]).status_code == 422                             # a part missing
    assert finish(c, uid, [parts[0], parts[0]]).status_code == 422                  # a part twice
    assert finish(c, uid, [parts[0], {"n": 2, "etag": "not-an-etag"}]).status_code == 422
    assert finish(c, uid, [parts[0], {"n": 9, "etag": parts[1]["etag"]}]).status_code == 422
    assert app.state.uploads.get(uid)                                               # none of that closed the upload


def test_wrong_etags_fail_and_clean_up(tmp_path, storage, monkeypatch):
    monkeypatch.setattr(api_mod, "PART_SIZE", 5 * MB)
    c, app = make(tmp_path, storage)
    r = start(c, b"x" * (6 * MB))
    uid, parts = r.json()["id"], put_parts(r.json(), b"x" * (6 * MB))
    forged = [{"n": 1, "etag": '"' + "0" * 32 + '"'}, parts[1]]
    assert finish(c, uid, forged).status_code == 422
    assert app.state.uploads.list() == []


def test_something_that_is_not_a_video_is_refused_and_removed(tmp_path, storage):
    c, app = make(tmp_path, storage)
    data = b"this is not a video" * 1000
    r = start(c, data)
    res = finish(c, r.json()["id"], put_parts(r.json(), data))
    assert res.status_code == 400 and "could not be read as a video" in res.json()["detail"]
    assert storage.has(r.json()["id"], "source.mp4") is False and app.state.uploads.list() == []


def test_a_video_too_short_for_the_clips_is_refused_and_removed(tmp_path, storage, big_video, monkeypatch):
    monkeypatch.setattr(api_mod, "PART_SIZE", 5 * MB)
    c, app = make(tmp_path, storage)
    r = start(c, big_video, "short.mp4")                                          # 12 s long
    res = finish(c, r.json()["id"], put_parts(r.json(), big_video), {"min_clip": 15, "max_clip": 60})   # a 15 s clip needs 20 s
    assert res.status_code == 400 and "need a longer video" in res.json()["detail"]
    assert storage.has(r.json()["id"], "source.mp4") is False and app.state.uploads.list() == []


def test_completing_twice_is_not_possible(tmp_path, storage, video_bytes):
    c, _ = make(tmp_path, storage)
    r = start(c, video_bytes)
    parts = put_parts(r.json(), video_bytes)
    assert finish(c, r.json()["id"], parts).status_code == 201
    assert finish(c, r.json()["id"], parts).status_code == 404


def test_unknown_and_malformed_upload_ids_are_just_not_found(tmp_path, storage):
    c, _ = make(tmp_path, storage)
    for uid in ("aaaaaaaaaaaa", "../../x", "0123456789ab%0A"):
        assert finish(c, uid, [{"n": 1, "etag": '"' + "a" * 32 + '"'}]).status_code in (404, 405, 422)
        assert c.delete(f"/api/uploads/{uid}").status_code in (404, 405)


# --- accounts -----------------------------------------------------------------------------------------------------------

def accounts_app(tmp_path, storage, free=100, pipeline=None):
    a = Accounts(tmp_path / "data" / "x.db")
    cat = Catalog(plans={"free": Plan("free", "Free", free, 7, 3)})
    app = create_app(tmp_path / "data", pipeline or FakePipeline(), storage=storage, accounts=a, catalog=cat)
    c = TestClient(app)
    c.post("/api/auth/register", json={"email": "a@example.com", "password": PW})
    return c, a, app


def test_with_accounts_uploads_need_a_session_and_belong_to_their_owner(tmp_path, storage, video_bytes):
    c, a, app = accounts_app(tmp_path, storage)
    anon = TestClient(app)
    assert anon.post("/api/uploads", json={"filename": "a.mp4", "size": 10, "rights": True}).status_code == 401
    r = start(c, video_bytes)
    uid = r.json()["id"]
    other = TestClient(app)
    other.post("/api/auth/register", json={"email": "b@example.com", "password": PW})
    parts = put_parts(r.json(), video_bytes)
    assert finish(other, uid, parts).status_code == 404 and other.delete(f"/api/uploads/{uid}").status_code == 404
    done = finish(c, uid, parts)
    assert done.status_code == 201 and c.get("/api/me").json()["used_minutes"] == 5       # the minutes came from probing the bucket copy
    assert other.get(f"/api/jobs/{uid}").status_code == 404


def test_not_enough_minutes_refuses_the_job_and_removes_the_upload(tmp_path, storage, video_bytes):
    c, a, app = accounts_app(tmp_path, storage, free=3)
    r = start(c, video_bytes)
    res = finish(c, r.json()["id"], put_parts(r.json(), video_bytes))
    assert res.status_code == 402 and "5 min" in res.json()["detail"]
    assert storage.has(r.json()["id"], "source.mp4") is False and c.get("/api/jobs").json() == []
    assert c.get("/api/me").json()["used_minutes"] == 0


def test_a_missing_object_at_run_time_fails_the_job_with_a_plain_message_and_refunds(tmp_path, storage, video_bytes):
    c, a, app = accounts_app(tmp_path, storage)
    r = start(c, video_bytes)
    uid = r.json()["id"]
    parts = put_parts(r.json(), video_bytes)
    real = storage.get_file

    def gone(job_id, rel, dest):
        raise StorageError("The file storage refused the request.")
    storage.get_file = gone
    try:
        assert finish(c, uid, parts).status_code == 201
        job = wait(c, uid)
    finally:
        storage.get_file = real
    assert job["status"] == "failed" and "file storage" in job["error"] and "Traceback" not in job["error"]
    assert c.get("/api/me").json()["used_minutes"] == 0                             # failed: the minutes came back


# --- abandoned uploads ------------------------------------------------------------------------------------------------------

def test_abandoned_uploads_expire_and_their_storage_is_released(tmp_path, storage, video_bytes):
    c, app = make(tmp_path, storage)
    r = start(c, video_bytes)
    uid = r.json()["id"]
    put_parts(r.json(), video_bytes)                                                # the parts are in the bucket, never completed
    app.state.expire_uploads()
    assert app.state.uploads.get(uid)                                               # not old enough yet
    rec = app.state.uploads.get(uid)
    app.state.uploads.save(rec.model_copy(update={"created_at": (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()}))
    app.state.expire_uploads()
    assert app.state.uploads.list() == []
    assert storage.client.list_multipart_uploads(Bucket=storage.bucket).get("Uploads", []) == []


# --- the content security policy and the bucket's CORS rule ---------------------------------------------------------------------

def test_the_csp_lets_the_page_talk_to_the_bucket_and_nothing_else(tmp_path, storage):
    c, _ = make(tmp_path, storage)
    csp = c.get("/api/config").headers["content-security-policy"]
    assert f"connect-src 'self' {storage.origin};" in csp and "default-src 'none'" in csp and "script-src 'self';" in csp


def test_the_cors_rule_the_browser_needs(storage):
    rule = storage.cors_rule(["https://app.example.com"])["CORSRules"][0]
    assert rule["AllowedMethods"] == ["PUT", "GET", "HEAD"] and rule["ExposeHeaders"] == ["ETag"]    # the ETag is how parts are confirmed
    assert storage.get_cors() == []
    storage.set_cors(["https://app.example.com/", "https://other.example.com"])
    got = storage.get_cors()[0]
    assert got["AllowedOrigins"] == ["https://app.example.com", "https://other.example.com"] and "ETag" in got["ExposeHeaders"]
    for bad in ("*", "https://*.example.com", "ftp://x", "https://x.example.com/path", "app.example.com"):
        with pytest.raises(ValueError):
            storage.set_cors([bad])
