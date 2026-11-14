"""Background music: the person's own track, gained to sit under the speech and ducked while someone talks."""
import re
import subprocess
from pathlib import Path

import pytest

from clipcrow import music


def run(cmd):
    subprocess.run(cmd, check=True, capture_output=True)


def clip_with_speech(path: Path, seconds=8.0):
    """1 kHz tone 'speech' in 0-2 s and 4-6 s, silence in between; a blue picture."""
    run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c=blue:s=160x284:r=10:d={seconds}", "-f", "lavfi", "-i", f"sine=f=1000:d={seconds}",
         "-af", "volume='if(between(t,0,2)+between(t,4,6),4,0)':eval=frame", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)])
    return path


def bed(path: Path, seconds=10.0):
    run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=f=200:d={seconds}", "-c:a", "aac", str(path)])
    return path


def level_of(path, start, length, centre):
    """Peak level (dB) of what is near ``centre`` Hz inside a window."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-ss", str(start), "-t", str(length), "-i", str(path), "-vn", "-af",
                        f"bandpass=f={centre}:width_type=h:w=60,volumedetect", "-f", "null", "-"], capture_output=True, text=True)
    return float(re.search(r"max_volume: (-?[\d.]+) dB", r.stderr).group(1))


def test_the_track_is_louder_in_the_pauses_than_under_the_speech(tmp_path):
    v, b = clip_with_speech(tmp_path / "v.mp4"), bed(tmp_path / "bed.m4a")
    speech_before = level_of(v, 0.8, 1.0, 1000)
    gain = music.mix(v, b, -10.0)
    assert gain is not None
    in_pause = level_of(v, 2.8, 0.9, 200)                  # nobody talks: the bed at its own level
    under_speech = level_of(v, 0.8, 1.0, 200)              # the tone is talking: the bed is pushed down
    assert in_pause - under_speech > 6, (in_pause, under_speech)
    assert abs(level_of(v, 0.8, 1.0, 1000) - speech_before) < 1.5   # and the speech itself is as loud as it was


def test_the_bed_sits_below_the_speech_by_the_asked_level_in_the_pauses(tmp_path):
    v, b = clip_with_speech(tmp_path / "v.mp4"), bed(tmp_path / "bed.m4a")
    speech_before = level_of(v, 0.8, 1.0, 1000)
    music.mix(v, b, -10.0)
    gap = level_of(v, 2.8, 0.9, 200)
    assert -22 < gap - speech_before < -4                   # roughly "10 dB under the speech" (loudness vs peak, with fades and the sine's crest)


def test_picture_and_length_are_kept_and_the_track_loops_when_it_is_shorter(tmp_path):
    v, b = clip_with_speech(tmp_path / "v.mp4", seconds=8.0), bed(tmp_path / "bed.m4a", seconds=5.0)
    frames = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=nb_frames", "-of", "csv=p=0", str(v)], capture_output=True, text=True).stdout
    assert music.mix(v, b, -10.0) is not None
    after = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=nb_frames", "-of", "csv=p=0", str(v)], capture_output=True, text=True).stdout
    assert frames == after and abs(music._duration(v) - 8.0) < 0.3
    assert level_of(v, 6.5, 1.0, 200) > -60                  # the bed is still playing after the 5 s track ended (it looped), fading out at the end


def test_no_speech_no_audio_or_an_unreadable_track_leave_the_clip_alone(tmp_path):
    ok = clip_with_speech(tmp_path / "ok.mp4")
    silent_video = tmp_path / "v.mp4"
    run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=160x284:r=10:d=4", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(silent_video)])
    quiet = tmp_path / "quiet.mp4"
    run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=160x284:r=10:d=4", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-shortest",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(quiet)])
    junk = tmp_path / "junk.m4a"
    junk.write_bytes(b"not audio")
    good = bed(tmp_path / "bed.m4a")
    for v, t in ((silent_video, good), (quiet, good), (ok, junk)):
        snap = v.read_bytes()
        assert music.mix(v, t, -10.0) is None and v.read_bytes() == snap, v.name
    assert not list(tmp_path.glob("*.music.mp4"))


def test_the_render_mixes_the_music_before_the_cues_the_outro_and_the_loudness_pass():
    src = (Path(__file__).resolve().parent.parent / "clipcrow" / "pipeline.py").read_text(encoding="utf-8")
    assert src.index("music.mix") < src.index("cues.mix") < src.index("append_outro") < src.index("loudness.normalise")
