"""The person's own pictures for an explainer: uploaded, checked, looked at by the model, placed by number, rendered."""
import base64
import re
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("multipart")

from clipcrow.assets.fetch import fetch_assets
from clipcrow.make import Hooks, make
from clipcrow.planner import FakeLLM, PlannerOptions, plan_edl
from clipcrow.planner.explainer import plan_beats, thumbnail
from clipcrow.planner.models import BeatsResult
from clipcrow.planner.transcript import Sentence
from clipcrow.service.pictures import MAX_DRAFTS, MAX_SIDE, PictureError, PictureStore, parse_ids
from tests.test_accounts import app_client, signup
from tests.test_branding import png
from tests.test_explainer import FakeImages, sentences  # noqa: F401
from tests.test_planner import SRC, make_transcript, scripted
from tests.test_service import client, upload, video_bytes, wait  # noqa: F401

OWNER = "a" * 32


@pytest.fixture
def store(tmp_path):
    return PictureStore(tmp_path)


def pic(tmp_path, name="p.png", size="400x300"):
    return png(tmp_path / name, size).read_bytes()


# --- the store ------------------------------------------------------------------------------------------------

def test_a_picture_is_re_encoded_and_given_a_strict_id(store, tmp_path):
    out = store.add(OWNER, pic(tmp_path))
    assert re.fullmatch(r"[0-9a-f]{12}", out["id"]) and (out["width"], out["height"]) == (400, 300)
    stored = store.path(OWNER, out["id"])
    assert stored.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_a_big_picture_is_scaled_down_and_a_tiny_or_huge_one_refused(store, tmp_path):
    out = store.add(OWNER, pic(tmp_path, "big.png", "3000x1500"))
    assert max(out["width"], out["height"]) <= MAX_SIDE
    for bad, msg in ((b"", "empty"), (b"<svg onload=alert(1)/>", "SVG"), (b"GIF89a" + b"0" * 100, "SVG"),
                     (b"\x89PNG\r\n\x1a\n" + b"x" * 60, "pixels"), (pic(tmp_path, "t.png", "20x20"), "pixels"),
                     (pic(tmp_path) + b"0" * (10 * 1024 * 1024), "at most")):
        with pytest.raises(PictureError, match=msg):
            store.add(OWNER, bad)


def test_drafts_are_limited_adopted_into_a_job_and_expire(store, tmp_path):
    data = pic(tmp_path)
    ids = [store.add(OWNER, data)["id"] for _ in range(3)]
    for k in range(MAX_DRAFTS - 3):
        (tmp_path / "pictures" / OWNER / f"{k:012x}.png").write_bytes(b"x")
    with pytest.raises(PictureError, match="many unused"):
        store.add(OWNER, data)
    job = tmp_path / "jobs" / "j1" / "pictures"
    assert store.adopt(OWNER, [ids[1], ids[0], "f" * 12], job) == [ids[1], ids[0]]       # in the order given; a missing one is skipped
    assert (job / f"{ids[0]}.png").is_file() and store.path(OWNER, ids[0]) is None and store.path(OWNER, ids[2]) is not None
    assert store.expire(now=time.time() + 25 * 3600) >= 1 and store.path(OWNER, ids[2]) is None


def test_ids_and_owners_cannot_escape_the_folder(store, tmp_path):
    for bad in ("../x", "a/b", "", "A" * 12, "g" * 12, "a" * 12 + "\n"):
        assert store.path(OWNER, bad) is None
    for owner in ("../x", "", "x" * 31):
        with pytest.raises(KeyError):
            store.count(owner)


def test_the_option_lists_ids_with_no_repeats_and_at_most_six():
    assert parse_ids("") == [] and parse_ids(None) == [] and parse_ids("a" * 12 + ", " + "b" * 12) == ["a" * 12, "b" * 12]
    for bad in ("a" * 12 + "," + "a" * 12, "xyz", ",".join(f"{i:012x}" for i in range(7)), "../etc"):
        with pytest.raises(PictureError):
            parse_ids(bad)


# --- the routes ----------------------------------------------------------------------------------------------------

def test_a_person_adds_views_and_removes_a_picture(tmp_path):
    c, _ = client(tmp_path)
    r = c.post("/api/pictures", files={"file": ("shot.png", pic(tmp_path), "image/png")})
    assert r.status_code == 201 and r.json()["url"] == f"/api/pictures/{r.json()['id']}"
    pid = r.json()["id"]
    got = c.get(f"/api/pictures/{pid}")
    assert got.status_code == 200 and got.headers["content-type"] == "image/png" and got.headers["cache-control"] == "no-store"
    assert c.post("/api/pictures", files={"file": ("x.svg", b"<svg/>", "image/svg+xml")}).status_code == 422
    assert c.get("/api/pictures/nothere").status_code == 404 and c.get("/api/pictures/" + "0" * 12).status_code == 404
    assert c.delete(f"/api/pictures/{pid}").status_code == 204 and c.get(f"/api/pictures/{pid}").status_code == 404
    assert c.delete(f"/api/pictures/{pid}").status_code == 404


def test_one_account_cannot_see_or_use_anothers_pictures(tmp_path, video_bytes):
    c, _, _ = app_client(tmp_path)
    signup(c, "a@example.com")
    pid = c.post("/api/pictures", files={"file": ("s.png", pic(tmp_path), "image/png")}).json()["id"]
    c.post("/api/auth/logout")
    signup(c, "b@example.com")
    assert c.get(f"/api/pictures/{pid}").status_code == 404 and c.delete(f"/api/pictures/{pid}").status_code == 404
    r = upload(c, video_bytes, explainer="true", pictures=pid)
    assert r.status_code == 422 and "no longer available" in r.json()["detail"]


def test_a_job_takes_the_chosen_pictures_and_the_drafts_are_gone(tmp_path, video_bytes):
    c, app = client(tmp_path)
    ids = [c.post("/api/pictures", files={"file": ("s.png", pic(tmp_path, f"{i}.png"), "image/png")}).json()["id"] for i in range(2)]
    r = upload(c, video_bytes, explainer="true", pictures=",".join(ids))
    assert r.status_code == 201 and r.json()["params"]["pictures"] == ids
    job = r.json()["id"]
    wait(c, job)
    for pid in ids:
        assert (tmp_path / "data" / "jobs" / job / "pictures" / f"{pid}.png").is_file()
        assert c.get(f"/api/pictures/{pid}").status_code == 404


def test_pictures_need_the_explainer_and_must_exist_and_the_old_links_option_is_refused(tmp_path, video_bytes):
    c, _ = client(tmp_path)
    pid = c.post("/api/pictures", files={"file": ("s.png", pic(tmp_path), "image/png")}).json()["id"]
    assert upload(c, video_bytes, pictures=pid).status_code == 422                                        # explainer is off
    assert upload(c, video_bytes, explainer="true", pictures="b" * 12).status_code == 422                  # unknown
    assert upload(c, video_bytes, explainer="true", pictures="not-an-id").status_code == 422
    assert upload(c, video_bytes, explainer="true", pictures=",".join(f"{i:012x}" for i in range(7))).status_code == 422
    assert upload(c, video_bytes, explainer="true", links="https://example.com/").status_code == 422
    assert c.get("/api/jobs").json() == [] and c.get(f"/api/pictures/{pid}").status_code == 200           # a refused submit costs the person nothing


# --- the model looks and places by number ------------------------------------------------------------------------

JPEG = b"\xff\xd8\xff\xe0" + b"j" * 200


def pic_beat(sentence, picture, kind="picture", caption="बातमी"):
    return dict(sentence=sentence, kind=kind, link=None, picture=picture, prompt="", caption=caption)


def run(raw, pictures=(JPEG, JPEG), **kw):
    seen = {}

    def respond(purpose, prompt, schema):
        seen["prompt"] = prompt
        return BeatsResult(beats=raw)
    out = plan_beats(FakeLLM(respond), sentences(), 100.0, 32.0, [], allow_images=False, language="mr", hook_end=2.8,
                     purpose="beats:c1", pictures=list(pictures), **kw)
    return out, seen["prompt"]


def test_the_model_is_shown_each_picture_with_its_number_then_the_instructions():
    (beats, warnings), prompt = run([pic_beat(2, 0)])
    assert isinstance(prompt, list) and prompt[-1]["type"] == "text" and "<transcript>" in prompt[-1]["text"]
    labels = [b["text"] for b in prompt if b["type"] == "text"][:-1]
    images = [b for b in prompt if b["type"] == "image"]
    assert labels == ["[P0]", "[P1]"] and len(images) == 2
    assert base64.b64decode(images[0]["source"]["data"]) == JPEG and images[0]["source"]["media_type"] == "image/jpeg"
    assert "LOOK at each picture" in prompt[-1]["text"] and "[L0]" not in prompt[-1]["text"]
    assert [(b.kind, b.picture) for b in beats] == [("upload", 0)] and warnings == []


def test_a_picture_is_used_once_and_a_number_that_does_not_exist_is_ignored():
    (beats, _), _ = run([pic_beat(2, 0), pic_beat(4, 0), pic_beat(5, 9), pic_beat(6, -1)])
    assert [(b.kind, b.picture) for b in beats] == [("upload", 0)]


def test_leaving_a_picture_unused_is_fine_and_there_is_room_for_one_beat_per_picture():
    (beats, _), _ = run([pic_beat(2, 1)])
    assert [b.picture for b in beats] == [1]
    many = [Sentence(id=i, text=f"वाक्य {i}", start=100.0 + i * 4.0, end=100.0 + (i + 1) * 4.0, first=i * 5, last=i * 5 + 4) for i in range(14)]
    raw = [pic_beat(i, k) for k, i in enumerate((1, 3, 5, 7, 9))]
    beats, _ = plan_beats(FakeLLM(lambda p, u, s: BeatsResult(beats=raw)), many, 100.0, 56.0, [], allow_images=False, language="mr",
                          hook_end=2.8, purpose="beats:c1", pictures=[JPEG] * 5)
    assert len(beats) == 5                                             # more than the usual three: the person gave five


def test_nothing_is_asked_without_pictures_links_or_permission():
    silent = FakeLLM(lambda p, u, s: pytest.fail("the model must not be asked"))
    assert plan_beats(silent, sentences(), 100.0, 32.0, [], allow_images=False, language="mr", hook_end=2.8, purpose="x") == ([], [])


def test_a_thumbnail_is_a_small_jpeg(tmp_path):
    jpg = thumbnail(png(tmp_path / "big.png", "3000x2000"))
    assert jpg[:3] == b"\xff\xd8\xff"


# --- the whole plan and the render -----------------------------------------------------------------------------------

def model_with_pictures():
    base = scripted()

    def respond(purpose, prompt, schema):
        if purpose.startswith("beats:"):
            ids = [int(x) for x in re.findall(r"\[S(\d+) \|", prompt[-1]["text"] if isinstance(prompt, list) else prompt)]
            return BeatsResult(beats=[pic_beat(ids[1], 1, caption="पहिली बातमी")])
        return base.respond(purpose, prompt, schema)
    return FakeLLM(respond)


def opts_with(tmp_path, n=2):
    pics = []
    for i in range(n):
        p = tmp_path / "pics" / f"{i:012x}.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        png(p, "600x800")
        pics.append((f"{i:012x}", p))
    return PlannerOptions(target_clips=1, workers=1, explainer=True, pictures=tuple(pics)), pics


def test_the_plan_places_the_picture_the_model_chose_with_honest_provenance(tmp_path):
    opts, pics = opts_with(tmp_path)
    edl = plan_edl(make_transcript(), SRC, model_with_pictures(), opts).edl
    a = [x for x in edl.assets if x.origin == "upload" and x.ref]
    assert len(a) == 1 and a[0].ref == pics[1][0] and a[0].ai == "none" and a[0].tool == "user-picture" and not a[0].uri
    media = [o for o in edl.clips[0].overlays if o.kind == "media"]
    assert len(media) == 1 and media[0].asset == a[0].id and media[0].placement == "auto"
    from clipcrow.provenance import provenance
    assert provenance(edl, edl.clips[0])["disclosure_recommended"] is False                   # the person's own picture is not AI-generated


def test_make_copies_the_picture_into_the_output_folder_before_rendering(tmp_path, video_bytes):
    video = tmp_path / "talk.mp4"
    video.write_bytes(video_bytes)
    opts, pics = opts_with(tmp_path)
    seen = []

    def fake_render(edl, clip_id, path, **kw):
        seen.append([(a.uri, (kw["asset_root"] / a.uri).is_file()) for a in edl.assets if a.origin == "upload" and a.ref])
        Path(path).write_bytes(b"mp4")
        return path, None
    events = []
    res = make(video, tmp_path / "out", llm=model_with_pictures(), opts=opts, transcribe_fn=lambda m, language=None, progress=None: make_transcript(),
               render_fn=fake_render, hooks=Hooks(stage=lambda n, s, d="": events.append((n, s))))
    assert ("assets", "done") in events and seen and seen[0][0][1] is True and seen[0][0][0].startswith("assets/")
    assert (tmp_path / "out" / seen[0][0][0]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_a_picture_that_went_missing_is_dropped_with_a_note_and_never_fails_the_job(tmp_path):
    opts, pics = opts_with(tmp_path)
    edl = plan_edl(make_transcript(), SRC, model_with_pictures(), opts).edl
    new, warnings = fetch_assets(edl, tmp_path / "out", pictures={})
    assert warnings and "no longer available" in warnings[0] and not [o for o in new.clips[0].overlays if o.kind == "media"]
