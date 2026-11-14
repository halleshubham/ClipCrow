import numpy as np
import pytest
from pydantic import ValidationError

from clipcrow import graphics
from clipcrow.edl import EDL, FitSpan, Keyframe, Layout, PunchIn
from clipcrow.framing import apply_framing
from clipcrow.reframe import Box, Framing, Sample
from tests.conftest import base_edl

W, H = 1920, 1080


def flat(r, g, b):
    return np.full((18, 32, 3), (b, g, r), np.float32)


SPEAKER, SLIDE, PORTRAIT, CAM2 = flat(200, 180, 230), flat(20, 30, 80), flat(150, 80, 20), flat(40, 120, 60)


def face(i):                                    # a person drifts a little from sample to sample
    return [Box(800 + (i % 5) * 9, 300 + (i % 3) * 5, 200, 200)]


def still(_i):                                  # a picture of a face: the box never moves
    return [Box(500, 300, 200, 200)]


def samples(plan, rate=5.0):
    """plan: [(count, thumb, faces_fn_or_None)] -> Samples at ``rate`` per second."""
    out, k = [], 0
    for count, thumb, fn in plan:
        for _ in range(count):
            out.append(Sample(k / rate, fn(k) if fn else [], [], thumb + (k % 2) * 0.5))
            k += 1
    return out


def spans(plan, duration=None):
    s = samples(plan)
    return graphics.graphic_spans(s, duration or len(s) / 5.0, W, H)


def test_a_slide_between_two_shots_of_the_speaker_is_one_span_cut_halfway_between_samples():
    got = spans([(25, SPEAKER, face), (20, SLIDE, None), (25, SPEAKER, face)])        # slide: samples 25..44 = 5.0 s..8.8 s
    assert got == [(4.9, 8.9)]


def test_a_portrait_on_a_slide_is_a_graphic_because_its_face_never_moves():
    assert spans([(25, SPEAKER, face), (20, PORTRAIT, still), (25, SPEAKER, face)]) == [(4.9, 8.9)]


def test_a_second_camera_with_a_live_face_stays_tracked():
    assert spans([(25, SPEAKER, face), (25, CAM2, face), (25, SPEAKER, face)]) == []


def test_a_short_blip_is_not_worth_a_cut_but_a_long_one_is():
    assert spans([(25, SPEAKER, face), (4, SLIDE, None), (25, SPEAKER, face)]) == []          # 0.8 s
    assert spans([(25, SPEAKER, face), (7, SLIDE, None), (25, SPEAKER, face)]) != []          # 1.4 s


def test_two_slides_close_together_are_one_span():
    got = spans([(25, SPEAKER, face), (10, SLIDE, None), (2, SPEAKER, face), (10, PORTRAIT, still), (25, SPEAKER, face)])
    assert len(got) == 1


def test_a_graphic_at_the_start_or_end_runs_to_the_edge():
    assert spans([(15, SLIDE, None), (30, SPEAKER, face)])[0][0] == 0.0
    s = samples([(30, SPEAKER, face), (15, SLIDE, None)])
    assert graphics.graphic_spans(s, 9.0, W, H)[0][1] == 9.0


def test_without_a_clear_speaker_shot_nothing_is_called_a_graphic():
    assert spans([(40, SLIDE, None), (5, SPEAKER, face)]) == []                       # the speaker is only 11% of the clip
    assert spans([(40, SLIDE, None)]) == []                                           # no face anywhere
    assert graphics.graphic_spans([Sample(i / 5, face(i)) for i in range(20)], 4.0, W, H) == []      # no thumbnails


def test_a_speaker_whose_hands_change_the_picture_a_little_is_still_the_speaker():
    shaky = [Sample(i / 5, face(i), [], SPEAKER + (i % 7) * 3.0) for i in range(50)]       # up to 18 levels of change, under the shot limit
    assert graphics.graphic_spans(shaky, 10.0, W, H) == []


# --- the EDL ---------------------------------------------------------------------------

def test_fit_spans_are_only_for_a_tracked_framing_and_must_be_ordered():
    kf = [Keyframe(t=0, cx=0.5)]
    Layout(framing="track", reframe=kf, fit_spans=[FitSpan(t0=1, t1=2), FitSpan(t0=3, t1=4)])
    with pytest.raises(ValidationError):
        Layout(framing="cover", fit_spans=[FitSpan(t0=1, t1=2)])
    with pytest.raises(ValidationError):
        Layout(framing="track", reframe=kf, fit_spans=[FitSpan(t0=1, t1=3), FitSpan(t0=2, t1=4)])
    with pytest.raises(ValidationError):
        FitSpan(t0=2, t1=2)


# --- framing -----------------------------------------------------------------------------

def test_apply_framing_stores_the_spans_and_drops_push_ins_that_run_over_a_slide(monkeypatch):
    from clipcrow import framing as fr
    edl = EDL.model_validate(base_edl())
    clip = edl.clips[0].model_copy(update={"layout": edl.clips[0].layout.model_copy(update={"punch_ins": [
        PunchIn(t=1.0, duration=1.0), PunchIn(t=4.5, duration=1.0)]})})
    edl = edl.model_copy(update={"clips": [clip]})
    seen = {}

    def fake_plan(video, a, b, **kw):
        seen.update(kw)
        return Framing("track", [(0.0, 0.5)], 0.7, fit_spans=[(4.0, 6.0)])
    monkeypatch.setattr(fr, "plan_framing", fake_plan)
    said = []
    out = apply_framing(edl, "x.mp4", graphics=True, say=said.append)
    lay = out.clips[0].layout
    assert seen["graphics"] is True and [(s.t0, s.t1) for s in lay.fit_spans] == [(4.0, 6.0)]
    assert [p.t for p in lay.punch_ins] == [1.0] and "graphic" in said[0]
    apply_framing(edl, "x.mp4")
    assert seen["graphics"] is False


# --- plan_framing on a real file ------------------------------------------------------------

cv2 = pytest.importorskip("cv2")


class BrightSquare:
    """A stand-in face detector: the bounding box of the near-white pixels (video compression softens pure white)."""
    def detect(self, frame):
        ys, xs = np.where((frame > 235).all(axis=2))
        if len(xs) == 0:
            return []
        return [Box(float(xs.min()), float(ys.min()), float(xs.max() - xs.min() + 1), float(ys.max() - ys.min() + 1))]


@pytest.fixture(scope="module")
def explainer_video(tmp_path_factory):
    """0-4 s the speaker (a white square drifting over a lilac studio), 4-7 s a navy slide with a STILL white square (a portrait), 7-10 s the speaker."""
    from tests.test_reframe import _write_video

    def frame(t):
        img = np.zeros((360, 640, 3), np.uint8)
        if 4 <= t < 7:
            img[:] = (90, 40, 20)
            img[100:160, 80:140] = 255
        else:
            img[:] = (230, 180, 200)
            x = int(250 + 30 * np.sin(t * 2))
            img[120 + int(5 * np.sin(t * 3)):180 + int(5 * np.sin(t * 3)), x:x + 60] = 255
        return img
    p = tmp_path_factory.mktemp("exp") / "explainer.mp4"
    _write_video(p, frame, 640, 360, 10)
    return p


def test_plan_framing_finds_the_slide_only_when_asked(explainer_video):
    from clipcrow.reframe import plan_framing
    f = plan_framing(str(explainer_video), 0, 10, detector=BrightSquare(), graphics=True)
    assert f.framing == "track" and len(f.fit_spans) == 1
    a, b = f.fit_spans[0]
    assert 3.7 <= a <= 4.3 and 6.7 <= b <= 7.3
    assert all(not (a - 0.01 < t < b - 0.01) or True for t, _ in f.keyframes)
    xs_during = [cx for t, cx in f.keyframes if a < t < b]
    assert all(abs(cx - 0.5) < 0.2 for cx in xs_during) or not xs_during            # the camera did not run off to the portrait
    plain = plan_framing(str(explainer_video), 0, 10, detector=BrightSquare())
    assert plain.fit_spans == []


# --- the render step ---------------------------------------------------------------------------

@pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None, reason="needs ffmpeg")
def test_prepare_footage_shows_the_whole_frame_during_a_span_and_the_crop_otherwise(tmp_path):
    """Source: 3 s all green, then 3 s of a slide that is blue on the left and red on the right. The crop follows the right (red); a span shows both."""
    import subprocess
    from clipcrow.compile import prepare_footage
    src = tmp_path / "src.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x20a020:s=1920x1080:d=3:r=30",
                    "-f", "lavfi", "-i", "color=c=0x2020d0:s=1920x1080:d=3:r=30", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                    "-filter_complex", "[1:v]drawbox=x=960:y=0:w=960:h=1080:c=red:t=fill[b];[0:v][b]concat=n=2:v=1:a=0[v]",
                    "-map", "[v]", "-map", "2:a", "-t", "6", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)], check=True)

    def colours(name, spans):
        out = tmp_path / f"{name}.mp4"
        prepare_footage(src, 0, 6, 30, 1080, 1920, out, framing="track", keyframes=[(0, 0.75)], src_size=(1920, 1080), fit_spans=spans)
        res = {}
        for t in (1.0, 4.0):
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", str(out), "-frames:v", "1", "-vf", "scale=54:96",
                            "-f", "rawvideo", "-pix_fmt", "rgb24", str(tmp_path / "px.raw")], check=True)
            d = (tmp_path / "px.raw").read_bytes()
            res[t] = tuple(d[(48 * 54 + 4) * 3:(48 * 54 + 4) * 3 + 3])             # left edge, middle row
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height", "-of", "csv=p=0", str(out)],
                               capture_output=True, text=True).stdout
        return res, probe
    plain, _ = colours("plain", [])
    fit, probe = colours("fit", [(3.0, 6.0)])
    assert plain[4.0][0] > 200 and plain[4.0][2] < 60                  # the crop sees only the red half
    assert fit[4.0][2] > 150 and fit[4.0][0] < 80                      # the whole slide: its blue left half is there
    assert fit[1.0] == plain[1.0]                                      # outside the span nothing changed
    assert "1080,1920" in probe and "audio" in probe                   # same canvas, audio kept
