import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from clipcrow.edl import EDL, Keyframe
from clipcrow.framing import apply_framing
from clipcrow.reframe import Box, Sample, pick_subject, smooth_path
from tests.conftest import base_edl

DATA = Path(__file__).parent / "data"


# --- pure logic (no OpenCV) ---------------------------------------------------------------

def test_pick_subject_follows_the_main_face_and_ignores_a_false_positive():
    W, H = 1280, 720
    samples = []
    for i in range(10):
        boxes = [Box(300 + i * 40, 200, 120, 120)]                       # the real face, drifting right
        if i in (3, 4):
            boxes.append(Box(1000, 500, 130, 130))                        # a false positive, bigger, in two samples
        samples.append(Sample(i * 0.2, boxes))
    subj = pick_subject(samples, W, H)
    assert all(c is not None for _, c in subj)
    assert subj[0][1] == pytest.approx((300 + 60) / W) and subj[9][1] == pytest.approx((660 + 60) / W)


def test_pick_subject_leaves_gaps_when_the_face_is_lost():
    samples = [Sample(0, [Box(500, 200, 100, 100)]), Sample(.2, []), Sample(.4, [Box(520, 200, 100, 100)])]
    assert [c is None for _, c in pick_subject(samples, 1280, 720)] == [False, True, False]


def test_smooth_path_holds_still_inside_the_dead_band():
    crop = 0.32
    subject = [(t / 5, 0.50 + 0.01 * ((-1) ** t)) for t in range(25)]       # small jitter around 0.5
    path = smooth_path(subject, 5.0, crop)
    xs = [x for _, x in path]
    assert max(xs) - min(xs) < 0.005


def test_smooth_path_is_speed_limited_and_stays_in_frame():
    crop = 0.32
    subject = [(0.0, 0.2), (0.2, 0.2), (0.4, 0.95), (4.0, 0.95)]            # the subject jumps across the frame
    path = smooth_path(subject, 4.0, crop)
    for (t0, x0), (t1, x1) in zip(path, path[1:]):
        assert abs(x1 - x0) / (t1 - t0) <= 0.60 * crop + 1e-6              # never faster than the limit
    assert all(crop / 2 - 1e-9 <= x <= 1 - crop / 2 + 1e-9 for _, x in path)
    assert path[-1][1] > path[0][1] + 0.2                                    # but it does get there


def test_smooth_path_without_any_detection_is_empty():
    assert smooth_path([(0, None), (1, None)], 2.0, 0.32) == []


# --- EDL ----------------------------------------------------------------------------------

def mk(**layout):
    return EDL.model_validate(base_edl(clips=[{"id": "c", "source_in": 0, "source_out": 5, "layout": layout}]))


def test_track_framing_needs_keyframes_and_they_must_be_ordered():
    with pytest.raises(ValidationError):
        mk(framing="track")
    with pytest.raises(ValidationError):
        mk(framing="track", reframe=[{"t": 2, "cx": .5}, {"t": 1, "cx": .5}])
    with pytest.raises(ValidationError):
        mk(framing="track", reframe=[{"t": 1, "cx": 1.5}])
    assert mk(framing="track", reframe=[{"t": 0, "cx": .4}, {"t": 1, "cx": .5}]).clips[0].layout.framing == "track"


# --- with real video (OpenCV + FFmpeg) -------------------------------------------------------

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")


def _write_video(path, frame_fn, w, h, secs, fps=30):
    p = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}",
                          "-r", str(fps), "-i", "-", "-c:v", "libx264", "-g", str(fps), "-pix_fmt", "yuv420p",
                          "-crf", "18", str(path)], stdin=subprocess.PIPE)
    for n in range(int(fps * secs)):
        p.stdin.write(frame_fn(n / fps).tobytes())
    p.stdin.close()
    assert p.wait() == 0


@pytest.fixture(scope="module")
def pan_video(tmp_path_factory):
    """1280x720, 6 s: a real (public-domain) face drifting left to right across the frame."""
    face = cv2.resize(cv2.imread(str(DATA / "face.jpg")), (560, 560))
    W, H = 1280, 720

    def frame(t):
        f = np.zeros((H, W, 3), np.uint8); f[:] = (60, 40, 40)
        x = int(40 + (W - 560 - 80) * t / 6)
        f[80:640, x:x + 560] = face
        return f
    path = tmp_path_factory.mktemp("v") / "pan.mp4"
    _write_video(path, frame, W, H, 6)
    return path


def _face_center_fraction(video, t):
    from clipcrow.reframe import HaarDetector
    out = Path(str(video) + f".{t}.png")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(t), "-i", str(video), "-frames:v", "1", str(out)], check=True)
    img = cv2.imread(str(out))
    boxes = HaarDetector().detect(img)
    if not boxes:
        return None
    b = max(boxes, key=lambda b: b.w)
    return b.cx / img.shape[1]


def test_plan_framing_tracks_a_moving_face(pan_video):
    from clipcrow.reframe import plan_framing
    f = plan_framing(str(pan_video), 0, 6)
    assert f.framing == "track" and f.coverage > 0.6
    xs = [cx for _, cx in f.keyframes]
    assert xs[-1] > xs[0] + 0.15                                              # the camera moved right with the face
    assert all(0 < x < 1 for x in xs)


def test_vertical_source_is_left_alone(tmp_path):
    from clipcrow.reframe import plan_framing
    v = tmp_path / "v.mp4"
    _write_video(v, lambda t: np.zeros((640, 360, 3), np.uint8), 360, 640, 1)
    assert plan_framing(str(v), 0, 1).framing == "cover"


def test_no_face_falls_back_to_a_blurred_fit(tmp_path):
    from clipcrow.reframe import plan_framing
    v = tmp_path / "blank.mp4"
    _write_video(v, lambda t: np.full((360, 640, 3), 80, np.uint8), 640, 360, 2)
    f = plan_framing(str(v), 0, 2)
    assert f.framing == "fit_blur" and f.keyframes == []


def test_footage_follows_the_face_when_cut(pan_video, tmp_path):
    """The point of the whole module: in the cut 9:16 footage the face stays near the middle."""
    from clipcrow.compile import prepare_footage
    from clipcrow.reframe import plan_framing
    f = plan_framing(str(pan_video), 0, 6)
    dest = tmp_path / "footage.mp4"
    prepare_footage(pan_video, 0, 6, 30, 1080, 1920, dest, framing="track", keyframes=f.keyframes, src_size=(1280, 720))
    for t in (0.5, 3.0, 5.5):
        c = _face_center_fraction(dest, t)
        assert c is not None and 0.2 < c < 0.8, (t, c)
    # control: this scenario really needs tracking. The face drifts from x~330 to x~950 of the 1280 px frame,
    # but a centre crop of 405 px only sees x 437..843, so at both ends the face is outside it.
    face_cx = lambda t: 40 + (1280 - 560 - 80) * t / 6 + 280
    assert face_cx(0.1) < (1280 - 405) / 2 and face_cx(5.8) > (1280 + 405) / 2


def test_fit_blur_output_has_the_canvas_size(tmp_path):
    from clipcrow.compile import prepare_footage
    v = tmp_path / "blank.mp4"
    _write_video(v, lambda t: np.full((360, 640, 3), 80, np.uint8), 640, 360, 1)
    out = tmp_path / "o.mp4"
    prepare_footage(v, 0, 1, 30, 1080, 1920, out, framing="fit_blur")
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                        "-of", "csv=p=0", str(out)], capture_output=True, text=True)
    assert r.stdout.strip() == "1080,1920"


def test_apply_framing_sets_layouts_and_respects_an_explicit_choice(pan_video):
    d = base_edl(source={"uri": "pan.mp4", "duration": 6, "width": 1280, "height": 720, "fps": 30}, clips=[
        {"id": "auto", "source_in": 0, "source_out": 6},
        {"id": "manual", "source_in": 0, "source_out": 6, "layout": {"framing": "fit_blur"}}])
    out = apply_framing(EDL.model_validate(d), str(pan_video))
    assert out.clips[0].layout.framing == "track" and out.clips[0].layout.reframe
    assert out.clips[1].layout.framing == "fit_blur" and out.clips[1].layout.reframe == []
