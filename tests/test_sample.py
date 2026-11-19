"""The sample job and the welcome mail a new account gets."""
import json
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from clipcrow.service.accounts import Accounts
from clipcrow.service.api import create_app
from clipcrow.service.jobs import JobStore
from clipcrow.service.plans import Catalog, Plan
from clipcrow.service.retention import sweep
from clipcrow.service.sample import export_sample, import_sample
from tests.mailhelp import Outbox, sign_up
from tests.test_service import FakePipeline, upload, video_bytes, wait  # noqa: F401


def cat():
    return Catalog(plans={"free": Plan("free", "Free", 30, 7, 3)})


@pytest.fixture
def sample(tmp_path, video_bytes):
    """A finished job from a real (fake-pipeline) run, exported as a sample folder."""
    src = TestClient(create_app(tmp_path / "src", FakePipeline()))
    jid = wait(src, upload(src, video_bytes).json()["id"])["id"]
    folder = tmp_path / "sample"
    n = export_sample(JobStore(tmp_path / "src"), jid, folder)
    assert n > 0
    return folder


def client(tmp_path, sample_dir=""):
    a = Accounts(tmp_path / "data" / "clipcrow.db")
    app = create_app(tmp_path / "data", FakePipeline(), accounts=a, mailer=Outbox(), catalog=cat(), sample_dir=str(sample_dir),
                     public_url="https://clipcrow.example")
    return TestClient(app), a, app


def test_a_new_account_gets_one_welcome_mail_and_a_finished_sample_job_that_costs_nothing(tmp_path, sample):
    c, a, app = client(tmp_path, sample)
    sign_up(c, "new@example.com")
    jobs = c.get("/api/jobs").json()
    assert len(jobs) == 1 and jobs[0]["status"] == "done" and jobs[0]["name"].startswith("Sample: ") and jobs[0]["clips"]
    assert jobs[0]["owner"] and jobs[0]["sample"] is True and jobs[0]["reserved_minutes"] == 0 and jobs[0]["expires_at"] is None        # "kept until" would be a lie
    assert c.get("/api/me").json()["used_minutes"] == 0                                           # no minutes were taken
    clip = jobs[0]["clips"][0]
    assert c.get(f"/api/jobs/{jobs[0]['id']}/clips/{clip['id']}/video").status_code in (200, 206)    # the clip really plays
    welcome = [m for m in app.state.outbox.sent if m.kind == "welcome"]
    assert len(welcome) == 1 and welcome[0].to == "new@example.com" and f"https://clipcrow.example/dashboard#/jobs/{jobs[0]['id']}" in welcome[0].text
    assert "30 minutes" in welcome[0].text
    sign_up(TestClient(app), "new@example.com")                                                    # signing in again: no second sample, no second welcome
    assert len(c.get("/api/jobs").json()) == 1 and len([m for m in app.state.outbox.sent if m.kind == "welcome"]) == 1


def test_another_person_never_sees_someone_elses_sample(tmp_path, sample):
    c, a, app = client(tmp_path, sample)
    sign_up(c, "a@example.com")
    other = TestClient(app)
    sign_up(other, "b@example.com")
    mine, theirs = c.get("/api/jobs").json()[0]["id"], other.get("/api/jobs").json()[0]["id"]
    assert mine != theirs and other.get(f"/api/jobs/{mine}").status_code == 404


def test_without_a_sample_the_welcome_mail_still_goes_and_says_nothing_about_one(tmp_path):
    c, a, app = client(tmp_path)
    sign_up(c, "new@example.com")
    assert c.get("/api/jobs").json() == []
    mail = next(m for m in app.state.outbox.sent if m.kind == "welcome")
    assert "sample job" not in mail.text


def test_a_broken_sample_never_stops_a_sign_in(tmp_path, sample):
    (sample / "job.json").write_text("not json")
    c, a, app = client(tmp_path, sample)
    sign_up(c, "new@example.com")
    assert c.get("/api/jobs").json() == [] and c.get("/api/me").status_code == 200
    assert any(m.kind == "welcome" for m in app.state.outbox.sent)


def test_only_plain_files_are_copied_and_no_original_or_bookkeeping_is_exported(tmp_path, sample):
    store = JobStore(tmp_path / "data")
    (sample / "out" / "evil").mkdir()
    (sample / "out" / "link.mp4").symlink_to("/etc/passwd")
    (sample / "out" / "..sneaky").write_bytes(b"x")
    job = import_sample(store, sample, "owner1")
    names = {p.name for p in (store.dir(job.id) / "out").iterdir()}
    assert "evil" not in names and "link.mp4" not in names and "..sneaky" not in names
    assert not list(store.dir(job.id).glob("source.*")) and job.source_removed is True
    assert not {"posts.json", "gpu_usage.json", "clean.mp4"} & names


def test_the_export_refuses_a_job_that_is_not_finished(tmp_path, video_bytes):
    store = JobStore(tmp_path)
    job = store.create("x", __import__("clipcrow.service.jobs", fromlist=["Params"]).Params())
    with pytest.raises(ValueError):
        export_sample(store, job.id, tmp_path / "out")


def test_retention_never_deletes_a_sample(tmp_path, sample):
    store = JobStore(tmp_path / "data")
    job = import_sample(store, sample, "owner1")
    far = datetime.now(timezone.utc) + timedelta(days=400)
    assert sweep(store, lambda j: (7, 3), now=far)["jobs"] == 0
    assert store.get(job.id).sample is True
