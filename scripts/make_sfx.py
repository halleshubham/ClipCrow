"""Generate the sound-effect library with the ElevenLabs sound-effects API and write ``clipcrow/sfx/manifest.json``.

    ELEVENLABS_API_KEY=... python scripts/make_sfx.py [--force] [--only id,id]

Existing files are kept (each generation costs credits and gives a different sound); ``--force`` makes them again. The key is read from the
environment, never printed or stored. Every sound gets a provenance row: the prompt, the date, the tool, a hash, its length and loudness.

LICENCE: ElevenLabs' help centre says a paid plan includes a commercial licence for what it generates and the free plan does not. Run this on a paid
plan and keep the invoice with the date in the manifest's ``generated`` field. Nothing here is a third party's sound: no Freesound/YouTube/Epidemic files.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import requests

OUT = Path(__file__).resolve().parent.parent / "clipcrow" / "sfx"
URL = "https://api.elevenlabs.io/v1/sound-generation?output_format=mp3_44100_128"
# id: (what it is for, prompt, seconds). Short, dry, no music: they sit under speech.
SOUNDS = {
    "whoosh-soft": ("a quick transition between two moments", "Soft fast air whoosh, short transition swoosh, clean, dry, no music", 0.8),
    "whoosh-wide": ("a card, cover or logo sliding in", "Cinematic wide swoosh sweeping across, clean, dry, no music", 1.2),
    "pop": ("a word, sticker or text popping in", "Short bubble pop, clean UI sound, no music", 0.5),
    "click": ("a hard cut or a UI tap", "A single sharp mouse button click, short dry tick sound effect", 0.5),
    "shutter": ("a screenshot or picture appearing", "Camera shutter click, single, clean, dry", 0.6),
    "riser": ("tension before a reveal", "Short rising tension riser building up, clean synth sweep, no music", 2.0),
    "impact": ("a big statement or the end of a riser", "Deep cinematic impact hit with a short tail, bass boom, no music", 1.5),
    "ding": ("a number or fact landing", "Bright pleasant notification chime, single ding, clean", 1.0),
}


def probe(path: Path) -> dict:
    d = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout
    v = subprocess.run(["ffmpeg", "-v", "info", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True).stderr
    peak = re.search(r"max_volume: (-?[\d.]+) dB", v)
    mean = re.search(r"mean_volume: (-?[\d.]+) dB", v)
    return {"seconds": round(float(d or 0), 2), "peak_db": float(peak.group(1)) if peak else None, "mean_db": float(mean.group(1)) if mean else None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    key = os.environ.get("ELEVENLABS_API_KEY", "")
    manifest_path = OUT / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {"tool": "elevenlabs sound-generation", "sounds": {}}
    want = [x for x in a.only.split(",") if x] or list(SOUNDS)
    OUT.mkdir(parents=True, exist_ok=True)
    for sid in want:
        use, prompt, seconds = SOUNDS[sid]
        dest = OUT / f"{sid}.mp3"
        if dest.is_file() and not a.force:
            print(f"{sid}: kept")
        else:
            if not key:
                print("set ELEVENLABS_API_KEY", file=sys.stderr)
                return 2
            r = requests.post(URL, headers={"xi-api-key": key, "Content-Type": "application/json"},
                              json={"text": prompt, "duration_seconds": seconds, "prompt_influence": 0.5}, timeout=120)
            if r.status_code != 200 or not r.content:
                print(f"{sid}: failed ({r.status_code})", file=sys.stderr)                      # the answer may echo account details: not printed
                return 1
            dest.write_bytes(r.content)
            print(f"{sid}: made")
        manifest["sounds"][sid] = {"file": dest.name, "use": use, "prompt": prompt, "requested_seconds": seconds, "generated": str(date.today()),
                                   "sha256": hashlib.sha256(dest.read_bytes()).hexdigest(), **probe(dest)}
    manifest_path.write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
