"""Several people on screen: who talks, when the picture cuts, and the stacked two-person layout."""
import subprocess
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from clipcrow.compile import prepare_footage
from clipcrow.edl import EDL, Layout, SplitPoint
from clipcrow.framing import apply_framing
from clipcrow.reframe import (Box, Sample, active_speakers, cut_path, normalise_activity, plan_framing,
                              segments, speaker_tracks)
from tests.conftest import base_edl

W, H = 1280, 720
LEFT, RIGHT = Box(200, 200, 160, 160), Box(920, 200, 160, 160)       # centres at 0.22 and 0.78 of the width


def noise(rng, amp):
    return rng.random((12, 24), dtype=np.float32) * amp


def two_talkers(seconds=12.0, switch=6.0, rate=5.0, amp_left=8.0, amp_right=8.0, idle=0.4, seed=1):
    """Left talks until ``switch``, right afterwards. A talking mouth changes a lot between samples, an idle one a little."""
    rng = np.random.default_rng(seed)
    samples = []
    base_l, base_r = np.full((12, 24), 100, np.float32), np.full((12, 24), 100, np.float32)
    for k in range(int(seconds * rate) + 1):
        t = k / rate
        left_talks = t < switch
        pl = base_l + noise(rng, amp_left if left_talks else idle)
        pr = base_r + noise(rng, idle if left_talks else amp_right)
        samples.append(Sample(t, [LEFT, RIGHT], [pl, pr]))
    return samples


# --- who is talking -------------------------------------------------------------------------

def test_speaker_tracks_keeps_two_people_left_to_right_and_drops_a_flicker():
    samples = two_talkers()
    for s in samples[:3]:                                      # a false positive seen in three samples only
        s.boxes.append(Box(600, 500, 100, 100))
        s.patches.append(np.zeros((12, 24), np.float32))
    people = speaker_tracks(samples, W, H)
    assert len(people) == 2
    assert sum(people[0]["pts"].values()) / len(people[0]["pts"]) < sum(people[1]["pts"].values()) / len(people[1]["pts"])


def test_active_speaker_follows_the_talking_mouth_and_switches_once():
    samples = two_talkers()
    people = speaker_tracks(samples, W, H)
    times = [s.t for s in samples]
    who = active_speakers(people, times)
    assert who[5] == 0 and who[-1] == 1                        # left early, right at the end
    assert sum(1 for a, b in zip(who, who[1:]) if a != b) == 1  # no flicker: exactly one change
    first_right = times[who.index(1)]
    assert 6.0 <= first_right <= 8.5                           # the change lags the real turn by a moment, not by long


def test_activity_is_normalised_per_person():
    # the right person is quiet (small movements) but the left person's idle camera noise is large in absolute terms:
    # compared raw the quiet person would never win, compared per person they do
    samples = two_talkers(amp_left=8.0, amp_right=1.5, idle=1.2)
    people = speaker_tracks(samples, W, H)
    who = active_speakers(people, [s.t for s in samples])
    assert who[-1] == 1


def test_normalise_activity_scales_by_the_persons_own_level():
    quiet = normalise_activity({1: 0.2, 2: 0.2, 3: 0.2, 4: 0.2, 5: 0.2, 6: 0.2, 7: 0.2, 8: 0.2, 9: 0.2, 10: 0.4})
    loud = normalise_activity({1: 2.0, 2: 2.0, 3: 2.0, 4: 2.0, 5: 2.0, 6: 2.0, 7: 2.0, 8: 2.0, 9: 2.0, 10: 4.0})
    assert quiet == pytest.approx(loud)
    assert normalise_activity({}) == {}
    assert max(quiet.values()) <= 2.0


def test_segments_are_runs_of_one_speaker_covering_the_clip():
    segs = segments([0, 1, 2, 3, 4, 5], [0, 0, 1, 1, 1, 0], 6.0)
    assert segs == [(0.0, 2, 0), (2, 5, 1), (5, 6.0, 0)]
    assert segments([], [], 5.0) == []


def test_cut_path_cuts_between_speakers_instead_of_panning():
    samples = two_talkers()
    people = speaker_tracks(samples, W, H)
    times = [s.t for s in samples]
    segs = segments(times, active_speakers(people, times), 12.0)
    crop = (H * 9 / 16) / W
    path = cut_path(people, segs, crop, W)
    ts = [t for t, _ in path]
    assert ts == sorted(ts)
    jumps = [(t1, abs(x1 - x0)) for (t0, x0), (t1, x1) in zip(path, path[1:]) if abs(x1 - x0) > 0.2]
    assert len(jumps) == 1                                     # exactly one hard cut, no slow pan across the room
    assert path[0][1] < 0.4 and path[-1][1] > 0.6
    assert len({t for t in ts}) == len(ts)                     # no two keyframes on one instant


# --- end to end on a video --------------------------------------------------------------------

cv2 = pytest.importorskip("cv2")


class TwoFaces:
    def __init__(self, boxes):
        self.boxes = boxes

    def detect(self, frame_bgr):
        return list(self.boxes)


def talkers_video(path: Path, seconds=12, fps=10, switch=6, boxes=(LEFT, RIGHT)):
    """Noise appears inside a box while that person 'talks'; the rest of the frame is flat grey."""
    rng = np.random.default_rng(3)
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (W, H))
    for k in range(seconds * fps):
        t = k / fps
        frame = np.full((H, W, 3), 110, np.uint8)
        for i, b in enumerate(boxes):
            talking = (t < switch) == (i == 0)
            x, y, w, h = int(b.x), int(b.y), int(b.w), int(b.h)
            frame[y:y + h, x:x + w] = 120
            if talking:
                frame[y + h // 2:y + h, x:x + w] = rng.integers(40, 220, (h - h // 2, w, 3), dtype=np.uint8)
        vw.write(frame)
    vw.release()
    return path


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    return talkers_video(tmp_path_factory.mktemp("v") / "talk.avi")


def test_plan_framing_cuts_to_the_speaker_who_talks(video):
    f = plan_framing(str(video), 0, 12, detector=TwoFaces([LEFT, RIGHT]), multi="switch")
    assert f.framing == "track" and f.people == 2
    early = [cx for t, cx in f.keyframes if t < 4]
    late = [cx for t, cx in f.keyframes if t > 9]
    assert early and max(early) < 0.4 and late and min(late) > 0.6
    assert sum(1 for (t0, a), (t1, b) in zip(f.keyframes, f.keyframes[1:]) if abs(b - a) > 0.2) == 1


def test_plan_framing_can_stack_two_people(video):
    f = plan_framing(str(video), 0, 12, detector=TwoFaces([LEFT, RIGHT]), multi="split")
    assert f.framing == "split" and len(f.split) == 2
    (lx, ly), (rx, ry) = f.split
    assert lx < 0.5 < rx and 0.2 < ly < 0.6 and 0.2 < ry < 0.6


def test_plan_framing_off_follows_one_face_as_before(video):
    f = plan_framing(str(video), 0, 12, detector=TwoFaces([LEFT, RIGHT]), multi="off")
    assert f.framing == "track" and f.people == 1


def test_two_people_close_together_share_one_window(tmp_path):
    a, b = Box(540, 200, 90, 90), Box(650, 200, 90, 90)         # a gap of 20 px between two faces
    v = talkers_video(tmp_path / "close.avi", boxes=(a, b))
    f = plan_framing(str(v), 0, 12, detector=TwoFaces([a, b]), multi="switch")
    xs = [cx for _, cx in f.keyframes]
    assert f.framing == "track" and max(xs) - min(xs) < 0.05     # no cutting back and forth between neighbours


def test_apply_framing_stores_the_split_and_moves_the_captions_to_the_seam(video):
    edl = EDL.model_validate(base_edl(clips=[{"id": "c1", "source_in": 0, "source_out": 12}]))
    out = apply_framing(edl, str(video), detector=TwoFaces([LEFT, RIGHT]), multi="split")
    c = out.clips[0]
    assert c.layout.framing == "split" and len(c.layout.split) == 2
    assert c.captions.top_px == edl.render.height // 2 - 90


# --- the EDL ------------------------------------------------------------------------------------

def test_layout_split_needs_exactly_two_points():
    pts = [SplitPoint(cx=0.25, cy=0.4), SplitPoint(cx=0.75, cy=0.4)]
    assert Layout(framing="split", split=pts).framing == "split"
    with pytest.raises(ValidationError):
        Layout(framing="split")
    with pytest.raises(ValidationError):
        Layout(framing="split", split=pts[:1])
    with pytest.raises(ValidationError):
        Layout(framing="cover", split=pts)
    with pytest.raises(ValidationError):
        SplitPoint(cx=1.2, cy=0.5)


# --- the stacked render (real ffmpeg) ------------------------------------------------------------

def test_split_render_stacks_the_two_people(tmp_path):
    src = tmp_path / "wide.mp4"                                  # left half red, right half blue
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=640x720:r=10:d=2",
                    "-f", "lavfi", "-i", "color=c=blue:s=640x720:r=10:d=2", "-filter_complex", "hstack",
                    "-pix_fmt", "yuv420p", str(src)], check=True)
    dest = tmp_path / "out.mp4"
    prepare_footage(src, 0, 1.5, 10, 1080, 1920, dest, framing="split", src_size=(1280, 720),
                    split=[(0.10, 0.5), (0.90, 0.5)])
    cap = cv2.VideoCapture(str(dest))
    assert (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))) == (1080, 1920)
    ok, frame = cap.read()
    cap.release()
    assert ok
    top_left, bottom_right = frame[100, 100], frame[1800, 1000]  # BGR
    assert top_left[2] > 150 > top_left[0]                       # the top window starts on the red person
    assert bottom_right[0] > 150 > bottom_right[2]               # the bottom window ends on the blue person
