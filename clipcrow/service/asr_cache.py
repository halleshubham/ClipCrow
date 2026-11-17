"""Do not listen to the same recording twice.

A job that is given a file an earlier job of the SAME account already transcribed (same bytes, same language setting) starts from that
transcript instead of running the recogniser again. Everything after the transcript (the text correction, the cleanup, the planning, the
framing, the render) runs as it always does.

What is saved is the recogniser's own output, as ``out/asr.json`` of every job: not the corrected, cleaned or hand-edited transcript.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import Path
from typing import Callable, Optional

from ..planner.transcript import Transcript, parse_transcript
from .jobs import Job, JobStore, now

ASR_FILE = "asr.json"
VERSION = 1


def file_sha256(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def find_reusable(store: JobStore, job: Job, sha: str, language: Optional[str]) -> Optional[tuple[Job, dict]]:
    """The newest earlier job of the same owner whose saved recogniser output is for these bytes and this language setting."""
    for other in store.list():
        if other.id == job.id or other.owner != job.owner or other.source_sha != sha:
            continue
        try:
            meta = json.loads((store.dir(other.id) / "out" / ASR_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError):
            continue
        if meta.get("version") == VERSION and meta.get("sha") == sha and meta.get("language_param", "") == (language or "") and meta.get("words"):
            return other, meta
    return None


class CachedTranscriber:
    """Wraps a transcribe function (the server's own, or the one that asks a worker): reuse a saved transcript of the same file, else transcribe and save."""

    def __init__(self, inner: Callable, store: JobStore, job: Job, out_dir: Path, *, fresh: bool = False):
        self.inner, self.store, self.job, self.out_dir, self.fresh = inner, store, job, Path(out_dir), fresh

    def __call__(self, media, language=None, progress=None, check=None) -> Transcript:
        say = progress or (lambda _m: None)
        sha = self.job.source_sha
        if sha and not self.fresh:
            hit = find_reusable(self.store, self.job, sha, language)
            if hit is not None:
                other, meta = hit
                try:
                    tr = parse_transcript({"language": meta.get("language") or language, "words": meta["words"]})
                except Exception:                                          # a damaged file is not a reason to fail: listen again
                    tr = None
                if tr is not None:
                    tr.engine = str(meta.get("engine") or "")
                    when = str(meta.get("created") or "")[:10]
                    say(f"this recording was transcribed before ({when}{', ' + tr.engine if tr.engine else ''}): using that transcript instead of listening again")
                    self._save(tr, sha, language, reused_from=other.id, created=meta.get("created"))
                    return tr
        kw = {"check": check} if check is not None and "check" in inspect.signature(self.inner).parameters else {}
        tr = self.inner(media, language, progress=say, **kw)
        if not tr.engine:
            tr.engine = "sravaani" if os.environ.get("CLIPCROW_ASR", "").strip().lower() == "sravaani" and tr.language in ("mr", "hi") else "whisper"
        if sha:
            self._save(tr, sha, language)
        return tr

    def _save(self, tr: Transcript, sha: str, language: Optional[str], *, reused_from: str = "", created: Optional[str] = None) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        data = {"version": VERSION, "sha": sha, "language_param": language or "", "language": tr.language, "engine": tr.engine, "created": created or now(),
                "reused_from": reused_from, "words": [{"w": w.w, "s": round(float(w.s), 3), "e": round(float(w.e), 3)} for w in tr.words]}
        tmp = self.out_dir / (ASR_FILE + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.out_dir / ASR_FILE)
