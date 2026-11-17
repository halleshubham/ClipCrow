"""Who speaks, who sits where, and a picture that follows the voice (no model downloads: the embedder and the face detector are small fakes)."""
import json
import shutil
import subprocess

import numpy as np
import pytest

from clipcrow import reframe, seats, voices
from clipcrow.edl import Word
from clipcrow.reframe import Box
from tests.test_service import video_bytes  # noqa: F401  (the pytest fixture)

ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
SR = 16000


class ToneEmbedder:
    """A voice here is a pitch: 200 Hz -> voice A, 400 Hz -> voice B (unit vectors, a little noise)."""

    def embed(self, chunks):
        out = []
        rng = np.random.default_rng(1)
        for c in chunks:
            c = np.asarray(c)
            c = c[np.abs(c) > 0.05]                                          # the voiced part only
            zc = np.mean(np.abs(np.diff(np.sign(c)))) / 2 * SR / 2 if len(c) > 2 else 0   # zero crossings -> frequency
            v = np.array([1.0, 0.0, 0.0]) if zc < 300 else np.array([0.0, 1.0, 0.0])
            v = v + rng.normal(0, 0.03, 3)
            out.append(v / np.linalg.norm(v))
        return np.array(out)


def tone(freq, seconds):
    t = np.arange(int(seconds * SR)) / SR
    return (0.4 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def talk_audio(plan):
    """plan: [(freq or None, seconds)] -> audio with a short pause between turns."""
    parts = []
    for f, s in plan:
        parts.append(tone(f, s) if f else np.zeros(int(s * SR), np.float32))
    return np.concatenate(parts)


# --- who speaks when -----------------------------------------------------------------------------

def test_clustering_finds_the_voices_and_a_stray_window_joins_the_closest():
    rng = np.random.default_rng(0)
    base = np.eye(4)[:3]
    emb = np.vstack([base[i % 3] + rng.normal(0, 0.05, 4) for i in range(30)])
    emb /= np.linalg.norm(emb, axis=1, keepdims=True)
    labels = voices.cluster(emb)
    assert len(set(labels)) == 3 and all(labels[i] == labels[i % 3] for i in range(3, 30))
    assert voices.cluster(np.zeros((0, 3))) == []


def test_diarize_gives_a_turn_per_stretch_of_one_voice():
    audio = talk_audio([(200, 9), (None, 2), (400, 9), (None, 2), (200, 9)])        # real speech has pauses: the loudness floor is read from them
    turns = voices.diarize(audio, ToneEmbedder())
    assert len(turns) == 3 and turns[0].voice == turns[2].voice != turns[1].voice
    first, second = turns[0], turns[1]
    assert 8.0 <= first.end <= 11.5 and 10.5 <= second.start <= 12.5
    assert voices.diarize(talk_audio([(200, 2)]), ToneEmbedder()) == []                 # too little to tell voices apart


# --- voice -> seat -------------------------------------------------------------------------------

def rows_for(voice_seat, n=24, lift=3.0):
    """Mouth activity rows: seat s is busy only while 'its' voice is heard."""
    rows = []
    rng = np.random.default_rng(2)
    for v, s_true in voice_seat.items():
        for s in range(2):
            for _ in range(n):
                busy = s == s_true
                rows.append((v, s, float((lift if busy else 1.0) * (1 + rng.normal(0, 0.1)))))
    return rows


def test_a_voice_belongs_to_the_seat_whose_mouth_is_busier_while_it_is_heard():
    rows = rows_for({0: 1, 1: 0})
    seen = {0: (40, 48), 1: (45, 48)}
    mapping, lift = seats.assign(rows, seen, [0, 1], 2)
    assert mapping == {0: 1, 1: 0} and lift[0] > 1.5


def test_a_voice_with_no_busy_seat_is_off_screen_and_a_seat_out_of_view_is_never_chosen():
    rows = rows_for({0: 0}, lift=3.0) + [(1, s, 1.0 + 0.01 * k) for s in range(2) for k in range(24)]
    mapping, _ = seats.assign(rows, {0: (40, 48), 1: (40, 48)}, [0, 1], 2)
    assert mapping[0] == 0 and mapping[1] is None                                         # voice 1 moves nobody's mouth: the interviewer
    mapping, _ = seats.assign(rows_for({0: 1, 1: 0}), {0: (5, 48), 1: (45, 48)}, [0, 1], 2)
    assert mapping == {0: 1, 1: None}                                                      # seat 0 was out of view in 90% of those frames: nobody is placed there


def test_a_single_voice_is_not_placed_there_is_nothing_to_compare_it_with():
    rows = [(0, 0, 3.0)] * 12 + [(0, 1, 1.0)] * 12
    assert seats.assign(rows, {0: (20, 24), 1: (20, 24)}, [0], 2)[0] == {0: None}


# --- the picture follows the voice ------------------------------------------------------------------

def fake_map(voice_seat, turns, overrides=None):
    s = [seats.Seat(0.15, 0.4, 0.05, 0.09, 1.0), seats.Seat(0.40, 0.4, 0.05, 0.09, 1.0), seats.Seat(0.62, 0.4, 0.05, 0.09, 1.0), seats.Seat(0.85, 0.4, 0.05, 0.09, 1.0)]
    return seats.SpeakerMap(s, [voices.Turn(a, b, v) for a, b, v in turns], voice_seat, {}, 60.0, overrides or {})


CROP = 405 / 1280


def test_the_camera_cuts_to_the_seat_of_whoever_is_heard():
    sm = fake_map({0: 0, 1: 3}, [(0, 10, 0), (10, 20, 1), (20, 30, 0)])
    f = reframe.plan_voices(sm, 0.0, 30.0, CROP, "switch")
    assert f.framing == "track" and [round(t) for t, _ in f.keyframes] == [0, 10, 20]
    assert [cx for _, cx in f.keyframes] == pytest.approx([CROP / 2, 1 - CROP / 2, CROP / 2], abs=1e-3) and f.people == 2       # the seats at the edges: the window stays inside the frame


def test_a_short_laugh_does_not_move_the_picture_and_a_clip_starts_on_the_first_speaker():
    sm = fake_map({0: 0, 1: 3}, [(0, 8, 0), (8, 8.6, 1), (8.6, 20, 0)])
    f = reframe.plan_voices(sm, 2.0, 20.0, CROP, "switch")
    assert len(f.keyframes) == 1 and f.keyframes[0][0] == 0.0                                 # 0.6 s is a laugh, not a turn


def test_an_off_screen_voice_keeps_the_last_speaker_briefly_and_shows_the_whole_frame_when_long():
    sm = fake_map({0: 0, 1: None}, [(0, 10, 0), (10, 12, 1), (12, 20, 0), (20, 28, 1), (28, 40, 0)])
    f = reframe.plan_voices(sm, 0.0, 40.0, CROP, "switch")
    assert f.fit_spans == [(20.0, 28.0)] and all(a != 10.0 for a, _ in f.fit_spans)          # 2 s: stay on speaker 0; 8 s: whole frame


def test_two_people_sitting_together_are_one_picture():
    sm = fake_map({0: 1, 1: 2}, [(0, 10, 0), (10, 20, 1)])
    sm.seats[1], sm.seats[2] = seats.Seat(0.45, 0.4, 0.05, 0.09, 1.0), seats.Seat(0.52, 0.4, 0.05, 0.09, 1.0)
    f = reframe.plan_voices(sm, 0.0, 20.0, CROP, "switch")
    assert len(f.keyframes) == 1                                                              # both fit one 9:16 window: no cut between them


def test_split_stacks_the_two_most_heard_people_and_a_persons_correction_wins():
    sm = fake_map({0: 0, 1: 3}, [(0, 10, 0), (10, 25, 1)])
    f = reframe.plan_voices(sm, 0.0, 25.0, CROP, "split")
    assert f.framing == "split" and f.split[0][0] < f.split[1][0]
    sm = fake_map({0: 0, 1: 3}, [(0, 10, 0), (10, 20, 1)], overrides={1: 1})
    f = reframe.plan_voices(sm, 0.0, 20.0, CROP, "switch")
    assert f.keyframes[-1][1] == pytest.approx(0.51, abs=0.01) and f.keyframes[0][1] == pytest.approx(CROP / 2, abs=1e-3)       # seats 1 and 2 share a window
    assert reframe.plan_voices(fake_map({0: None}, [(0, 10, 0)]), 0.0, 10.0, CROP, "switch") is None      # nobody to follow: the old path decides


def test_the_map_survives_a_round_trip_through_json():
    sm = fake_map({0: 0, 1: None}, [(0, 5, 0), (5, 9, 1)], overrides={1: 2})
    again = seats.SpeakerMap.from_json(json.loads(json.dumps(sm.to_json())))
    assert again.voice_seat == {0: 0, 1: None} and again.overrides == {1: 2} and again.seat_of(1) == 2 and [t.voice for t in again.turns] == [0, 1]


# --- the whole thing, on a made-up video -------------------------------------------------------------

class FixedFaces:
    """Two faces at fixed places in a 640x360 frame (the picture is made so), like a camera that stays put."""

    def detect(self, frame):
        h, w = frame.shape[:2]
        return [Box(0.25 * w - 30, 0.4 * h - 30, 60, 60), Box(0.75 * w - 30, 0.4 * h - 30, 60, 60)]


def make_video(path, audio, plan):
    """640x360, 10 fps. Two grey faces; the lower half of the face of whoever is speaking flickers (a mouth moving)."""
    fps, w, h = 10, 640, 360
    rng = np.random.default_rng(3)
    n = int(len(audio) / SR * fps)
    wav = path.with_suffix(".wav")
    import wave
    with wave.open(str(wav), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes((audio * 32767).astype(np.int16).tobytes())
    proc = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-", "-i", str(wav),
                             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], stdin=subprocess.PIPE)
    for k in range(n):
        t = k / fps
        frame = np.full((h, w, 3), 90, np.uint8)
        for seat_i, cx in enumerate((0.25, 0.75)):
            x0, y0 = int(cx * w) - 30, int(0.4 * h) - 30
            frame[y0:y0 + 60, x0:x0 + 60] = 170
            speaking = any(a <= t < b and s == seat_i for a, b, s in plan)
            if speaking:
                frame[y0 + 30:y0 + 60, x0:x0 + 60] = rng.integers(60, 230, (30, 60, 1), dtype=np.uint8)
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    proc.wait()


@ffmpeg
def test_build_speaker_map_matches_each_voice_to_the_face_that_moves_with_it(tmp_path):
    plan = [(0, 10, 0), (12, 22, 1), (24, 34, 0)]                                           # seat 0 talks, then seat 1, then seat 0
    audio = talk_audio([(200, 10), (None, 2), (400, 10), (None, 2), (200, 10)])
    video = tmp_path / "talk.mp4"
    make_video(video, audio, plan)
    said = []
    sm = seats.build_speaker_map(video, tmp_path / "out", detector=FixedFaces(), embedder=ToneEmbedder(), say=said.append)
    assert sm is not None and len(sm.seats) == 2 and sm.usable
    first = sm.turns[0].voice
    other = sm.turns[1].voice
    assert sm.voice_seat[first] == 0 and sm.voice_seat[other] == 1
    assert any("who is who" in m for m in said) and (tmp_path / "out" / "speakers.json").is_file() and (tmp_path / "out" / "speakers.jpg").is_file()
    again = seats.build_speaker_map(video, tmp_path / "out", detector=None, embedder=None)           # cached: no detector or model needed
    assert again is not None and again.voice_seat == sm.voice_seat
    f = reframe.plan_framing(str(video), 0.0, 34.0, multi="switch", speakers=sm)
    assert f.framing == "track" and f.people == 2 and len(f.keyframes) == 3 and f.keyframes[0][1] < 0.5 < f.keyframes[1][1]


@ffmpeg
def test_a_video_with_one_person_costs_no_voice_work(tmp_path):
    class OneFace:
        def detect(self, frame):
            h, w = frame.shape[:2]
            return [Box(0.5 * w - 30, 0.4 * h - 30, 60, 60)]

    class Boom:
        def embed(self, chunks):
            raise AssertionError("no voice work for one person")
    video = tmp_path / "solo.mp4"
    make_video(video, talk_audio([(200, 10), (None, 3), (200, 10), (None, 3)]), [(0, 26, 0)])
    assert seats.build_speaker_map(video, tmp_path / "o", detector=OneFace(), embedder=Boom()) is None
    assert seats.build_speaker_map(tmp_path / "missing.mp4", tmp_path / "o2") is None


def test_no_embedder_available_means_the_picture_alone_decides(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPCROW_SPEAKERS", "0")
    assert voices.default_embedder() is None


# --- the clip editor, the routes and the runner ------------------------------------------------------------

def test_a_clip_can_stay_on_one_person_or_go_back_to_following_the_voices(make_edl):
    from clipcrow.service.editor import ClipEdit, apply_edit
    edl = make_edl(clips=[{"id": "c1", "source_in": 0, "source_out": 10, "title": "t"}])
    on, replan = apply_edit(edl, "c1", ClipEdit(follow=1), source_duration=600, seat_cx=[0.2, 0.7, 0.9])
    lay = on.clips[0].layout
    assert replan is None and lay.framing == "track" and [(k.t, k.cx) for k in lay.reframe] == [(0.0, 0.7)]
    back, replan = apply_edit(on, "c1", ClipEdit(follow=-1), source_duration=600, seat_cx=[0.2, 0.7, 0.9])
    assert replan == "switch" and back.clips[0].layout.reframe == []
    with pytest.raises(ValueError):
        apply_edit(edl, "c1", ClipEdit(follow=5), source_duration=600, seat_cx=[0.2, 0.7])
    with pytest.raises(ValueError):
        apply_edit(edl, "c1", ClipEdit(follow=0), source_duration=600)                       # a video with no seats


def test_the_speaker_routes_show_who_is_who_and_a_correction_frames_the_clips_again(tmp_path, video_bytes):
    from tests.test_service import FakePipeline, client, upload, wait
    c, app = client(tmp_path, FakePipeline())
    job = wait(c, upload(c, video_bytes).json()["id"])
    jid = job["id"]
    assert c.get(f"/api/jobs/{jid}/speakers").json()["available"] is False                    # one person (or not made with voices): nothing to choose yet
    out = tmp_path / "data" / "jobs" / jid / "out"
    out.mkdir(parents=True, exist_ok=True)
    sm = fake_map({0: 0, 1: None}, [(0.0, 1.5, 0), (1.5, 3.0, 1)])
    sm.seats = sm.seats[:2]
    seats.save_map(out, sm)
    (out / "speakers.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    body = c.get(f"/api/jobs/{jid}/speakers").json()
    assert [s["n"] for s in body["seats"]] == [1, 2] and body["voices"][0]["seat"] == 1 and body["voices"][1]["seat"] is None and body["picture"] is True
    assert c.get(f"/api/jobs/{jid}/speakers/picture").status_code == 200
    assert c.get(f"/api/jobs/{jid}/speakers/9/sample").status_code == 404
    assert c.put(f"/api/jobs/{jid}/speakers", json={"voices": {"7": 1}}).status_code == 409           # no such voice
    assert c.put(f"/api/jobs/{jid}/speakers", json={"voices": {"1": 9}}).status_code == 409           # no such person
    r = c.put(f"/api/jobs/{jid}/speakers", json={"voices": {"1": 2}})                                  # voice 2 is person 2
    assert r.status_code == 200
    again = seats.load_map(out)
    assert again.seat_of(1) == 1 and again.seat_of(0) == 0
    assert c.get(f"/api/jobs/{jid}/speakers").json()["voices"][1]["changed"] is True
    assert c.put(f"/api/jobs/{jid}/speakers", json={"voices": {"1": 2}}).json()["clips"] == []       # nothing changed
    assert c.get("/api/jobs/zzzzzzzzzzzz/speakers").status_code == 404


def test_a_finished_job_can_find_its_people_later_and_its_clips_follow_the_voices(tmp_path, video_bytes, monkeypatch):
    from clipcrow import seats as seats_mod
    from tests.test_service import FakePipeline, client, upload, wait
    c, app = client(tmp_path, FakePipeline())
    job = wait(c, upload(c, video_bytes).json()["id"])
    jid = job["id"]
    body = c.get(f"/api/jobs/{jid}/speakers").json()
    assert body["available"] is False and "can_build" in body and body["building"] is False

    def fake_build(video, out, **kw):
        out = tmp_path / "data" / "jobs" / jid / "out"
        sm = fake_map({0: 0, 1: 1}, [(0.0, 1.5, 0), (1.5, 3.0, 1)])
        sm.seats = sm.seats[:2]
        seats_mod.save_map(out, sm)
        kw["say"]("who is who: voice 1 = person 1")
        return sm
    monkeypatch.setattr(seats_mod, "build_speaker_map", fake_build)
    r = c.post(f"/api/jobs/{jid}/speakers/build")
    assert r.status_code == 200 and r.json()["started"] is True
    import time
    out = tmp_path / "data" / "jobs" / jid / "out"
    for _ in range(100):
        if (out / "speakers.json").is_file() and not (out / "speakers.building").exists():
            break
        time.sleep(0.1)
    assert seats_mod.load_map(out) is not None and not (out / "speakers.building").exists()
    assert c.get(f"/api/jobs/{jid}/speakers").json()["available"] is True
    assert any("who is who" in line for line in c.get(f"/api/jobs/{jid}").json()["log"])
    assert c.post("/api/jobs/zzzzzzzzzzzz/speakers/build").status_code == 404


def test_one_head_found_twice_is_one_seat():
    s = [seats.Seat(0.1162, 0.4418, 0.07, 0.12, 0.6), seats.Seat(0.14, 0.4202, 0.07, 0.12, 0.2), seats.Seat(0.5806, 0.4432, 0.07, 0.12, 0.9),
         seats.Seat(0.7867, 0.4498, 0.07, 0.12, 0.5), seats.Seat(0.808, 0.4422, 0.07, 0.12, 0.3), seats.Seat(0.3828, 0.4479, 0.07, 0.12, 0.7)]
    merged = seats.merge_seats(s)
    assert [round(x.cx, 2) for x in merged] == [0.12, 0.38, 0.58, 0.79] and merged[0].seen == pytest.approx(0.8)
    near = seats.merge_seats([seats.Seat(0.40, 0.4, 0.07, 0.12, 1.0), seats.Seat(0.50, 0.4, 0.07, 0.12, 1.0)])
    assert len(near) == 2                                                          # two people side by side are two


def test_download_all_reels_is_one_zip_of_the_ready_clips_only(tmp_path, video_bytes):
    import zipfile
    from tests.test_service import FakePipeline, client, upload, wait
    c, app = client(tmp_path, FakePipeline())
    job = wait(c, upload(c, video_bytes, name="my talk.mp4").json()["id"])
    ready = [x["id"] for x in job["clips"] if x["ready"]]
    assert ready
    r = c.get(f"/api/jobs/{job['id']}/clips.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip" and "my_talk-reels.zip" in r.headers["content-disposition"]
    zpath = tmp_path / "got.zip"
    zpath.write_bytes(r.content)
    names = zipfile.ZipFile(zpath).namelist()
    assert sorted(names) == sorted(f"my_talk-{cid}.mp4" for cid in ready)
    assert c.get("/api/jobs/zzzzzzzzzzzz/clips.zip").status_code == 404


def test_every_stage_records_how_long_it_took(tmp_path, video_bytes):
    from tests.test_service import FakePipeline, client, upload, wait
    c, app = client(tmp_path, FakePipeline())
    job = wait(c, upload(c, video_bytes).json()["id"])
    done = [s for s in job["stages"] if s["state"] == "done"]
    assert done and all(s["seconds"] is not None and s["seconds"] >= 0 for s in done)
