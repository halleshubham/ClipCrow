"""Freeing the server's disk: a finished clip's file leaves the server once an identical copy is verified in the bucket; old jobs can be given to an account and kept."""
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("moto")
from fastapi.testclient import TestClient

from clipcrow.cli import DEFAULT_LOCAL_CLIP_DAYS, local_clip_days_from_env
from clipcrow.service.accounts import Accounts
from clipcrow.service.api import create_app
from clipcrow.service.jobs import Params
from clipcrow.service.plans import Catalog, Plan
from clipcrow.service.retention import evict_local_clips, sweep
from tests.mailhelp import Outbox, sign_up
from tests.test_accounts import age
from tests.test_service import FakePipeline, upload, video_bytes, wait  # noqa: F401
from tests.test_storage import make_app, synced


def test_the_env_setting_means_about_an_hour_by_default_a_number_of_days_or_never():
    assert local_clip_days_from_env("", True) == DEFAULT_LOCAL_CLIP_DAYS and 0 < DEFAULT_LOCAL_CLIP_DAYS < 0.1
    assert local_clip_days_from_env("0", True) == 0.0 and local_clip_days_from_env("2", True) == 2.0
    assert local_clip_days_from_env("off", True) is None and local_clip_days_from_env("never", True) is None
    assert local_clip_days_from_env("", False) is None and local_clip_days_from_env("5", False) is None          # no bucket, nothing to rely on
    assert local_clip_days_from_env("nonsense", True) == DEFAULT_LOCAL_CLIP_DAYS


def clips_synced(app, jid):
    synced(app, jid, [f"out/{c.id}.mp4" for c in app.state.store.get(jid).clips])


def test_zero_days_frees_a_clip_as_soon_as_it_is_in_the_bucket_and_none_keeps_it(tmp_path, storage, video_bytes):
    c, app = make_app(tmp_path, storage)
    jid = wait(c, upload(c, video_bytes).json()["id"])["id"]
    clips_synced(app, jid)
    paths = [app.state.store.clip_path(jid, k.id) for k in app.state.store.get(jid).clips]
    assert sweep(app.state.store, lambda j: (0, 0), storage=storage, local_clip_days=None)["evicted"] == 0 and all(p.is_file() for p in paths)
    assert sweep(app.state.store, lambda j: (0, 0), storage=storage, local_clip_days=0)["evicted"] == len(paths) and not any(p.exists() for p in paths)
    assert c.get(f"/api/jobs/{jid}/clips/{app.state.store.get(jid).clips[0].id}/video", follow_redirects=False).status_code == 302      # still playable, from the bucket


def test_verified_eviction_keeps_a_clip_whose_bucket_copy_is_missing(tmp_path, storage, video_bytes):
    c, app = make_app(tmp_path, storage)
    jid = wait(c, upload(c, video_bytes).json()["id"])["id"]
    clips_synced(app, jid)
    job = app.state.store.get(jid)
    first = job.clips[0].id
    storage.delete(jid, f"out/{first}.mp4")                                   # the record says uploaded, the bucket no longer has it
    r = evict_local_clips(app.state.store, job, storage, verify=True)
    assert r["kept"] == 1 and r["removed"] == len(job.clips) - 1
    assert app.state.store.clip_path(jid, first).is_file()                    # never lose the only copy


def test_a_kept_job_is_not_expired_by_retention(tmp_path, storage, video_bytes):
    c, app = make_app(tmp_path, storage)
    jid = wait(c, upload(c, video_bytes).json()["id"])["id"]
    synced(app, jid, ["out/edl.json"])
    age(app, jid, 400)
    app.state.store.update(jid, lambda j: setattr(j, "keep", True))
    assert sweep(app.state.store, lambda j: (30, 0), storage=storage)["jobs"] == 0 and (tmp_path / "data" / "jobs" / jid).exists()
    app.state.store.update(jid, lambda j: setattr(j, "keep", False))
    assert sweep(app.state.store, lambda j: (30, 0), storage=storage)["jobs"] == 1


def _admin_app(tmp_path, monkeypatch, storage):
    monkeypatch.setenv("CLIPCROW_ADMINS", "boss@example.com")
    cat = Catalog(plans={"free": Plan("free", "Free", 100, 30, 7)})
    a = Accounts(tmp_path / "data" / "clipcrow.db")
    app = create_app(tmp_path / "data", FakePipeline(), accounts=a, mailer=Outbox(), catalog=cat, storage=storage)
    boss, helper = TestClient(app), TestClient(app)
    sign_up(boss, "boss@example.com")
    sign_up(helper, "helper@example.com")
    assert boss.put("/api/admin/settings", json={"values": {"extra_admins": "helper@example.com"}}).status_code == 200
    return app, a, boss, helper


def test_the_operator_sees_what_is_safe_to_remove_and_a_superadmin_removes_it(tmp_path, storage, video_bytes, monkeypatch):
    app, a, boss, helper = _admin_app(tmp_path, monkeypatch, storage)
    jid = wait(boss, upload(boss, video_bytes).json()["id"])["id"]
    clips_synced(app, jid)
    n = len(app.state.store.get(jid).clips)
    rep = boss.get("/api/admin/storage").json()
    assert rep["r2"] is True and rep["clips_safe_to_remove"] == n and rep["clips_not_in_bucket"] == 0 and rep["superadmin"] is True
    assert helper.get("/api/admin/storage").json()["superadmin"] is False
    assert helper.post("/api/admin/storage/cleanup").status_code == 403                                   # looking is for operators, deleting is for a superadmin
    done = boss.post("/api/admin/storage/cleanup").json()
    assert done["removed"] == n and done["kept"] == 0
    assert not any(app.state.store.clip_path(jid, k.id).exists() for k in app.state.store.get(jid).clips)
    assert boss.get("/api/admin/storage").json()["clips_safe_to_remove"] == 0
    assert boss.post("/api/admin/storage/cleanup").json()["removed"] == 0                                 # nothing left to do, nothing harmed
    assert boss.get(f"/api/jobs/{jid}/clips/{app.state.store.get(jid).clips[0].id}/video", follow_redirects=False).status_code == 302


def test_without_a_bucket_nothing_is_removed(tmp_path, video_bytes, monkeypatch):
    monkeypatch.setenv("CLIPCROW_ADMINS", "boss@example.com")
    cat = Catalog(plans={"free": Plan("free", "Free", 100, 30, 7)})
    app = create_app(tmp_path / "data", FakePipeline(), accounts=Accounts(tmp_path / "data" / "c.db"), mailer=Outbox(), catalog=cat)
    boss = TestClient(app)
    sign_up(boss, "boss@example.com")
    r = boss.post("/api/admin/storage/cleanup")
    assert r.status_code == 409 and "nothing to rely on" in r.json()["detail"]


def test_old_jobs_with_no_owner_are_given_to_one_account_and_kept(tmp_path, storage, monkeypatch):
    app, a, boss, helper = _admin_app(tmp_path, monkeypatch, storage)
    store = app.state.store
    old = [store.create(f"old{i}.mp4", Params()) for i in range(3)]
    for j in old:
        store.update(j.id, lambda x: setattr(x, "status", "done"))
    assert boss.get("/api/admin/storage").json()["unowned_jobs"] == 3
    assert helper.post("/api/admin/jobs/claim", json={"email": "boss@example.com"}).status_code == 403
    assert boss.post("/api/admin/jobs/claim", json={"email": "nobody@example.com"}).status_code == 404
    assert boss.post("/api/admin/jobs/claim", json={"email": "Boss@Example.com"}).json() == {"assigned": 3}
    uid = a.by_email("boss@example.com").id
    assert all(store.get(j.id).owner == uid and store.get(j.id).keep for j in old)
    assert {j["id"] for j in boss.get("/api/jobs").json()} >= {j.id for j in old}                          # they are in the account's list now
    assert all(j.id not in {x["id"] for x in helper.get("/api/jobs").json()} for j in old)              # and nobody else's
    assert boss.post("/api/admin/jobs/claim", json={"email": "boss@example.com"}).json() == {"assigned": 0}   # safe to repeat
    assert boss.get("/api/admin/storage").json()["unowned_jobs"] == 0
