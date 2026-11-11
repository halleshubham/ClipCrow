import subprocess
import threading
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("multipart")
from fastapi.testclient import TestClient

from clipcrow.make import Cancelled, make
from clipcrow.service.api import create_app
from clipcrow.service.pipelines import planner_options
from tests.test_planner import make_transcript, scripted


@pytest.fixture(scope="module")
def video_bytes(tmp_path_factory):
    p = tmp_path_factory.mktemp("v") / "talk.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=90x160:r=30:d=250",
                    "-f", "lavfi", "-i", "sine=f=220:d=250", "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                    "-g", "30", "-pix_fmt", "yuv420p", "-c:a", "aac", str(p)], check=True)
    return p.read_bytes()


class FakePipeline:
    """The real make() with fakes for the slow parts: no Whisper, no API, no browser."""

    def __init__(self, ready=None, gate: threading.Event | None = None, boom: Exception | None = None):
        self.ready, self.gate, self.boom = ready, gate, boom

    def check_ready(self):
        return self.ready

    def run(self, job, video, out, hooks, say):
        if self.boom:
            raise self.boom
        if self.gate:
            while not self.gate.wait(0.02):
                hooks.check()

        def fake_render(edl, clip_id, path, **kw):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"0123456789" * 1000)
            return path, None

        return make(video, out, llm=scripted(), opts=planner_options(job), hooks=hooks, say=say,
                    transcribe_fn=lambda media, language=None, progress=None: make_transcript(),
                    render_fn=fake_render)


def client(tmp_path, pipeline=None, **kw):
    app = create_app(tmp_path / "data", pipeline or FakePipeline(), **kw)
    return TestClient(app), app


def upload(c, video_bytes, name="ep12.mp4", **form):
    data = {"rights": "true", "clips": "2", "min_clip": "20", "max_clip": "60", **form}
    return c.post("/api/jobs", data=data, files={"file": (name, video_bytes, "video/mp4")})


def wait(c, job_id, status=("done", "failed", "cancelled"), timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        j = c.get(f"/api/jobs/{job_id}").json()
        if j["status"] in status:
            return j
        time.sleep(0.1)
    raise AssertionError(f"job still {j['status']}")


# --- happy path ------------------------------------------------------------------------------

def test_a_job_runs_end_to_end_and_the_clips_can_be_played_and_downloaded(tmp_path, video_bytes):
    c, _ = client(tmp_path)
    r = upload(c, video_bytes)
    assert r.status_code == 201
    job = wait(c, r.json()["id"])
    assert job["status"] == "done" and job["error"] is None
    assert [s["state"] for s in job["stages"]] == ["done"] * 4
    assert job["source"]["duration"] > 240 and len(job["clips"]) == 2 and all(k["ready"] for k in job["clips"])
    k = job["clips"][0]
    assert k["title"] and k["hook"] and "cut points" in k["ai_decided"]

    v = c.get(f"/api/jobs/{job['id']}/clips/{k['id']}/video")
    assert v.status_code == 200 and v.headers["content-type"] == "video/mp4" and len(v.content) == 10000
    part = c.get(f"/api/jobs/{job['id']}/clips/{k['id']}/video", headers={"Range": "bytes=100-199"})
    assert part.status_code == 206 and len(part.content) == 100                 # the browser can seek
    d = c.get(f"/api/jobs/{job['id']}/clips/{k['id']}/download")
    assert 'attachment; filename="ep12-c1.mp4"' in d.headers["content-disposition"]


def test_usage_endpoint_reports_calls_assets_and_disclosure(tmp_path, video_bytes):
    c, _ = client(tmp_path)
    job = wait(c, upload(c, video_bytes).json()["id"])
    u = c.get(f"/api/jobs/{job['id']}/usage").json()
    assert len(u["calls"]) >= 2 and u["disclosure_recommended"] is False
    assert [a["id"] for a in u["assets"]] == ["source"] and u["clips"][0]["decided"]


def test_jobs_are_listed_newest_first_and_survive_a_restart(tmp_path, video_bytes):
    c, _ = client(tmp_path)
    a = wait(c, upload(c, video_bytes).json()["id"])["id"]
    b = wait(c, upload(c, video_bytes).json()["id"])["id"]
    assert [j["id"] for j in c.get("/api/jobs").json()] == [b, a]
    c2, _ = client(tmp_path)                                                    # same data dir, new process
    assert [j["id"] for j in c2.get("/api/jobs").json()] == [b, a]


# --- input checks ------------------------------------------------------------------------------

def test_the_rights_confirmation_is_required(tmp_path, video_bytes):
    c, _ = client(tmp_path)
    r = upload(c, video_bytes, rights="")
    assert r.status_code == 400 and "rights" in r.json()["detail"]


@pytest.mark.parametrize("name,payload,code,text", [
    ("notes.txt", b"hello", 400, "MP4"),
    ("fake.mp4", b"this is not a video at all", 400, "could not be read"),
])
def test_bad_uploads_are_refused_and_leave_nothing_behind(tmp_path, name, payload, code, text):
    c, app = client(tmp_path)
    r = upload(c, payload, name=name)
    assert r.status_code == code and text in r.json()["detail"]
    assert app.state.store.list() == []


def test_a_video_too_short_for_the_requested_clips_is_refused(tmp_path):
    p = tmp_path / "s.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=90x160:r=30:d=10",
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", str(p)], check=True)
    c, app = client(tmp_path)
    r = upload(c, p.read_bytes())
    assert r.status_code == 400 and "longer video" in r.json()["detail"] and app.state.store.list() == []


def test_bad_options_are_a_clear_422(tmp_path, video_bytes):
    c, _ = client(tmp_path)
    assert upload(c, video_bytes, clips="99").status_code == 422
    assert upload(c, video_bytes, min_clip="60", max_clip="30").status_code == 422
    assert upload(c, video_bytes, language="not a language").status_code == 422


def test_the_upload_size_limit_is_enforced(tmp_path):
    c, app = client(tmp_path, max_upload_mb=1)
    r = upload(c, b"x" * (3 * 1024 * 1024))
    assert r.status_code == 413 and app.state.store.list() == []


def test_jobs_cannot_start_when_the_server_is_not_ready(tmp_path, video_bytes):
    c, _ = client(tmp_path, FakePipeline(ready="No Claude credentials on the server: set ANTHROPIC_API_KEY."))
    r = upload(c, video_bytes)
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]
    assert c.get("/api/config").json()["ready"].startswith("No Claude credentials")


# --- paths -------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/api/jobs/..%2f..%2fetc", "/api/jobs/....//", "/api/jobs/ABCDEF123456", "/api/jobs/short",
    "/api/jobs/000000000000/clips/c1/video"])
def test_malformed_ids_are_just_not_found(tmp_path, path):
    c, _ = client(tmp_path)
    assert c.get(path).status_code == 404


def test_clip_ids_cannot_escape_the_job_directory(tmp_path, video_bytes):
    c, _ = client(tmp_path)
    job = wait(c, upload(c, video_bytes).json()["id"])
    for bad in ("..%2fsource.mp4", "c1%2f..%2f..%2fjob.json", "c1.mp4", "C1", "c1000"):
        assert c.get(f"/api/jobs/{job['id']}/clips/{bad}/video").status_code == 404, bad
    assert c.get(f"/api/jobs/{job['id']}/clips/c9/video").status_code == 404          # well formed, not there


# --- auth --------------------------------------------------------------------------------------

def test_with_a_token_the_api_needs_a_login(tmp_path, video_bytes):
    c, _ = client(tmp_path, token="s3cret-token")
    assert c.get("/api/jobs").status_code == 401
    cfg = c.get("/api/config").json()
    assert cfg["needs_login"] is True and cfg["signed_in"] is False
    assert c.post("/api/login", json={"token": "wrong"}).status_code == 401
    ok = c.post("/api/login", json={"token": "s3cret-token"})
    assert ok.status_code == 200 and "httponly" in ok.headers["set-cookie"].lower()
    assert "samesite=strict" in ok.headers["set-cookie"].lower()
    assert c.get("/api/jobs").status_code == 200 and c.get("/api/config").json()["signed_in"] is True
    c.cookies.clear()
    assert c.get("/api/jobs", headers={"Authorization": "Bearer s3cret-token"}).status_code == 200
    assert upload(c, video_bytes).status_code == 401                                   # uploads are protected too


def test_without_a_token_the_login_endpoint_does_not_pretend(tmp_path):
    c, _ = client(tmp_path)
    assert c.post("/api/login", json={"token": ""}).status_code == 401


# --- running, cancelling, failing ---------------------------------------------------------------

def test_a_running_job_can_be_cancelled(tmp_path, video_bytes):
    gate = threading.Event()
    c, _ = client(tmp_path, FakePipeline(gate=gate))
    jid = upload(c, video_bytes).json()["id"]
    wait(c, jid, status=("running",))
    assert c.post(f"/api/jobs/{jid}/cancel").status_code == 200
    assert wait(c, jid)["status"] == "cancelled"
    gate.set()


def test_a_queued_job_cancelled_before_it_starts_never_runs(tmp_path, video_bytes):
    gate = threading.Event()
    c, app = client(tmp_path, FakePipeline(gate=gate))
    first = upload(c, video_bytes).json()["id"]
    wait(c, first, status=("running",))
    second = upload(c, video_bytes).json()
    assert second["status"] == "queued" and second["queue_position"] == 1
    c.post(f"/api/jobs/{second['id']}/cancel")
    gate.set()
    assert wait(c, first)["status"] == "done"
    time.sleep(0.3)
    assert c.get(f"/api/jobs/{second['id']}").json()["status"] == "cancelled"


def test_an_expected_failure_shows_its_message_an_unexpected_one_hides_internals(tmp_path, video_bytes):
    c, _ = client(tmp_path, FakePipeline(boom=ValueError("No speech was recognised in this video.")))
    j = wait(c, upload(c, video_bytes).json()["id"])
    assert j["status"] == "failed" and j["error"] == "No speech was recognised in this video."

    c2, _ = client(tmp_path / "b", FakePipeline(boom=RuntimeError("open('/srv/secret/path') failed")))
    j2 = wait(c2, upload(c2, video_bytes).json()["id"])
    assert j2["status"] == "failed" and "/srv/secret" not in j2["error"] and "RuntimeError" in j2["error"]


def test_a_running_job_cannot_be_deleted_a_finished_one_can(tmp_path, video_bytes):
    gate = threading.Event()
    c, app = client(tmp_path, FakePipeline(gate=gate))
    jid = upload(c, video_bytes).json()["id"]
    wait(c, jid, status=("running",))
    assert c.delete(f"/api/jobs/{jid}").status_code == 409
    gate.set()
    wait(c, jid)
    assert c.delete(f"/api/jobs/{jid}").status_code == 204
    assert c.get(f"/api/jobs/{jid}").status_code == 404 and not (app.state.store.root / jid).exists()


def test_after_a_restart_interrupted_jobs_fail_and_queued_jobs_run(tmp_path, video_bytes):
    c, app = client(tmp_path, start_runner=False)
    store = app.state.store
    from clipcrow.service.jobs import Params
    running = store.create("a.mp4", Params())
    running.status = "running"
    store.save(running)
    queued = store.create("b.mp4", Params())
    (store.dir(queued.id) / "source.mp4").write_bytes(video_bytes)
    c2, _ = client(tmp_path)                                                           # restart: recover() runs
    assert c2.get(f"/api/jobs/{running.id}").json()["status"] == "failed"
    assert "restarted" in c2.get(f"/api/jobs/{running.id}").json()["error"]
    assert wait(c2, queued.id)["status"] == "done"


def test_responses_carry_a_strict_content_security_policy(tmp_path):
    c, _ = client(tmp_path)
    r = c.get("/api/config")
    assert "default-src 'none'" in r.headers["content-security-policy"] and "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
    assert c.get("/").status_code == 200                                               # the UI is served


# --- the UI's own safety rules ---------------------------------------------------------------------

def test_the_ui_never_builds_markup_from_strings():
    """Job data (file names, model text) must only ever be inserted as text. This guards that rule."""
    from clipcrow.service.api import STATIC
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert banned not in js, banned
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "onclick=" not in html and "<script>" not in html and 'style="' not in html          # CSP: no inline code
    assert "http://" not in js and "https://" not in js and "//cdn" not in html                 # nothing third-party


def test_the_ui_files_are_served(tmp_path):
    c, _ = client(tmp_path)
    for path, kind in (("/", "text/html"), ("/app.js", "javascript"), ("/style.css", "text/css")):
        r = c.get(path)
        assert r.status_code == 200 and kind in r.headers["content-type"], path


def test_usage_calls_are_in_a_stable_order(tmp_path, video_bytes):
    c, _ = client(tmp_path)
    job = wait(c, upload(c, video_bytes).json()["id"])
    purposes = [x["purpose"] for x in c.get(f"/api/jobs/{job['id']}/usage").json()["calls"]]
    assert purposes[0] == "score"
    details = [int(p.split(":")[1]) for p in purposes[1:]]
    assert details == sorted(details)
