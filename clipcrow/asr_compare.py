"""Run the two speech-recognition settings on the same stretch of a video and print them side by side.

    clipcrow compare-asr talk.mp4 --from 60 --to 180 --language mr --model large-v3 [--refine] [--out compare.json]

"old" is the whole-file pass with a script prompt, hotwords and a voice-activity filter; "new" is the 15 s windowed pass with no prompt (what a
Marathi / Hindi job uses now). Each line is a 15 s slice of the audio. With ``--refine`` the new text is also corrected by the model.
"""
from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

from . import asr
from .planner.transcript import Transcript

SLICE = 15.0


def cut(media: str, start: float, end: float, dest: Path) -> Path:
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(start), "-i", str(media), "-t", str(end - start), "-vn", "-ar", "16000",
                        "-ac", "1", str(dest)], capture_output=True, text=True)
    if r.returncode != 0 or not dest.is_file():
        raise RuntimeError("could not cut that stretch of the media")
    return dest


def slices(tr: Transcript, offset: float, total: float) -> list[str]:
    """The words of ``tr`` grouped by the 15 s slice their start falls in (times shifted back by ``offset`` into the source)."""
    n = max(1, int(total // SLICE) + (1 if total % SLICE else 0))
    buckets: list[list[str]] = [[] for _ in range(n)]
    for w in tr.words:
        buckets[min(int(w.s // SLICE), n - 1)].append(w.w)
    return [" ".join(b) for b in buckets]


def compare(media: str, start: float, end: float, language: str, *, config: asr.AsrConfig | None = None, model_loader: Callable | None = None,
            llm=None, say: Callable[[str], None] = lambda _m: None, cutter: Callable | None = None) -> dict:
    if end <= start:
        raise ValueError("--to must be after --from")
    cfg = config or asr.AsrConfig()
    with tempfile.TemporaryDirectory() as d:
        wav = (cutter or cut)(media, start, end, Path(d) / "region.wav")        # looked up at call time so tests can replace it
        runs: dict[str, tuple[Transcript, float]] = {}
        for name, windowed in (("old", False), ("new", True)):
            say(f"{name}: decoding {int(end - start)} s")
            t0 = time.time()
            tr = asr.transcribe(str(wav), language, config=cfg, model_loader=model_loader, windowed=windowed, progress=say)
            runs[name] = (tr, round(time.time() - t0, 1))
    result = {"from": start, "to": end, "language": language, "model": cfg.indic_model or cfg.model,
              "old": {"seconds": runs["old"][1], "words": len(runs["old"][0].words), "text": slices(runs["old"][0], start, end - start)},
              "new": {"seconds": runs["new"][1], "words": len(runs["new"][0].words), "text": slices(runs["new"][0], start, end - start)}}
    if llm is not None:
        from .textfix import refine
        fixed, n = refine(runs["new"][0], llm, say=say)
        result["refined"] = {"changed": n, "text": slices(fixed, start, end - start)}
    return result


def render(result: dict) -> str:
    keys = [k for k in ("old", "new", "refined") if k in result]
    lines = [f"{result['language']} {result['from']:.0f}-{result['to']:.0f} s, model {result['model']}"]
    for k in keys:
        r = result[k]
        info = f"{r['seconds']} s, {r['words']} words" if "seconds" in r else f"{r['changed']} sentence(s) changed"
        lines.append(f"  {k}: {info}")
    for i in range(len(result["new"]["text"])):
        a = result["from"] + i * SLICE
        lines.append(f"\n[{a:.0f}-{a + SLICE:.0f} s]")
        for k in keys:
            lines.append(f"  {k:8} {result[k]['text'][i]}")
    return "\n".join(lines)
