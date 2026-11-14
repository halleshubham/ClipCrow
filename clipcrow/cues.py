"""Sound cues: quiet pops, whooshes, ticks and dings under the speech, placed by code from the clip's own events.

  * WHICH sound goes WHERE is a fixed table of events (``CUE_FOR``): a stat appearing is a ding, a picture a shutter, a lower third a soft whoosh, a hook a pop, a
    push-in a click. The model never picks a sound or a time, and a cue is never put on a caption word.
  * Density: at most one cue per ``MIN_GAP`` seconds. When two events are closer, the more important one stays (``PRIORITY``).
  * Level: each file is gained so its PEAK lands at ``Sfx.level_db`` (default -10 dBFS: well under the speech), using the peak recorded in ``sfx/manifest.json``.
  * A cue starts ``LEAD`` seconds before the visual event (a whoosh that arrives with the picture sounds late) but never before the clip starts.
  * Mixed with one ffmpeg pass (``adelay`` + ``volume`` + ``amix normalize=0``), picture copied, speech untouched. A clip with no audio, or any failure, is left as it was.
The files and their licence are in ``clipcrow/sfx`` (see ``scripts/make_sfx.py``).
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .edl import Clip
from .transitions import hard_cuts

SFX_DIR = Path(__file__).parent / "sfx"
MIN_GAP = 2.0
LEAD = 0.08
MAX_CUES = 40
# overlay kind (or "punch") -> sound id; higher PRIORITY wins when events are closer than MIN_GAP
CUE_FOR = {"stat_pill": "ding", "media": "shutter", "lower_third": "whoosh-soft", "hook": "pop", "punch": "click", "cut": "click"}
PRIORITY = {"stat_pill": 5, "media": 4, "lower_third": 3, "hook": 2, "cut": 1, "punch": 0}


@dataclass(frozen=True)
class Cue:
    t: float            # clip time, seconds
    sound: str          # id in the manifest
    kind: str           # the event it belongs to


def manifest() -> dict:
    return json.loads((SFX_DIR / "manifest.json").read_text(encoding="utf-8"))["sounds"]


def plan_cues(clip: Clip) -> list[Cue]:
    events: list[tuple[float, str]] = []
    for o in clip.overlays:
        if o.kind in CUE_FOR and getattr(o, "t_in", None) is not None:
            events.append((float(o.t_in), o.kind))
    events += [(float(p.t), "punch") for p in clip.layout.punch_ins]
    events += [(float(t), "cut") for t in hard_cuts(clip)]
    chosen: list[tuple[float, str]] = []
    for t, kind in sorted(events, key=lambda e: (-PRIORITY[e[1]], e[0])):            # the important ones first
        if 0 <= t < clip.duration - 0.2 and all(abs(t - c[0]) >= MIN_GAP for c in chosen):
            chosen.append((t, kind))
    chosen.sort()
    return [Cue(round(max(0.0, t - LEAD), 3), CUE_FOR[k], k) for t, k in chosen[:MAX_CUES]]


def _channels(path: Path) -> int:
    """How many channels the first audio stream has (0 = none, or unreadable)."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=channels", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, timeout=60)
    try:
        return int(r.stdout.strip().splitlines()[0]) if r.returncode == 0 and r.stdout.strip() else 0
    except ValueError:
        return 0


def mix(video: str | Path, cues: list[Cue], level_db: float, *, sounds: Optional[dict] = None) -> Optional[int]:
    """Lay ``cues`` under the audio of ``video`` in place. Returns how many were mixed, or None when nothing was done (no audio, no cues, a failure)."""
    video = Path(video)
    sounds = sounds or manifest()
    cues = [c for c in cues if c.sound in sounds and (SFX_DIR / sounds[c.sound]["file"]).is_file()]
    channels = _channels(video)
    if not cues or channels == 0:
        return None
    layout = "mono" if channels == 1 else "stereo"                 # the speech keeps its own layout; the cues are made to match it
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(video)]
    parts = []
    for k, c in enumerate(cues, start=1):
        cmd += ["-i", str(SFX_DIR / sounds[c.sound]["file"])]
        peak = sounds[c.sound].get("peak_db")
        gain = round(level_db - (peak if isinstance(peak, (int, float)) else 0.0), 2)
        parts.append(f"[{k}:a]aformat=sample_rates=48000:channel_layouts={layout},adelay={int(round(c.t * 1000))}:all=1,volume={gain}dB[c{k}]")
    labels = "".join(f"[c{k}]" for k in range(1, len(cues) + 1))
    parts.append(f"[0:a]aformat=sample_rates=48000:channel_layouts={layout}[sp]")
    parts.append(f"[sp]{labels}amix=inputs={len(cues) + 1}:normalize=0:duration=first:dropout_transition=0[mixed]")
    tmp = video.with_name(video.stem + ".cues.mp4")
    r = subprocess.run(cmd + ["-filter_complex", ";".join(parts), "-map", "0:v", "-map", "[mixed]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                              "-movflags", "+faststart", str(tmp)], capture_output=True, text=True, timeout=900)
    if r.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        return None
    tmp.replace(video)
    return len(cues)
