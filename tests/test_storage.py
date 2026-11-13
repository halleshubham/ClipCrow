"""Object storage (Cloudflare R2 / S3): mirroring, presigned delivery, eviction, purge on delete.

The S3 code runs against a real S3-compatible server (moto in server mode) on localhost; R2-specific settings are
checked on the client config. Nothing here talks to Cloudflare."""
import json
import socket
import time
import urllib.request

import pytest

pytest.importorskip("fastapi")
boto3 = pytest.importorskip("boto3")
pytest.importorskip("moto")
from fastapi.testclient import TestClient

from clipcrow.service.accounts import Accounts
from clipcrow.service.api import create_app
from clipcrow.service.plans import Catalog, Plan
from clipcrow.service.retention import sweep
from clipcrow.service.storage import Mirror, S3Storage, StorageError, signature
from tests.test_accounts import age
from tests.test_service import FakePipeline, upload, video_bytes, wait  # noqa: F401

PW = "correct horse battery"
JOB = "0123456789ab"


def objects(st):
    return sorted(o["Key"] for o in st.client.list_objects_v2(Bucket=st.bucket).get("Contents", []))


# --- configuration and R2 specifics -----------------------------------------------------------------------

def test_the_client_uses_the_settings_r2_needs():
    st = S3Storage("b", endpoint="https://abc123.r2.cloudflarestorage.com", access_key="k", secret_key="s")
    cfg = st.client.meta.config
    assert cfg.signature_version == "s3v4" and cfg.s3["addressing_style"] == "path"
    assert st.client.meta.region_name == "auto" and st.client.meta.endpoint_url == "https://abc123.r2.cloudflarestorage.com"
    assert cfg.request_checksum_calculation == "when_required" and cfg.response_checksum_validation == "when_required"
    assert st.host == "abc123.r2.cloudflarestorage.com" and st.origin == "https://abc123.r2.cloudflarestorage.com"


def test_configuration_comes_from_the_environment(monkeypatch):
    for k in ("R2_ACCOUNT_ID", "R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "R2_PREFIX"):
        monkeypatch.delenv(k, raising=False)
    assert S3Storage.from_env() is None
    monkeypatch.setenv("R2_BUCKET", "clips"); monkeypatch.setenv("R2_ACCESS_KEY_ID", "k"); monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "s")
    assert S3Storage.from_env() is None                                          # no account id or endpoint yet
    monkeypatch.setenv("R2_ACCOUNT_ID", "acct42")
    st = S3Storage.from_env()
    assert st.endpoint == "https://acct42.r2.cloudflarestorage.com" and st.prefix == "clipcrow" and st.bucket == "clips"
    monkeypatch.setenv("R2_ENDPOINT", "https://acct42.eu.r2.cloudflarestorage.com"); monkeypatch.setenv("R2_PREFIX", "prod/app")
    st = S3Storage.from_env()
    assert st.host == "acct42.eu.r2.cloudflarestorage.com" and st.prefix == "prod/app"


@pytest.mark.parametrize("kw", [dict(endpoint="http://example.com"), dict(endpoint="https://x.example", prefix="../up"),
                                dict(endpoint="https://x.example", prefix="a b"), dict(endpoint="")])
def test_unsafe_configuration_is_refused(kw):
    base = dict(bucket="b", access_key="k", secret_key="s")
    with pytest.raises(ValueError):
        S3Storage(**{**base, **kw})
    with pytest.raises(ValueError):
        S3Storage("", endpoint="https://x.example", access_key="k", secret_key="s")


def test_keys_are_built_only_from_a_job_id_and_an_allow_listed_name(storage):
    assert storage.key(JOB, "out/c3.mp4") == f"cc/jobs/{JOB}/out/c3.mp4"
    assert storage.key(JOB, "source.mp4").endswith("/source.mp4")
    for job, rel in (("../x", "out/c1.mp4"), ("0123456789abc", "out/c1.mp4"), (JOB, "../../etc/passwd"), (JOB, "out/../../x"),
                     (JOB, "out/c1.mp4/../x"), (JOB, "out/evil.sh"), (JOB, "source.m p4"), (JOB, "/out/c1.mp4"), (JOB, "out/c1.mp4\n")):
        with pytest.raises(ValueError):
            storage.key(job, rel)


# --- operations against an S3-compatible server -------------------------------------------------------------

def test_upload_presign_and_download_round_trip(storage, tmp_path):
    f = tmp_path / "c1.mp4"
    f.write_bytes(b"VIDEO" * 1000)
    storage.put_file(JOB, "out/c1.mp4", f)
    assert storage.has(JOB, "out/c1.mp4") and not storage.has(JOB, "out/c2.mp4")
    head = storage.client.head_object(Bucket=storage.bucket, Key=storage.key(JOB, "out/c1.mp4"))
    assert head["ContentType"] == "video/mp4"
    url = storage.presign_get(JOB, "out/c1.mp4", filename="My clip (final)!.mp4")
    assert url.startswith(storage.endpoint) and "X-Amz-Signature=" in url and "X-Amz-Expires=600" in url
    with urllib.request.urlopen(url) as r:
        assert r.read() == b"VIDEO" * 1000 and 'attachment; filename="My_clip_final_.mp4"' in r.headers["Content-Disposition"]
    dest = tmp_path / "back.mp4"
    storage.get_file(JOB, "out/c1.mp4", dest)
    assert dest.read_bytes() == b"VIDEO" * 1000 and not list(tmp_path.glob("*.part"))
    assert "X-Amz-Expires=3600" in storage.presign_get(JOB, "out/c1.mp4", expires=10 ** 6)               # a link never lives longer than an hour


def test_deleting_a_job_removes_only_that_jobs_objects(storage, tmp_path):
    f = tmp_path / "x"
    f.write_bytes(b"x" * 10)
    other = "ba9876543210"
    for rel in ("out/c1.mp4", "out/edl.json", "source.mp4"):
        storage.put_file(JOB, rel, f)
    storage.put_file(other, "out/c1.mp4", f)
    assert storage.delete_job(JOB) == 3
    assert objects(storage) == [storage.key(other, "out/c1.mp4")]
    assert storage.delete_job(JOB) == 0                                          # nothing left is not an error


def test_storage_errors_are_one_plain_sentence_without_details(tmp_path):
    st = S3Storage("b", endpoint="http://127.0.0.1:9", access_key="k", secret_key="s")     # nothing listens there
    f = tmp_path / "x"
    f.write_bytes(b"x")
    with pytest.raises(StorageError) as e:
        st.put_file(JOB, "out/c1.mp4", f)
    assert "127.0.0.1" not in str(e.value) and "bucket" not in str(e.value).lower()


# --- the mirror ---------------------------------------------------------------------------------------------------------

def make_app(tmp_path, storage, **kw):
    app = create_app(tmp_path / "data", FakePipeline(), storage=storage, **kw)
    return TestClient(app), app


def synced(app, jid, want=(), timeout=20):
    """Wait until the background mirror has recorded EVERYTHING that exists locally (and ``want`` in particular), so a test
    that then edits the job file by hand cannot race the mirror's own bookkeeping."""
    end = time.time() + timeout
    while time.time() < end:
        job = app.state.store.get(jid)
        on_disk = [r for r in Mirror(None, app.state.store, backup_source=app.state.mirror_source).wanted(job)
                   if (app.state.store.dir(jid) / r).is_file()]
        if set(want) <= set(job.remote) and all(r in job.remote for r in on_disk):
            time.sleep(0.1)                                          # let the last bookkeeping write finish
            return app.state.store.get(jid)
        time.sleep(0.05)
    raise AssertionError(app.state.store.get(jid).remote)


def test_a_finished_job_is_mirrored_once_and_a_changed_clip_goes_up_again(tmp_path, storage, video_bytes):
    c, app = make_app(tmp_path, storage)
    jid = wait(c, upload(c, video_bytes).json()["id"])["id"]
    cid = app.state.store.get(jid).clips[0].id
    job = synced(app, jid, [f"out/{cid}.mp4", "out/edl.json", "out/plan-report.json", "out/transcript.json"])
    keys = objects(storage)
    assert storage.key(jid, f"out/{cid}.mp4") in keys and storage.key(jid, "out/edl.json") in keys
    assert not any(k.endswith("source.mp4") for k in keys)                       # the original is not uploaded unless asked
    m = Mirror(storage, app.state.store)
    assert m.backup(jid) == 0                                                    # nothing changed: nothing uploaded
    p = app.state.store.clip_path(jid, cid)
    p.write_bytes(b"re-rendered" * 500)
    assert m.backup(jid) == 1
    assert signature(p) == app.state.store.get(jid).remote[f"out/{cid}.mp4"]
    body = urllib.request.urlopen(storage.presign_get(jid, f"out/{cid}.mp4")).read()
    assert body == b"re-rendered" * 500


def test_the_original_is_uploaded_only_when_the_operator_asks(tmp_path, storage, video_bytes):
    c, app = make_app(tmp_path, storage, backup_source=True)
    jid = wait(c, upload(c, video_bytes).json()["id"])["id"]
    synced(app, jid, ["source.mp4"])
    assert storage.has(jid, "source.mp4")


def test_a_failed_upload_is_retried_by_the_next_pass(tmp_path, storage, video_bytes):
    c, app = make_app(tmp_path, S3Storage("nope", endpoint=storage.endpoint, access_key="k", secret_key="s"))   # no such bucket
    jid = wait(c, upload(c, video_bytes).json()["id"])["id"]
    time.sleep(1.0)
    assert app.state.store.get(jid).remote == {} and app.state.store.get(jid).status == "done"      # the job is unharmed
    m = Mirror(storage, app.state.store)                                                          # now a working bucket
    assert m.backup(jid) >= 2 and "out/edl.json" in app.state.store.get(jid).remote


def test_editing_a_clip_mirrors_the_new_plan_and_video(tmp_path, storage, video_bytes):
    c, app = make_app(tmp_path, storage)
    jid = wait(c, upload(c, video_bytes).json()["id"])["id"]
    cid = app.state.store.get(jid).clips[0].id
    before = synced(app, jid, [f"out/{cid}.mp4", "out/edl.json"]).remote["out/edl.json"]
    c.put(f"/api/jobs/{jid}/clips/{cid}", json={"title": "संपादित"})
    end = time.time() + 15
    while time.time() < end and app.state.store.get(jid).remote.get("out/edl.json") == before:
        time.sleep(0.05)
    assert app.state.store.get(jid).remote["out/edl.json"] != before
    data = json.loads(urllib.request.urlopen(storage.presign_get(jid, "out/edl.json")).read())
    assert next(x for x in data["clips"] if x["id"] == cid)["title"] == "संपादित"


# --- delivery -------------------------------------------------------------------------------------------------------------

def test_a_clip_whose_local_file_is_gone_is_served_from_the_bucket_by_a_short_lived_link(tmp_path, storage, video_bytes):
    c, app = make_app(tmp_path, storage)
    jid = wait(c, upload(c, video_bytes).json()["id"])["id"]
    cid = app.state.store.get(jid).clips[0].id
    synced(app, jid, [f"out/{cid}.mp4"])
    local = c.get(f"/api/jobs/{jid}/clips/{cid}/video", follow_redirects=False)
    assert local.status_code == 200                                              # still on disk: served locally
    app.state.store.clip_path(jid, cid).unlink()
    r = c.get(f"/api/jobs/{jid}/clips/{cid}/video", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith(storage.endpoint) and r.headers["cache-control"] == "no-store"
    assert urllib.request.urlopen(r.headers["location"]).read() == b"0123456789" * 1000
    d = c.get(f"/api/jobs/{jid}/clips/{cid}/download", follow_redirects=False)
    assert d.status_code == 302 and "response-content-disposition" in d.headers["location"].lower()
    assert "attachment" in urllib.request.urlopen(d.headers["location"]).headers["Content-Disposition"]


def test_redirect_mode_sends_everyone_to_the_bucket_while_the_copy_is_identical_and_to_disk_when_it_is_not(tmp_path, storage, video_bytes):
    c, app = make_app(tmp_path, storage, storage_redirect=True)
    jid = wait(c, upload(c, video_bytes).json()["id"])["id"]
    cid = app.state.store.get(jid).clips[0].id
    synced(app, jid, [f"out/{cid}.mp4"])
    assert c.get(f"/api/jobs/{jid}/clips/{cid}/video", follow_redirects=False).status_code == 302
    app.state.store.clip_path(jid, cid).write_bytes(b"newer local render")        # a re-render the bucket does not have yet
    r = c.get(f"/api/jobs/{jid}/clips/{cid}/video", follow_redirects=False)
    assert r.status_code == 200 and r.content == b"newer local render"             # never a stale copy


def test_the_content_security_policy_allows_the_bucket_origin_for_media_only_when_storage_is_on(tmp_path, storage):
    c, _ = make_app(tmp_path, storage)
    csp = c.get("/api/config").headers["content-security-policy"]
    assert f"media-src 'self' {storage.origin};" in csp and "default-src 'none'" in csp and "script-src 'self';" in csp
    plain = TestClient(create_app(tmp_path / "plain", FakePipeline()))
    assert "media-src 'self';" in plain.get("/api/config").headers["content-security-policy"]


def test_other_people_never_get_a_link_to_my_clip(tmp_path, storage, video_bytes):
    a = Accounts(tmp_path / "data" / "x.db")
    cat = Catalog(plans={"free": Plan("free", "Free", 100, 7, 3)})
    app = create_app(tmp_path / "data", FakePipeline(), storage=storage, accounts=a, catalog=cat)
    c, other = TestClient(app), TestClient(app)
    c.post("/api/auth/register", json={"email": "a@example.com", "password": PW})
    other.post("/api/auth/register", json={"email": "b@example.com", "password": PW})
    jid = wait(c, upload(c, video_bytes).json()["id"])["id"]
    cid = app.state.store.get(jid).clips[0].id
    synced(app, jid, [f"out/{cid}.mp4"])
    app.state.store.clip_path(jid, cid).unlink()
    assert c.get(f"/api/jobs/{jid}/clips/{cid}/video", follow_redirects=False).status_code == 302
    assert other.get(f"/api/jobs/{jid}/clips/{cid}/video", follow_redirects=False).status_code == 404


# --- deleting ----------------------------------------------------------------------------------------------------------------

def test_deleting_a_job_removes_its_bucket_copies_first_and_keeps_the_job_if_that_fails(tmp_path, storage, video_bytes, monkeypatch):
    c, app = make_app(tmp_path, storage)
    jid = wait(c, upload(c, video_bytes).json()["id"])["id"]
    synced(app, jid, ["out/edl.json"])
    assert any(jid in k for k in objects(storage))

    def broken(job_id):
        raise StorageError("The file storage refused the request.")
    real = storage.delete_job
    monkeypatch.setattr(storage, "delete_job", broken)
    r = c.delete(f"/api/jobs/{jid}")
    assert r.status_code == 502 and "nothing was deleted" in r.json()["detail"]
    assert c.get(f"/api/jobs/{jid}").status_code == 200 and any(jid in k for k in objects(storage))
    monkeypatch.setattr(storage, "delete_job", real)
    assert c.delete(f"/api/jobs/{jid}").status_code == 204
    assert not any(jid in k for k in objects(storage)) and not (tmp_path / "data" / "jobs" / jid).exists()


def test_deleting_an_account_purges_the_bucket_before_the_database(tmp_path, storage, video_bytes, monkeypatch):
    a = Accounts(tmp_path / "data" / "x.db")
    cat = Catalog(plans={"free": Plan("free", "Free", 100, 7, 3)})
    app = create_app(tmp_path / "data", FakePipeline(), storage=storage, accounts=a, catalog=cat)
    c = TestClient(app)
    c.post("/api/auth/register", json={"email": "a@example.com", "password": PW})
    jid = wait(c, upload(c, video_bytes).json()["id"])["id"]
    synced(app, jid, ["out/edl.json"])
    real = storage.delete_job
    monkeypatch.setattr(storage, "delete_job", lambda j: (_ for _ in ()).throw(StorageError("x")))
    r = c.request("DELETE", "/api/account", json={"password": PW})
    assert r.status_code == 502 and a.count() == 1 and (tmp_path / "data" / "jobs" / jid).exists()     # nothing was deleted
    monkeypatch.setattr(storage, "delete_job", real)
    assert c.request("DELETE", "/api/account", json={"password": PW}).status_code == 200
    assert a.count() == 0 and objects(storage) == []


# --- retention and eviction -----------------------------------------------------------------------------------------------------

def test_retention_removes_bucket_copies_with_the_job_and_retries_when_the_bucket_is_down(tmp_path, storage, video_bytes, monkeypatch):
    c, app = make_app(tmp_path, storage)
    jid = wait(c, upload(c, video_bytes).json()["id"])["id"]
    synced(app, jid, ["out/edl.json"])
    age(app, jid, 40)
    real = storage.delete_job
    monkeypatch.setattr(storage, "delete_job", lambda j: (_ for _ in ()).throw(StorageError("down")))
    assert sweep(app.state.store, lambda j: (30, 0), storage=storage)["jobs"] == 0          # kept: the bucket copies are still there
    assert (tmp_path / "data" / "jobs" / jid).exists()
    monkeypatch.setattr(storage, "delete_job", real)
    assert sweep(app.state.store, lambda j: (30, 0), storage=storage)["jobs"] == 1
    assert objects(storage) == [] and not (tmp_path / "data" / "jobs" / jid).exists()


def test_the_stored_original_goes_with_the_local_one(tmp_path, storage, video_bytes):
    c, app = make_app(tmp_path, storage, backup_source=True)
    jid = wait(c, upload(c, video_bytes).json()["id"])["id"]
    synced(app, jid, ["source.mp4"])
    age(app, jid, 5)
    assert sweep(app.state.store, lambda j: (30, 3), storage=storage)["sources"] == 1
    assert not storage.has(jid, "source.mp4") and "source.mp4" not in app.state.store.get(jid).remote
    assert storage.has(jid, "out/edl.json")                                                  # the rest stays until the job expires


def test_old_local_clips_are_freed_only_when_an_identical_copy_is_in_the_bucket(tmp_path, storage, video_bytes):
    c, app = make_app(tmp_path, storage)
    jid = wait(c, upload(c, video_bytes).json()["id"])["id"]
    cid = app.state.store.get(jid).clips[0].id
    synced(app, jid, [f"out/{c_.id}.mp4" for c_ in app.state.store.get(jid).clips])             # every clip, not just the first
    path = app.state.store.clip_path(jid, cid)
    age(app, jid, 5)
    path.write_bytes(b"changed after the upload")                                          # the bucket copy is now out of date
    # the job has two clips: the untouched one is identical in the bucket and is freed, the changed one is not
    assert sweep(app.state.store, lambda j: (30, 0), storage=storage, local_clip_days=3)["evicted"] == 1 and path.is_file()
    Mirror(storage, app.state.store).backup(jid)
    assert sweep(app.state.store, lambda j: (30, 0), storage=storage, local_clip_days=3)["evicted"] == 1
    assert not path.exists()
    assert c.get(f"/api/jobs/{jid}/clips/{cid}/video", follow_redirects=False).status_code == 302    # still watchable
    assert sweep(app.state.store, lambda j: (30, 0), storage=None, local_clip_days=3)["evicted"] == 0   # never without storage


def test_a_clip_that_was_freed_locally_can_still_be_shared(tmp_path, storage, video_bytes):
    from tests.test_publish import FakeSB
    sb = FakeSB()
    app = create_app(tmp_path / "data", FakePipeline(), storage=storage, sociobird=sb)
    c = TestClient(app)
    jid = wait(c, upload(c, video_bytes).json()["id"])["id"]
    cid = app.state.store.get(jid).clips[0].id
    synced(app, jid, [f"out/{cid}.mp4"])
    app.state.store.clip_path(jid, cid).unlink()
    assert c.post(f"/api/jobs/{jid}/clips/{cid}/publish", json={"channels": ["ch-yt"]}).status_code == 202
    end = time.time() + 10
    while time.time() < end and not sb.posted:
        time.sleep(0.05)
    assert sb.uploaded == [f"{cid}.mp4"] and sb.posted                                       # it was fetched back from the bucket first
