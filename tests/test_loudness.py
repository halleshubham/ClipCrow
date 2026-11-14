"""Every finished reel is brought to about -14 LUFS; one that is already right is not touched."""
import subprocess
from pathlib import Path

import pytest

from clipcrow import loudness


def clip(path: Path, db: float, seconds=6.0, audio=True, tone="sine=f=300") -> Path:
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c=blue:s=160x284:r=10:d={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"{tone}:d={seconds}", "-af", f"volume={db}dB", "-c:a", "aac"]
    subprocess.run(cmd + ["-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def lufs(path):
    m = loudness.measure(path)
    return None if m is None else m["i"]


def test_a_quiet_clip_is_raised_to_the_target_and_the_picture_is_copied(tmp_path):
    p = clip(tmp_path / "q.mp4", -6)
    before = lufs(p)
    assert before is not None and before < -20
    size_video = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=codec_name,nb_frames", "-of", "csv=p=0", str(p)],
                                capture_output=True, text=True).stdout
    note = loudness.normalise(p)
    assert note and "LUFS" in note and abs(lufs(p) - loudness.TARGET) <= 1.0
    after = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=codec_name,nb_frames", "-of", "csv=p=0", str(p)],
                           capture_output=True, text=True).stdout
    assert after == size_video                                                        # the same video stream, not re-encoded
    assert not list(tmp_path.glob("*.loud.mp4"))


def test_a_loud_clip_is_brought_down_below_the_true_peak_limit(tmp_path):
    p = clip(tmp_path / "l.mp4", 6, tone="anoisesrc=color=pink:a=0.9")
    assert lufs(p) > -12
    assert loudness.normalise(p) and abs(lufs(p) - loudness.TARGET) <= 1.5


def test_a_clip_that_is_already_right_is_left_byte_for_byte(tmp_path):
    p = clip(tmp_path / "ok.mp4", -6)
    loudness.normalise(p)
    snapshot = p.read_bytes()
    assert loudness.normalise(p) is None and p.read_bytes() == snapshot


def test_no_audio_silence_and_hopelessly_quiet_clips_are_left_alone(tmp_path):
    silent_video = clip(tmp_path / "v.mp4", 0, audio=False)
    silence = clip(tmp_path / "s.mp4", 0, tone="anullsrc=r=44100:cl=mono")
    faint = clip(tmp_path / "f.mp4", -70)
    for p in (silent_video, silence, faint):
        snapshot = p.read_bytes()
        assert loudness.normalise(p) is None and p.read_bytes() == snapshot, p.name


def test_an_unreadable_file_is_not_an_error(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video")
    assert loudness.measure(bad) is None and loudness.normalise(bad) is None


def test_the_operator_can_turn_it_off_and_the_render_calls_it_after_the_outro():
    src = (Path(__file__).resolve().parent.parent / "clipcrow" / "pipeline.py").read_text(encoding="utf-8")
    assert src.index("append_outro") < src.index("loudness.normalise") < src.index("extract_cover") and "CLIPCROW_LOUDNORM" in src
