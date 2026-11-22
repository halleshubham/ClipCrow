"""Copy the records that live in files into the database, and check the two agree: ``clipcrow migrate-stores`` (and ``--verify``).

Read-only on the files. Safe to run twice: a record that is already there with the same content is left alone. The report has counts only, never a value or an
address. Each phase of the move adds its own collector below; ``collect`` is the one list of what a file-backed record is.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterator

from .docstore import DocStore
from .jobrows import JobRows

OWNER = re.compile(r"[a-f0-9]{32}\Z")
HEX12 = re.compile(r"[a-f0-9]{12}\Z")


def _read(p: Path) -> str | None:
    try:
        return p.read_text("utf-8")
    except OSError:
        return None


def collect(data_dir: str | Path) -> Iterator[tuple[str, str, str, str]]:
    """Every record kept in a file, as ``(kind, owner, key, text)``."""
    root = Path(data_dir)
    t = _read(root / "settings.json")
    if t is not None:
        yield "operator_settings", "", "current", t
    for p in sorted((root / "schedule").glob("*.json")) if (root / "schedule").is_dir() else []:
        text = _read(p)
        if text is not None and (OWNER.match(p.stem) or p.stem == "shared"):
            yield "schedule", p.stem, "prefs", text
    for od in sorted((root / "styles").iterdir()) if (root / "styles").is_dir() else []:
        if OWNER.match(od.name):
            for p in sorted(od.glob("*.json")):
                text = _read(p)
                if text is not None and HEX12.match(p.stem):
                    yield "style", od.name, p.stem, text
    for od in sorted((root / "brand").iterdir()) if (root / "brand").is_dir() else []:
        if not OWNER.match(od.name):
            continue
        text = _read(od / "brands.json")
        if text is not None:
            yield "brand_registry", od.name, "registry", text
        for bd in sorted(od.iterdir()):
            if bd.is_dir() and HEX12.match(bd.name):
                for kind, name in (("brand_settings", "settings.json"), ("brand_overlay", "overlay.json")):
                    text = _read(bd / name)
                    if text is not None:
                        yield kind, od.name, bd.name, text
    for p in sorted((root / "uploads").glob("*.json")) if (root / "uploads").is_dir() else []:
        text = _read(p)
        if text is not None and HEX12.match(p.stem):
            yield "upload", "", p.stem, text
    for jd in sorted((root / "jobs").iterdir()) if (root / "jobs").is_dir() else []:
        if HEX12.match(jd.name):
            for kind, name in (("job_posts", "posts.json"), ("job_gpu", "gpu_usage.json")):
                text = _read(jd / "out" / name)
                if text is not None:
                    yield kind, "", jd.name, text


def collect_jobs(data_dir: str | Path) -> Iterator[tuple[str, str]]:
    """``(job id, job.json text)`` of every valid job folder; the lists inside the folder come from ``collect``."""
    from .jobs import Job
    root = Path(data_dir) / "jobs"
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        text = _read(d / "job.json") if HEX12.match(d.name) else None
        if text is None:
            continue
        try:
            Job.model_validate_json(text)
        except ValueError:
            continue                                          # a damaged record is not copied (the verify report counts it as only in files)
        yield d.name, text


def _same(a: str, b: str) -> bool:
    try:
        return json.loads(a) == json.loads(b)
    except ValueError:
        return a == b


def migrate(data_dir: str | Path, docs: DocStore, rows: JobRows | None = None) -> dict:
    """Put every file record into the database. Returns ``{kind: copied}``; ``unchanged`` counts what was already there."""
    report: dict = {"unchanged": 0}
    if rows is not None:
        from .jobs import Job
        for job_id, text in collect_jobs(data_dir):
            have = rows.get(job_id)
            if have is not None and _same(have[0], text):
                report["unchanged"] += 1
                continue
            j = Job.model_validate_json(text)
            rows.put(job_id, j.owner, j.status, j.created_at, text)
            report["job"] = report.get("job", 0) + 1
    for kind, owner, key, text in collect(data_dir):
        have = docs.text(kind, owner, key)
        if have is not None and _same(have, text):
            report["unchanged"] += 1
            continue
        docs.put(kind, owner, key, text)
        report[kind] = report.get(kind, 0) + 1
    return report


def verify(data_dir: str | Path, docs: DocStore, rows: JobRows | None = None) -> dict:
    """Compare files and database without changing either. ``{"files": n, "same": n, "different": [...], "only_in_files": [...], "only_in_db": [...]}`` with kind/key
    names only (never contents). Empty lists mean they agree."""
    seen: set[tuple[str, str, str]] = set()
    out: dict = {"files": 0, "same": 0, "different": [], "only_in_files": [], "only_in_db": []}
    if rows is not None:
        have_ids = set(rows.ids())
        file_ids = set()
        for job_id, text in collect_jobs(data_dir):
            out["files"] += 1
            file_ids.add(job_id)
            got = rows.get(job_id)
            if got is None:
                out["only_in_files"].append(f"job/{job_id}")
            elif _same(got[0], text):
                out["same"] += 1
            else:
                out["different"].append(f"job/{job_id}")
        out["only_in_db"] += [f"job/{i}" for i in sorted(have_ids - file_ids)]
    for kind, owner, key, text in collect(data_dir):
        out["files"] += 1
        seen.add((kind, owner, key))
        have = docs.text(kind, owner, key)
        if have is None:
            out["only_in_files"].append(f"{kind}/{key}")
        elif _same(have, text):
            out["same"] += 1
        else:
            out["different"].append(f"{kind}/{key}")
    for kind in KINDS:
        for owner in docs.owners(kind):
            for key, _ in docs.items(kind, owner):
                if (kind, owner, key) not in seen:
                    out["only_in_db"].append(f"{kind}/{key}")
    return out


KINDS = ("operator_settings", "schedule", "style", "brand_registry", "brand_settings", "brand_overlay", "job_posts", "job_gpu", "upload")
