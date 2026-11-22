"""Job records wherever they live: the files, both, or the database only. The same lifecycle, the same answers, and no lost update when writers race."""
import json
import threading

import pytest

from clipcrow.service import db as dbm
from clipcrow.service.docstore import DocStore
from clipcrow.service.jobs import Job, JobStore, Params
from tests.test_service import video_bytes  # noqa: F401  (fixture)

OWNER = "a" * 32
OTHER = "b" * 32
PG = __import__("os").environ.get("CLIPCROW_TEST_DATABASE_URL")


@pytest.fixture(params=["files", "both", "db"])
def kit(request, tmp_path):
    db = dbm.connect(tmp_path / "x.db")
    docs = DocStore(db)
    yield request.param, tmp_path, docs, JobStore(tmp_path, docs, request.param)
    db.close()


def test_a_job_through_its_whole_life(kit):
    mode, tmp, docs, st = kit
    a = st.create("Podcast ep 12", Params())
    a = st.update(a.id, lambda j: (setattr(j, "owner", OWNER), setattr(j, "status", "running")))
    b = st.create("Another", Params())
    st.update(b.id, lambda j: setattr(j, "owner", OTHER))
    assert st.exists(a.id) and not st.exists("0" * 12) and st.get(a.id).status == "running" and st.get(a.id).owner == OWNER
    assert [j.id for j in st.list()] == [b.id, a.id]                                            # newest first
    briefs = {x.id: x for x in st.briefs()}
    assert briefs[a.id].status == "running" and briefs[a.id].owner == OWNER and briefs[b.id].owner == OTHER
    st.update(a.id, lambda j: setattr(j, "status", "done"))
    assert {x.id: x.status for x in st.briefs()}[a.id] == "done"                                # the cached view follows a change
    with pytest.raises(KeyError):
        st.get("f" * 12)
    with pytest.raises(KeyError):
        st.update("f" * 12, lambda j: None)
    st.add_posts(a.id, [{"id": "p1", "clip": "c1", "status": "queued", "detail": ""}, {"id": "p2", "clip": "c2", "status": "queued", "detail": ""}])
    st.update_post(a.id, "p1", "posted", "ok" * 200)
    assert [(p["id"], p["status"]) for p in st.posts(a.id)] == [("p1", "posted"), ("p2", "queued")] and len(st.posts(a.id)[0]["detail"]) == 300
    assert [p["id"] for p in st.posts(a.id, "c2")] == ["p2"] and st.posts(b.id) == []
    st.add_gpu_usage(a.id, {"kind": "render", "worker": "w", "seconds": 4.5, "ok": True, "rate": 45, "at": "x"})
    st.add_gpu_usage(a.id, {"kind": "asr", "worker": "w", "seconds": 1, "ok": True, "rate": 45, "at": "y"})
    assert [r["kind"] for r in st.gpu_usage(a.id)] == ["render", "asr"]
    st.delete(a.id)
    assert not st.exists(a.id) and [j.id for j in st.list()] == [b.id] and st.posts(a.id) == [] and st.gpu_usage(a.id) == []
    if mode != "files":
        assert docs.count("job_posts") == 0 and docs.count("job_gpu") == 0


def test_a_restart_reads_what_was_written(kit):
    mode, tmp, docs, st = kit
    j = st.create("Persist", Params())
    st.update(j.id, lambda x: setattr(x, "name", "तुमची झोप"))
    again = JobStore(tmp, docs, mode)
    assert again.get(j.id).name == "तुमची झोप" and [b.id for b in again.briefs()] == [j.id]


def test_both_writes_the_same_text_to_the_file_and_the_database(kit):
    mode, tmp, docs, st = kit
    if mode != "both":
        pytest.skip("both only")
    j = st.create("Same", Params())
    st.update(j.id, lambda x: setattr(x, "status", "running"))
    st.add_posts(j.id, [{"id": "p", "clip": "c1", "status": "queued", "detail": ""}])
    assert json.loads((tmp / "jobs" / j.id / "job.json").read_text()) == json.loads(st.rows.get(j.id)[0])
    assert json.loads((tmp / "jobs" / j.id / "out" / "posts.json").read_text()) == json.loads(docs.text("job_posts", "", j.id))


def test_racing_writers_lose_nothing_in_the_database(tmp_path):
    """Eight threads each bump a counter 25 times and add 10 posts: every change is there (no process lock; a version check and a retry)."""
    db = dbm.connect(tmp_path / "x.db")
    docs = DocStore(db)
    st = JobStore(tmp_path, docs, "db")
    j = st.create("Race", Params())
    errors = []

    def work(n):
        try:
            for _ in range(25):
                st.update(j.id, lambda x: setattr(x, "attempts", x.attempts + 1))
            st.add_posts(j.id, [{"id": f"{n}-{i}", "clip": "c1", "status": "queued", "detail": ""} for i in range(10)])
        except Exception as e:                    # noqa: BLE001
            errors.append(e)
    ts = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors and st.get(j.id).attempts == 200 and len(st.posts(j.id)) == 80
    db.close()


@pytest.mark.skipif(not PG, reason="needs CLIPCROW_TEST_DATABASE_URL")
def test_the_same_races_on_postgresql():
    import uuid
    import tempfile
    from pathlib import Path
    db = dbm.connect(PG, schema="t_" + uuid.uuid4().hex[:16])
    docs = DocStore(db)
    with tempfile.TemporaryDirectory() as d:
        st = JobStore(Path(d), docs, "db")
        j = st.create("Race", Params())
        errors = []

        def work():
            try:
                for _ in range(25):
                    st.update(j.id, lambda x: setattr(x, "attempts", x.attempts + 1))
            except Exception as e:                # noqa: BLE001
                errors.append(e)
        ts = [threading.Thread(target=work) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert not errors and st.get(j.id).attempts == 200
        assert [b.id for b in st.briefs()] == [j.id]
    db.drop_schema()


def test_migrate_moves_jobs_and_their_lists_and_verify_agrees(tmp_path):
    from clipcrow.service import storemigrate
    from clipcrow.service.jobrows import JobRows
    files = JobStore(tmp_path)
    a = files.create("One", Params())
    files.update(a.id, lambda j: setattr(j, "owner", OWNER))
    files.add_posts(a.id, [{"id": "p", "clip": "c1", "status": "queued", "detail": ""}])
    files.add_gpu_usage(a.id, {"kind": "render", "worker": "w", "seconds": 2, "ok": True, "rate": 45, "at": "x"})
    (tmp_path / "jobs" / ("c" * 12)).mkdir()
    (tmp_path / "jobs" / ("c" * 12) / "job.json").write_text("{not json")                         # a damaged record is skipped, not fatal
    db = dbm.connect(tmp_path / "x.db")
    docs, rows = DocStore(db), JobRows(db)
    rep = storemigrate.migrate(tmp_path, docs, rows)
    assert rep["job"] == 1 and rep["job_posts"] == 1 and rep["job_gpu"] == 1
    check = storemigrate.verify(tmp_path, docs, rows)
    assert not (check["different"] or check["only_in_db"]) and check["only_in_files"] == [] and check["same"] == 3
    asdb = JobStore(tmp_path, docs, "db")
    assert asdb.get(a.id).owner == OWNER and asdb.posts(a.id)[0]["id"] == "p" and asdb.gpu_usage(a.id)[0]["seconds"] == 2
    assert storemigrate.migrate(tmp_path, docs, rows) == {"unchanged": 3}
    db.close()


@pytest.mark.parametrize("mode", ["files", "both", "db"])
def test_a_real_job_runs_through_the_api_in_every_mode(tmp_path, monkeypatch, mode, video_bytes):
    pytest.importorskip("fastapi")
    monkeypatch.setenv("CLIPCROW_STORE", mode)
    from tests.test_accounts import app_client, signup
    from tests.test_service import upload, wait
    c, accounts, app = app_client(tmp_path)
    signup(c, "who@example.com")
    job = wait(c, upload(c, video_bytes).json()["id"])
    assert job["status"] == "done" and job["clips"]
    assert [j["id"] for j in c.get("/api/jobs").json()] == [job["id"]]
    assert c.get("/api/health").json() == {"ok": True, "busy": False}
    if mode != "files":
        assert accounts.db.q("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 1
    assert c.delete(f"/api/jobs/{job['id']}").status_code in (200, 204)
    assert c.get("/api/jobs").json() == []
