import json
import subprocess
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("multipart")

from clipcrow.service.style_store import MAX_STYLES, StyleStore
from clipcrow.styles import Measurements, PunchRhythm, StyleError, StyleSettings, derive, measure
from tests.test_accounts import PW, app_client, signup
from tests.test_service import FakePipeline, client, video_bytes, wait, upload  # noqa: F401  (fixtures)

OWNER = "a" * 32


def cutting_video(path, shots=5, seconds=4, audio=True):
    """Solid colours that change every ``seconds``: ``shots - 1`` hard cuts."""
    colors = ["white", "black", "white", "black", "white", "black", "white", "black"][:shots]
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for c in colors:
        cmd += ["-f", "lavfi", "-i", f"color=c={c}:s=160x90:r=30:d={seconds}"]
    n = len(colors)
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=f=330:d={shots * seconds}"]
    cmd += ["-filter_complex", "".join(f"[{i}:v]" for i in range(n)) + f"concat=n={n}:v=1:a=0[v]", "-map", "[v]"]
    if audio:
        cmd += ["-map", f"{n}:a", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True)
    return path


@pytest.fixture(scope="module")
def ref_bytes(tmp_path_factory):
    return cutting_video(tmp_path_factory.mktemp("ref") / "ref.mp4").read_bytes()


class WordyPipeline(FakePipeline):
    """A FakePipeline whose 'speech recognition' counts a fixed number of words."""
    words = 60

    def reference_words(self, path):
        return self.words


# --- the mapping from measurements to settings -------------------------------------------------------------------------

def m(**kw):
    base = dict(duration=30.0, cuts=10, cuts_per_minute=20.0, median_shot_seconds=2.0, words_per_minute=150.0)

    return Measurements(**{**base, **kw})


def test_fast_edits_get_frequent_push_ins_and_calm_ones_few():
    fast, medium, calm = derive(m(cuts_per_minute=30)), derive(m(cuts_per_minute=10)), derive(m(cuts_per_minute=3))
    assert (fast.punch.min_gap, fast.punch.max_per_clip) == (1.0, 6) and fast.punch_ins
    assert (medium.punch.min_gap, medium.punch.max_per_clip) == (2.0, 4)
    assert calm.punch.min_gap > medium.punch.min_gap > fast.punch.min_gap and calm.punch.max_per_clip == 2
    assert derive(m(cuts_per_minute=0.5)).punch_ins is False                 # an almost uncut reference: no push-ins


def test_speech_rate_sets_the_caption_group_size_and_unknown_speech_keeps_the_default():
    assert derive(m(words_per_minute=200)).caption_max_words == 5
    assert derive(m(words_per_minute=150)).caption_max_words == 4
    assert derive(m(words_per_minute=100)).caption_max_words == 3
    assert derive(m(words_per_minute=None)).caption_max_words == 4


def test_clip_length_follows_the_reference_and_stays_in_range():
    s = derive(m(duration=30))
    assert (s.min_clip, s.max_clip) == (18, 38)
    short, long_ = derive(m(duration=6)), derive(m(duration=170))
    assert short.min_clip >= 10 and short.max_clip > short.min_clip
    assert long_.max_clip <= 180 and long_.min_clip < long_.max_clip


def test_derive_is_deterministic():
    assert derive(m()) == derive(m())


def test_settings_are_bounded_and_closed():
    with pytest.raises(Exception):
        StyleSettings(caption_max_words=9)
    with pytest.raises(Exception):
        StyleSettings(punch=PunchRhythm(min_gap=0.1))
    with pytest.raises(Exception):
        StyleSettings(unknown_field=1)
    with pytest.raises(StyleError):
        StyleSettings(min_clip=60, max_clip=30).check()


# --- measuring ---------------------------------------------------------------------------------------------------------

def test_measure_counts_cuts_and_speech_rate(tmp_path):
    p = cutting_video(tmp_path / "r.mp4")                                     # 20 s, 4 cuts
    r = measure(p, count_words=lambda path: 50)
    assert r.cuts == 4 and 11 <= r.cuts_per_minute <= 13 and r.duration == pytest.approx(20, abs=0.5)
    assert r.words_per_minute == pytest.approx(150, abs=5)                    # 50 words in 20 s
    assert 3.5 <= r.median_shot_seconds <= 4.5 and r.loudness_lufs is not None


def test_measure_survives_a_failing_recognizer_and_a_silent_reference(tmp_path):
    def boom(path):
        raise RuntimeError("no model")
    r = measure(cutting_video(tmp_path / "a.mp4"), count_words=boom)
    assert r.words_per_minute is None and r.cuts == 4 and r.notes
    s = measure(cutting_video(tmp_path / "s.mp4", audio=False), count_words=lambda p: 99)
    assert s.words_per_minute is None and s.loudness_lufs is None and s.cuts == 4


def test_measure_refuses_what_is_too_short_too_long_or_not_a_video(tmp_path):
    with pytest.raises(StyleError, match="at least"):
        measure(cutting_video(tmp_path / "t.mp4", shots=2, seconds=2))
    (tmp_path / "junk.mp4").write_bytes(b"not a video at all")
    with pytest.raises(StyleError, match="could not be read"):
        measure(tmp_path / "junk.mp4")
    with pytest.raises(StyleError, match="at most"):
        measure(cutting_video(tmp_path / "long.mp4", shots=4, seconds=50))


# --- the store ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    return StyleStore(tmp_path)


def test_owner_and_style_ids_cannot_escape_the_folder(store):
    for bad in ("../x", "a/b", "", "A" * 32, "x" * 31, "a" * 32 + "\n"):
        with pytest.raises(KeyError):
            store.list(bad)
    rec = store.create(OWNER, "Mine")
    for bad in ("../" + rec.id, rec.id + "\n", "", "Z" * 12):
        with pytest.raises(KeyError):
            store.get(OWNER, bad)


def test_one_analysis_at_a_time_and_a_cap(store):
    first = store.create(OWNER, "One")
    with pytest.raises(StyleError, match="still being measured"):
        store.create(OWNER, "Two")
    store.finish(OWNER, first.id, {}, StyleSettings())
    for i in range(MAX_STYLES - 1):
        store.finish(OWNER, store.create(OWNER, f"s{i}").id, {}, StyleSettings())
    with pytest.raises(StyleError, match="Delete one"):
        store.create(OWNER, "Eleventh")
    other = "b" * 32
    assert store.create(other, "Not capped by someone else's styles").status == "analyzing"


def test_update_validates_and_a_style_being_measured_cannot_be_edited(store):
    rec = store.create(OWNER, "  Fast   reel  ")
    assert rec.name == "Fast reel"
    with pytest.raises(StyleError):
        store.update(OWNER, rec.id, name="x")
    store.finish(OWNER, rec.id, {"cuts": 3}, StyleSettings())
    assert store.update(OWNER, rec.id, name="Renamed").name == "Renamed"
    with pytest.raises(StyleError):
        store.update(OWNER, rec.id, name="   ")
    with pytest.raises(StyleError):
        store.update(OWNER, rec.id, settings=StyleSettings.model_construct(min_clip=80, max_clip=40))


def test_stale_analyses_are_found_and_erasure_removes_everything(store):
    rec = store.create(OWNER, "Left behind")
    assert [(o, r.id) for o, r in store.stale_analyzing()] == [(OWNER, rec.id)]
    store.fail(OWNER, rec.id, "restarted")
    assert store.get(OWNER, rec.id).status == "failed" and not store.stale_analyzing()
    store.delete_all(OWNER)
    assert store.list(OWNER) == []
    store.fail(OWNER, rec.id, "already gone")                                 # deleted while measuring: no error


# --- the service -------------------------------------------------------------------------------------------------------

def make_style(c, ref, name="Fast reel", **form):
    r = c.post("/api/styles", data={"name": name, "rights": "true", **form}, files={"file": ("ref.mp4", ref, "video/mp4")})
    assert r.status_code == 202, r.text
    return r.json()


def wait_style(c, style_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        s = c.get(f"/api/styles/{style_id}").json()
        if s["status"] != "analyzing":
            return s
        time.sleep(0.1)
    raise AssertionError("still measuring")


def test_a_reference_is_measured_saved_and_not_kept(tmp_path, ref_bytes):
    c, _ = client(tmp_path, WordyPipeline())
    made = make_style(c, ref_bytes)
    assert made["status"] == "analyzing" and made["name"] == "Fast reel"
    s = wait_style(c, made["id"])
    assert s["status"] == "ready", s
    assert s["measured"]["cuts"] == 4 and s["settings"]["punch"]["min_gap"] == 2.0 and s["settings"]["caption_max_words"] == 5
    assert [x["id"] for x in c.get("/api/styles").json()] == [made["id"]]
    kept = [p.name for p in (tmp_path / "data").rglob("*") if p.is_file() and p.suffix in (".mp4", ".mov", ".mkv", ".webm", ".m4v")]
    assert kept == []                                                         # the reference video is deleted after measuring
    assert not [p for p in (tmp_path / "data").iterdir() if p.name.startswith("tmp")]


def test_upload_checks(tmp_path, ref_bytes):
    c, _ = client(tmp_path, WordyPipeline())
    f = {"file": ("ref.mp4", ref_bytes, "video/mp4")}
    assert c.post("/api/styles", data={"name": "x"}, files=f).status_code == 400            # rights not confirmed
    assert c.post("/api/styles", data={"rights": "true"}, files={"file": ("ref.exe", b"x", "x/y")}).status_code == 400
    bad = make_style(c, b"definitely not video", name="Broken")
    s = wait_style(c, bad["id"])
    assert s["status"] == "failed" and "could not be read" in s["error"]
    assert c.post("/api/styles", data={"rights": "true"}, files=f).status_code == 202       # a failed one does not block the next
    assert c.get("/api/styles/zzzzzzzzzzzz").status_code == 404


def test_edit_and_delete(tmp_path, ref_bytes):
    c, _ = client(tmp_path, WordyPipeline())
    sid = make_style(c, ref_bytes)["id"]
    wait_style(c, sid)
    r = c.put(f"/api/styles/{sid}", json={"name": "Calmer", "settings": {"caption_max_words": 3}})
    assert r.status_code == 200 and r.json()["name"] == "Calmer" and r.json()["settings"]["caption_max_words"] == 3
    assert c.put(f"/api/styles/{sid}", json={"settings": {"caption_max_words": 99}}).status_code == 422
    assert c.put(f"/api/styles/{sid}", json={"settings": {"min_clip": 90, "max_clip": 30}}).status_code == 422
    assert c.put(f"/api/styles/{sid}", json={"settings": {"surprise": 1}}).status_code == 422
    assert c.delete(f"/api/styles/{sid}").status_code == 204
    assert c.get("/api/styles").json() == [] and c.delete(f"/api/styles/{sid}").status_code == 404


def edl_of(tmp_path, job_id):
    return json.loads((tmp_path / "data" / "jobs" / job_id / "out" / "edl.json").read_text("utf-8"))


def test_a_job_uses_the_style_and_keeps_its_own_copy(tmp_path, ref_bytes, video_bytes):
    c, _ = client(tmp_path, WordyPipeline())
    sid = make_style(c, ref_bytes)["id"]
    s = wait_style(c, sid)
    r = c.post("/api/jobs", data={"rights": "true", "style": sid, "clips": "2"}, files={"file": ("ep.mp4", video_bytes, "video/mp4")})
    assert r.status_code == 201, r.text
    job_id = r.json()["id"]
    assert wait(c, job_id)["status"] == "done"
    job = json.loads((tmp_path / "data" / "jobs" / job_id / "job.json").read_text("utf-8"))
    assert job["params"]["style_name"] == "Fast reel" and job["params"]["style"]["caption_max_words"] == 5
    assert (job["params"]["min_clip"], job["params"]["max_clip"]) == (s["settings"]["min_clip"], s["settings"]["max_clip"])
    caps = [cl["captions"] for cl in edl_of(tmp_path, job_id)["clips"]]
    assert caps and all(x["max_words"] == 5 and x["pause_break"] == pytest.approx(0.15) for x in caps)
    c.put(f"/api/styles/{sid}", json={"settings": {"caption_max_words": 2}})            # editing later changes nothing already made
    c.delete(f"/api/styles/{sid}")
    job = json.loads((tmp_path / "data" / "jobs" / job_id / "job.json").read_text("utf-8"))
    assert job["params"]["style"]["caption_max_words"] == 5


def test_an_explicit_clip_length_beats_the_style_and_a_missing_style_is_refused(tmp_path, ref_bytes, video_bytes):
    c, _ = client(tmp_path, WordyPipeline())
    sid = make_style(c, ref_bytes)["id"]
    wait_style(c, sid)
    r = c.post("/api/jobs", data={"rights": "true", "style": sid, "min_clip": "20", "max_clip": "50"},
               files={"file": ("ep.mp4", video_bytes, "video/mp4")})
    job = json.loads((tmp_path / "data" / "jobs" / r.json()["id"] / "job.json").read_text("utf-8"))
    assert (job["params"]["min_clip"], job["params"]["max_clip"]) == (20, 50) and job["params"]["style"] is not None
    nope = c.post("/api/jobs", data={"rights": "true", "style": "0" * 12}, files={"file": ("ep.mp4", video_bytes, "video/mp4")})
    assert nope.status_code == 422 and "does not exist" in nope.json()["detail"]


def test_a_style_still_being_measured_cannot_be_used(tmp_path, video_bytes):
    c, app = client(tmp_path, WordyPipeline())
    store = StyleStore(tmp_path / "data")
    rec = store.create("shared", "Pending")
    r = c.post("/api/jobs", data={"rights": "true", "style": rec.id}, files={"file": ("ep.mp4", video_bytes, "video/mp4")})
    assert r.status_code == 422 and "not ready" in r.json()["detail"]


def test_styles_belong_to_their_account(tmp_path, ref_bytes, video_bytes):
    c, accounts, app = app_client(tmp_path, WordyPipeline())
    signup(c, "a@example.com")
    sid = make_style(c, ref_bytes)["id"]
    wait_style(c, sid)
    c.post("/api/auth/logout")
    signup(c, "b@example.com")
    assert c.get("/api/styles").json() == []
    assert c.get(f"/api/styles/{sid}").status_code == 404
    assert c.put(f"/api/styles/{sid}", json={"name": "mine now"}).status_code == 404
    assert c.delete(f"/api/styles/{sid}").status_code == 404
    r = c.post("/api/jobs", data={"rights": "true", "style": sid}, files={"file": ("ep.mp4", video_bytes, "video/mp4")})
    assert r.status_code == 422                                              # another account's style is simply not there


def test_erasing_an_account_removes_its_styles(tmp_path, ref_bytes):
    c, accounts, app = app_client(tmp_path, WordyPipeline())
    signup(c, "a@example.com")
    sid = make_style(c, ref_bytes)["id"]
    wait_style(c, sid)
    assert list((tmp_path / "data" / "styles").glob("*/*.json"))
    r = c.request("DELETE", "/api/account", json={"password": PW})
    assert r.status_code == 200, r.text
    assert not list((tmp_path / "data" / "styles").glob("*/*.json"))


def test_restart_marks_an_interrupted_measuring_as_failed(tmp_path):
    StyleStore(tmp_path / "data").create("shared", "Interrupted")
    c, _ = client(tmp_path, WordyPipeline())
    (s,) = c.get("/api/styles").json()
    assert s["status"] == "failed" and "restarted" in s["error"]


# --- phase 2: the caption look, read from frames ------------------------------------------------------------------------

from clipcrow.planner.llm import FakeLLM, LLMError                                   # noqa: E402
from clipcrow.styles_look import BANDS, CaptionLook, family_for, grab_frames, read_look, size_scale  # noqa: E402

LOOK = {"present": True, "band": "middle", "size": 7.5, "words_per_line": 3, "color": "#00FF00",
        "highlight_color": "#FF00FF", "uppercase": True, "face": "heavy sans", "note": "one word pops per beat"}


def look_llm(answer=None, calls=None):
    def respond(purpose, user, schema):
        if calls is not None:
            calls.append((purpose, user))
        if isinstance(answer, Exception):
            raise answer
        return schema.model_validate(answer if answer is not None else LOOK)
    return FakeLLM(respond)


def test_the_model_may_only_say_bounded_things_and_bad_colours_are_dropped():
    with pytest.raises(Exception):
        CaptionLook(band="diagonal")
    with pytest.raises(Exception):
        CaptionLook(size=40)
    with pytest.raises(Exception):
        CaptionLook(face="comic sans")
    with pytest.raises(Exception):
        CaptionLook(trust_me="rm -rf")
    c = CaptionLook(present=True, color="yellow; }</style><script>x()</script>", highlight_color="#ABCDEF").clean()
    assert c.color is None and c.highlight_color == "#ABCDEF"
    with pytest.raises(Exception):
        CaptionLook(note="x" * 201)                                      # the note is bounded before clean() ever runs
    assert CaptionLook(note="a  b\n\nc").clean().note == "a b c"


def test_a_described_face_maps_to_a_font_we_ship_and_devanagari_keeps_its_own():
    assert family_for("heavy sans", False) == ("Noto Sans", 700)
    assert family_for("serif", False) == ("Noto Serif", 700)
    assert family_for("heavy sans", True) == ("Noto Sans Devanagari", 700)
    assert family_for("serif", True)[0] == "Noto Serif Devanagari"
    assert family_for("whatever the model said", False)[0] == "Noto Serif Devanagari"


def test_size_is_a_clamped_multiplier():
    assert size_scale(5.0) == 1.0 and size_scale(None) == 1.0
    assert size_scale(100) == 1.6 and size_scale(0.5) == 0.6 and size_scale(7.5) == 1.5


def test_reading_the_look_sends_frames_and_returns_a_clean_answer(tmp_path):
    p = cutting_video(tmp_path / "r.mp4")
    calls = []
    got = read_look(p, 20.0, look_llm(calls=calls))
    assert got.present and got.band == "middle" and got.color == "#00FF00" and got.uppercase
    (purpose, content), = calls
    assert purpose == "style:captions"
    assert sum(1 for b in content if b["type"] == "image") >= 4 and content[-1]["type"] == "text"
    assert all(b["source"]["media_type"] == "image/jpeg" for b in content if b["type"] == "image")


def test_grab_frames_reads_the_middle_of_the_video(tmp_path):
    frames = grab_frames(cutting_video(tmp_path / "r.mp4"), 20.0, n=4)
    assert len(frames) == 4 and all(f[:2] == b"\xff\xd8" for f in frames)       # JPEGs
    assert len(set(frames)) > 1                                                 # different moments, different pictures


def test_the_look_becomes_bounded_settings_and_the_words_seen_beat_the_speech_rate():
    s = derive(m(words_per_minute=200, look=LOOK))
    assert s.captions.from_reference and s.captions.top_px == BANDS["middle"]
    assert s.captions.size_scale == 1.5 and s.captions.uppercase and s.captions.color == "#00FF00"
    assert s.caption_max_words == 3                                             # 3 words seen, not 5 from fast speech
    plain = derive(m(words_per_minute=200, look={"present": False}))
    assert not plain.captions.from_reference and plain.captions.color == "#FFFFFF" and plain.caption_max_words == 5
    half = derive(m(look={"present": True, "band": "nonsense", "color": None}))
    assert half.captions.top_px == BANDS["lower"] and half.captions.color == "#FFFFFF"


def test_measuring_survives_a_model_that_will_not_answer(tmp_path):
    def boom(path, duration):
        raise LLMError("declined")
    r = measure(cutting_video(tmp_path / "r.mp4"), read_look=boom)
    assert r.look is None and r.cuts == 4 and any("caption look" in n for n in r.notes)
    assert not derive(r).captions.from_reference
    none = measure(cutting_video(tmp_path / "n.mp4"), read_look=lambda p, d: {"present": False})
    assert any("No captions were visible" in n for n in none.notes)


def test_the_breakdown_is_plain_readable_text(tmp_path):
    r = measure(cutting_video(tmp_path / "r.mp4"), count_words=lambda p: 60, read_look=lambda p, d: LOOK)
    text = r.breakdown
    assert "cuts" in text and "words a minute" in text and "Captions sit in the middle" in text
    assert "one word pops per beat" in text and "<" not in text
    assert r.view()["breakdown"] == text


class LookPipeline(WordyPipeline):
    look = LOOK

    def reference_look(self, path, duration):
        return read_look(path, duration, look_llm(self.look)).model_dump()


def test_the_caption_look_reaches_the_edl(tmp_path, ref_bytes, video_bytes):
    c, _ = client(tmp_path, LookPipeline())
    sid = make_style(c, ref_bytes)["id"]
    s = wait_style(c, sid)
    assert s["settings"]["captions"]["from_reference"] and s["measured"]["look"]["band"] == "middle"
    assert "Captions sit in the middle" in s["measured"]["breakdown"]
    r = c.post("/api/jobs", data={"rights": "true", "style": sid, "clips": "2"}, files={"file": ("ep.mp4", video_bytes, "video/mp4")})
    assert wait(c, r.json()["id"])["status"] == "done"
    caps = [cl["captions"] for cl in edl_of(tmp_path, r.json()["id"])["clips"]]
    assert caps and all(x["top_px"] == BANDS["middle"] and x["size_scale"] == 1.5 and x["color"] == "#00FF00"
                        and x["highlight_color"] == "#FF00FF" for x in caps)


def test_devanagari_keeps_its_font_and_never_goes_uppercase(tmp_path, ref_bytes, video_bytes):
    c, _ = client(tmp_path, LookPipeline())           # the fake transcript is Marathi (tests/test_service.py)
    sid = make_style(c, ref_bytes)["id"]
    wait_style(c, sid)
    r = c.post("/api/jobs", data={"rights": "true", "style": sid, "clips": "2"}, files={"file": ("ep.mp4", video_bytes, "video/mp4")})
    assert wait(c, r.json()["id"])["status"] == "done"
    for cl in edl_of(tmp_path, r.json()["id"])["clips"]:
        assert cl["captions"]["font_family"] in ("Noto Sans Devanagari", "Noto Serif Devanagari")
        assert cl["captions"]["uppercase"] is False


# --- speech share: how much of the reference has sound (cheap, deterministic, no speech recognition) -----------------------

def half_silent_video(path):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=white:s=160x90:r=30:d=10",
                    "-f", "lavfi", "-i", "sine=f=330:d=5", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                    "-filter_complex", "[1:a]atrim=0:5,asetpts=N/SR/TB[a1];[2:a]atrim=0:5,asetpts=N/SR/TB[a2];[a1][a2]concat=n=2:v=0:a=1[a]",
                    "-map", "0:v", "-map", "[a]", "-t", "10", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    str(path)], check=True)
    return path


def test_speech_share_counts_sound_against_silence(tmp_path):
    from clipcrow.styles import speech_share
    assert 0.4 <= speech_share(half_silent_video(tmp_path / "h.mp4"), 10.0) <= 0.6
    assert speech_share(cutting_video(tmp_path / "t.mp4"), 20.0) >= 0.95            # a steady tone is all sound


def test_measure_reports_speech_share_without_any_speech_recognition(tmp_path):
    r = measure(half_silent_video(tmp_path / "h.mp4"))
    assert r.words_per_minute is None and r.speech_share is not None and r.speech_share < 0.7
    assert "Sound for about" in r.breakdown and r.view()["speech_share"] == round(r.speech_share, 2)
    assert derive(r).caption_max_words == 4                                          # no rate, no look: the default


# --- a look must be seen often enough and stay readable --------------------------------------------------------------------

from clipcrow.styles import luminance, look_in_use  # noqa: E402


def test_luminance_orders_colours_the_way_the_eye_does():
    assert luminance("#000000") == 0 and luminance("#FFFFFF") == pytest.approx(1)
    assert luminance("#FF00FF") > 0.175 > luminance("#7A0000") and luminance("#0000FF") < 0.175      # magenta reads, deep blue does not


def test_a_caption_seen_in_one_frame_is_not_the_references_style():
    one = {**LOOK, "frames_with_captions": 1}
    assert not look_in_use(one) and look_in_use({**LOOK, "frames_with_captions": 4}) and look_in_use(LOOK)   # no count: trusted
    s = derive(m(look=one))
    assert not s.captions.from_reference and s.captions.top_px == BANDS["lower"] and s.captions.color == "#FFFFFF"


def test_dark_caption_text_is_replaced_with_readable_text_but_position_and_size_are_kept():
    boxed = {**LOOK, "frames_with_captions": 5, "color": "#000000", "highlight_color": "#101010", "band": "upper", "size": 7.0}
    c = derive(m(look=boxed)).captions
    assert c.from_reference and c.color == "#FFFFFF" and c.highlight_color == "#FFD54A"
    assert c.top_px == BANDS["upper"] and c.size_scale == 1.4 and c.uppercase


def test_the_notes_say_why_a_look_was_not_used(tmp_path):
    p = cutting_video(tmp_path / "r.mp4")
    few = measure(p, read_look=lambda path, d: {**LOOK, "frames_with_captions": 1})
    assert any("only 1 of the frames" in n for n in few.notes) and "Captions sit" not in few.breakdown
    dark = measure(p, read_look=lambda path, d: {**LOOK, "frames_with_captions": 5, "color": "#000000"})
    assert any("dark caption text" in n for n in dark.notes)
