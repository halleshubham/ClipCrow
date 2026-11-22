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


def _same(a: str, b: str) -> bool:
    try:
        return json.loads(a) == json.loads(b)
    except ValueError:
        return a == b


def migrate(data_dir: str | Path, docs: DocStore) -> dict:
    """Put every file record into the database. Returns ``{kind: copied}``; ``unchanged`` counts what was already there."""
    report: dict = {"unchanged": 0}
    for kind, owner, key, text in collect(data_dir):
        have = docs.text(kind, owner, key)
        if have is not None and _same(have, text):
            report["unchanged"] += 1
            continue
        docs.put(kind, owner, key, text)
        report[kind] = report.get(kind, 0) + 1
    return report


def verify(data_dir: str | Path, docs: DocStore) -> dict:
    """Compare files and database without changing either. ``{"files": n, "same": n, "different": [...], "only_in_files": [...], "only_in_db": [...]}`` with kind/key
    names only (never contents). Empty lists mean they agree."""
    seen: set[tuple[str, str, str]] = set()
    out: dict = {"files": 0, "same": 0, "different": [], "only_in_files": [], "only_in_db": []}
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


KINDS = ("operator_settings", "schedule", "style", "brand_registry", "brand_settings", "brand_overlay")
