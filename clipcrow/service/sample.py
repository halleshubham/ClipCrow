"""The sample job a new account starts with: a finished job (clips, covers, the plan) that the operator exported from a real one, copied into the new
person's account so the first screen after signing in shows what ClipCrow makes without spending minutes or GPU time.

``export_sample`` (``clipcrow export-sample JOB_ID --to DIR``) writes ``job.json`` and ``out/`` into a folder; ``import_sample`` copies that folder into a new
job. The folder is the operator's own, but nothing in it is trusted blindly: ``job.json`` is validated as a ``Job``, only plain files with plain names are
copied (no links, no folders), and the total size is capped. No uploaded original is exported, so the sample cannot be re-cut (``source_removed``).
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import uuid
from pathlib import Path
from typing import Optional

from .jobs import Job, JobStore, Params

log = logging.getLogger("clipcrow.service")

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
SKIP = {"posts.json", "gpu_usage.json", "clean.mp4", "speakers.json", "retakes.json"}      # who posted what and what it cost are not part of a sample
MAX_BYTES = 300 * 1024 * 1024


def _plain_files(folder: Path):
    for f in sorted(folder.iterdir()):
        if f.is_symlink() or not f.is_file() or not NAME.match(f.name) or f.name in SKIP:
            continue
        yield f


def export_sample(store: JobStore, job_id: str, dest: str | Path) -> int:
    """Write a finished job into ``dest`` as a sample. Returns how many files were copied."""
    job = store.get(job_id)
    if job.status != "done" or not job.clips:
        raise ValueError("only a finished job with clips can be a sample")
    dest = Path(dest)
    (dest / "out").mkdir(parents=True, exist_ok=True)
    n, total = 0, 0
    for f in _plain_files(store.dir(job_id) / "out"):
        total += f.stat().st_size
        if total > MAX_BYTES:
            raise ValueError("the sample is larger than %d MB" % (MAX_BYTES // 2 ** 20))
        shutil.copyfile(f, dest / "out" / f.name)
        n += 1
    clean = job.model_copy(update={"owner": None, "remote": {}, "log": job.log[-12:], "reserved_minutes": 0, "source_pending": None,
                                   "source_sha": None, "source_removed": True, "sample": True, "attempts": 0, "cancel_requested": False})
    (dest / "job.json").write_text(clean.model_dump_json(indent=1), encoding="utf-8")
    return n


def import_sample(store: JobStore, sample_dir: str | Path, owner: str) -> Optional[Job]:
    """Copy the sample into a new job owned by ``owner``. None when there is no sample or it is not valid (logged, never raised: signing in must not fail)."""
    root = Path(sample_dir)
    try:
        template = Job.model_validate(json.loads((root / "job.json").read_text(encoding="utf-8")))
        files = list(_plain_files(root / "out")) if (root / "out").is_dir() else []
        if not template.clips or sum(f.stat().st_size for f in files) > MAX_BYTES:
            raise ValueError("the sample has no clips or is too large")
    except (OSError, ValueError) as e:
        log.warning("sample job not added: %s", e)
        return None
    job = store.create(("Sample: " + template.name)[:200], template.params or Params(), job_id=uuid.uuid4().hex[:12])
    out = store.dir(job.id) / "out"
    for f in files:
        shutil.copyfile(f, out / f.name)

    def fill(j: Job) -> None:
        for field in ("status", "stages", "clips", "left_out", "source", "demo", "log"):
            setattr(j, field, getattr(template, field))
        j.status, j.owner, j.sample, j.source_removed, j.reserved_minutes, j.remote = "done", owner, True, True, 0, {}
        j.log = ["This is a sample job: the clips are made from a real recording so you can see what ClipCrow does.", *j.log][-40:]
    return store.update(job.id, fill)
