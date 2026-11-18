"""One person on screen, with camera cuts: the crop is on the new person from the first frame of each shot."""
import cv2
import numpy as np
import pytest

from clipcrow.graphics import thumbnail
from clipcrow.reframe import Box, Sample, plan_framing, pick_subject_per_shot, shot_cuts, smooth_shots

W, H = 1280, 720


def thumb(level):
    return np.full((18, 32, 3), float(level), np.float32)


def samples_with_cut(at=4.0, rate=5.0, seconds=8.0):
    out = []
    for k in range(int(seconds * rate) + 1):
        t = k / rate
        wob = (k % 3) * 0.7                                                   # a still picture breathes a little
        out.append(Sample(t, [Box(300, 200, 160, 160)] if t < at else [Box(900, 200, 160, 160)], [], thumb((40 if t < at else 190) + wob)))
    return out


def test_a_cut_is_found_where_the_picture_jumps_and_not_in_a_steady_shot():
    s = samples_with_cut()
    cuts = shot_cuts(s)
    assert len(cuts) == 1 and 3.8 <= cuts[0] <= 4.2
    steady = [Sample(k / 5, [], [], thumb(60 + (k % 4))) for k in range(41)]
    assert shot_cuts(steady) == []
    assert shot_cuts([Sample(k / 5) for k in range(10)]) == []                # no thumbnails: nothing to say


def test_a_flash_shorter_than_half_a_second_is_not_a_shot():
    s = samples_with_cut()
    for sm in s:
        if 2.0 <= sm.t < 2.2:
            sm.thumb = thumb(250)                                              # one bright frame
    assert len(shot_cuts(s)) == 1                                              # the real cut only; the flash and its return are folded away
    assert all(c > 3.0 for c in shot_cuts(s))


def test_each_shot_picks_its_own_face_and_the_crop_jumps_at_the_cut():
    s = samples_with_cut()
    cuts = shot_cuts(s)
    subject = pick_subject_per_shot(s, cuts, W, H)
    left = [cx for t, cx in subject if t < 3.5 and cx is not None]
    right = [cx for t, cx in subject if t > 4.5 and cx is not None]
    assert left and max(left) < 0.4 and right and min(right) > 0.6
    keys = smooth_shots(subject, cuts, 8.0, (H * 9 / 16) / W)
    before = [cx for t, cx in keys if t < cuts[0]]
    after = [cx for t, cx in keys if t >= cuts[0]]
    assert max(before) < 0.4 and min(after) > 0.6                              # no pan across the room: the first key of the new shot is on the new face
    assert [t for t, _ in keys] == sorted(t for t, _ in keys)


def test_without_cuts_the_old_path_is_unchanged():
    s = [Sample(k / 5, [Box(300 + k * 3, 200, 160, 160)], [], thumb(60 + (k % 3))) for k in range(41)]
    assert shot_cuts(s) == []
    subject = pick_subject_per_shot(s, [], W, H)
    from clipcrow.reframe import smooth_path
    assert smooth_shots(subject, [], 8.0, 0.3) == smooth_path(subject, 8.0, 0.3)


def test_a_shot_without_a_face_holds_where_the_last_one_was():
    s = samples_with_cut()
    for sm in s:
        if sm.t >= 4.0:
            sm.boxes = []
    cuts = shot_cuts(s)
    keys = smooth_shots(pick_subject_per_shot(s, cuts, W, H), cuts, 8.0, (H * 9 / 16) / W)
    assert max(cx for _, cx in keys) < 0.4


class BrightSquare:
    """Finds the one white square in the picture: a stand-in for a face detector."""

    def detect(self, frame):
        ys, xs = np.where(frame[:, :, 0] > 240)
        return [Box(float(xs.min()), float(ys.min()), float(xs.max() - xs.min() + 1), float(ys.max() - ys.min() + 1))] if len(xs) else []


def test_plan_framing_follows_the_person_after_a_camera_cut(tmp_path):
    path = tmp_path / "cut.avi"
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (W, H))
    for k in range(80):
        t = k / 10
        frame = np.full((H, W, 3), 40 if t < 4 else 190, np.uint8)
        x = 250 if t < 4 else 900
        frame[250:410, x:x + 160] = 255
        vw.write(frame)
    vw.release()
    f = plan_framing(str(path), 0, 8, detector=BrightSquare(), multi="off")
    assert f.framing == "track"
    early = [cx for t, cx in f.keyframes if t < 3.6]
    late = [cx for t, cx in f.keyframes if t > 4.4]
    assert early and max(early) < 0.4 and late and min(late) > 0.6
    first_late = min(t for t, cx in f.keyframes if cx > 0.6)
    assert first_late <= 4.3                                                   # on the new person at the cut, not panning there afterwards
