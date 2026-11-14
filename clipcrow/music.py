"""A background track under the speech, ducked while someone talks.

The track is the ACCOUNT'S OWN upload (the person says they may use it; we ship no music, see ``.claude/development-rules.md``). One ffmpeg pass:
  * the track is looped to the clip's length, faded in (0.6 s) and out (1.5 s), and gained so that, while nobody talks, it sits ``level_db`` below the
    SPEECH's integrated loudness (both measured with ``loudness.measure``), whatever the track's own level;
  * the speech itself is the sidechain of a compressor on the track (``sidechaincompress``): while the speech is above the threshold the track is pushed
    down by several more dB, and it comes back up in the pauses (attack 10 ms, release 350 ms);
  * the speech is untouched, the picture is copied, the result is mixed with ``amix normalize=0`` so nothing else changes level.
No audio in the clip, speech too quiet to measure, an unreadable track or any failure leaves the clip as it was.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from . import cues, loudness

THRESHOLD = 0.02          # linear: about -34 dBFS, below the speech of any usable recording
RATIO = 8
ATTACK_MS, RELEASE_MS = 10, 350
FADE_IN, FADE_OUT = 0.6, 1.5
MAX_GAIN = 40.0


def _duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=60)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def mix(video: str | Path, track: str | Path, level_db: float) -> Optional[float]:
    """Put ``track`` under the speech of ``video`` in place. Returns the gain given to the track (dB), or None when nothing was done."""
    video, track = Path(video), Path(track)
    speech = loudness.measure(video)
    bed = loudness.measure(track)
    dur = _duration(video)
    if speech is None or bed is None or dur < 1.0:
        return None
    layout = "mono" if cues._channels(video) == 1 else "stereo"          # the speech keeps its own layout (mono -> stereo would lower it by 3 dB)
    gain = max(-MAX_GAIN, min(MAX_GAIN, speech["i"] + level_db - bed["i"]))
    fade_out_at = max(0.0, dur - FADE_OUT)
    fc = (f"[1:a]aformat=sample_rates=48000:channel_layouts={layout},atrim=0:{dur:.3f},asetpts=PTS-STARTPTS,"
          f"afade=t=in:st=0:d={FADE_IN},afade=t=out:st={fade_out_at:.3f}:d={FADE_OUT},volume={gain:.2f}dB[bed];"
          f"[0:a]aformat=sample_rates=48000:channel_layouts={layout},asplit=2[sp][sc];"
          f"[bed][sc]sidechaincompress=threshold={THRESHOLD}:ratio={RATIO}:attack={ATTACK_MS}:release={RELEASE_MS}:makeup=1[ducked];"
          f"[sp][ducked]amix=inputs=2:normalize=0:duration=first:dropout_transition=0[mixed]")
    tmp = video.with_name(video.stem + ".music.mp4")
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-stream_loop", "-1", "-i", str(track), "-filter_complex", fc,
                        "-map", "0:v", "-map", "[mixed]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-t", f"{dur:.3f}", "-movflags", "+faststart", str(tmp)],
                       capture_output=True, text=True, timeout=1200)
    if r.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        return None
    tmp.replace(video)
    return round(gain, 2)
