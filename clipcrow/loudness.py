"""Make every reel about as loud as the platforms play it: -14 LUFS integrated, true peak at most -1.5 dB.

Two passes of ffmpeg's ``loudnorm`` (measure, then apply the measured values linearly), on the FINISHED clip (the outro included), so the picture is copied and
only the audio is re-encoded. A clip that is already within ``TOLERANCE`` of the target and has headroom is left byte for byte as it is (no re-encode, and a
render that was fine stays deterministic). Silence, a clip with no audio, and a clip so quiet that it would need more than ``MAX_GAIN`` dB are left alone: that is
a recording problem, and amplifying it only raises the noise. Any failure keeps the original clip.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Optional

TARGET = -14.0                # LUFS integrated
TRUE_PEAK = -1.5              # dBTP
LRA = 11.0
TOLERANCE = 1.0               # within this many LU of the target nothing is done
MAX_GAIN = 18.0               # dB
FLOOR = -60.0                 # quieter than this is silence


def measure(path: str | Path, *, target: float = TARGET) -> Optional[dict]:
    """``{"i", "tp", "lra", "thresh", "offset"}`` of the first audio stream, or None (no audio, silence, unreadable)."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af",
                        f"loudnorm=I={target}:TP={TRUE_PEAK}:LRA={LRA}:print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=600)
    blocks = re.findall(r"\{[^{}]*\}", r.stderr)
    if r.returncode != 0 or not blocks:
        return None
    try:
        d = json.loads(blocks[-1])
        out = {"i": float(d["input_i"]), "tp": float(d["input_tp"]), "lra": float(d["input_lra"]),
               "thresh": float(d["input_thresh"]), "offset": float(d["target_offset"])}
    except (ValueError, KeyError, TypeError):
        return None
    return out if out["i"] > FLOOR else None                 # "-inf" parses as -inf: silence


def normalise(path: str | Path, *, target: float = TARGET) -> Optional[str]:
    """Bring the clip at ``path`` to the target loudness in place. Returns a sentence saying what was done, or None when nothing was."""
    path = Path(path)
    m = measure(path, target=target)
    if m is None:
        return None
    gain = target - m["i"]
    if abs(gain) <= TOLERANCE and m["tp"] <= TRUE_PEAK + 0.5:
        return None
    if gain > MAX_GAIN:
        return None
    tmp = path.with_name(path.stem + ".loud.mp4")
    af = (f"loudnorm=I={target}:TP={TRUE_PEAK}:LRA={LRA}:measured_I={m['i']}:measured_TP={m['tp']}:measured_LRA={m['lra']}"
          f":measured_thresh={m['thresh']}:offset={m['offset']}:linear=true")
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-map", "0:v", "-map", "0:a:0", "-c:v", "copy", "-af", af,
                        "-ar", "48000", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(tmp)],
                       capture_output=True, text=True, timeout=900)
    if r.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        return None
    after = measure(tmp, target=target)
    if after is None or abs(after["i"] - target) > abs(m["i"] - target):        # it did not help: keep what we had
        tmp.unlink(missing_ok=True)
        return None
    tmp.replace(path)
    return f"loudness set from {m['i']:.1f} to {after['i']:.1f} LUFS"
