"""Generate the built-in background tracks with the ElevenLabs music API and write ``clipcrow/tracks/manifest.json``.

    ELEVENLABS_API_KEY=... python scripts/make_tracks.py [--force] [--only id,id]

Existing files are kept (each generation costs credits and gives a different piece); ``--force`` makes them again. The key is read from the environment and never
printed or stored. Every track has a provenance row: title, mood, the prompt, the date, the tool, a hash, its length and loudness. Instrumental only, a quiet bed
meant to sit under a voice (the render lowers it further and ducks it while someone speaks).

LICENCE: ElevenLabs says what its music tool generates may be used commercially on a paid plan; the free plan does not include that. Run this on a paid plan
and keep the invoice with the date in the manifest's ``generated`` field. No third party's music is shipped.
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

OUT = Path(__file__).resolve().parent.parent / "clipcrow" / "tracks"
URL = "https://api.elevenlabs.io/v1/music?output_format=mp3_44100_128"
TRACKS = {
    "calm-keys": ("Calm keys", "calm, thoughtful",
                  "Calm minimal lo-fi instrumental, soft piano and warm pads, steady low energy, no vocals, no drum build-ups, a quiet bed under a spoken voice", 90_000),
    "bright-pulse": ("Bright pulse", "light, upbeat",
                     "Light upbeat modern instrumental, gentle muted percussion and a soft plucked synth, steady and unobtrusive, no vocals, no big drops or build-ups, "
                     "a friendly bed under a spoken voice", 90_000),
}


def stats(path: Path) -> dict:
    d = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout
    v = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "loudnorm=I=-14:print_format=json", "-f", "null", "-"], capture_output=True, text=True).stderr
    m = re.findall(r"\{[^{}]*\}", v)
    lufs = None
    if m:
        try:
            lufs = float(json.loads(m[-1])["input_i"])
        except (ValueError, KeyError):
            lufs = None
    return {"seconds": round(float(d or 0), 1), "lufs": lufs}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    key = os.environ.get("ELEVENLABS_API_KEY", "")
    mp = OUT / "manifest.json"
    manifest = json.loads(mp.read_text(encoding="utf-8")) if mp.is_file() else {"tool": "elevenlabs music (music_v1, instrumental)", "tracks": {}}
    OUT.mkdir(parents=True, exist_ok=True)
    for tid in [x for x in a.only.split(",") if x] or list(TRACKS):
        title, mood, prompt, ms = TRACKS[tid]
        dest = OUT / f"{tid}.mp3"
        if dest.is_file() and not a.force:
            print(f"{tid}: kept")
        else:
            if not key:
                print("set ELEVENLABS_API_KEY", file=sys.stderr)
                return 2
            r = requests.post(URL, headers={"xi-api-key": key, "Content-Type": "application/json"},
                              json={"prompt": prompt, "music_length_ms": ms, "force_instrumental": True, "model_id": "music_v1"}, timeout=600)
            if r.status_code != 200 or not r.content:
                print(f"{tid}: failed ({r.status_code})", file=sys.stderr)
                return 1
            dest.write_bytes(r.content)
            print(f"{tid}: made")
        manifest["tracks"][tid] = {"file": dest.name, "title": title, "mood": mood, "prompt": prompt, "generated": str(date.today()),
                                   "sha256": hashlib.sha256(dest.read_bytes()).hexdigest(), **stats(dest)}
    mp.write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
