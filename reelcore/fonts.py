"""Resolve a font family to a file we can ship inside the render project.

HyperFrames' bundled fonts are Latin only; any other family (every Indic script)
must be an @font-face to a file in the project, or the render draws boxes.
Search order: $REELCORE_FONT_DIR, ./fonts, the system Noto directories.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

DEVANAGARI = re.compile("[ऀ-ॿ꣠-ꣿ]")

# family -> {weight: file name}
KNOWN = {
    "Noto Sans Devanagari": {400: "NotoSansDevanagari-Regular.ttf", 700: "NotoSansDevanagari-Bold.ttf"},
    "Noto Sans": {400: "NotoSans-Regular.ttf", 700: "NotoSans-Bold.ttf"},
}
SYSTEM_DIRS = ["/usr/share/fonts/truetype/noto", "/usr/share/fonts/noto", "/usr/local/share/fonts"]
DEVANAGARI_FAMILIES = {"Noto Sans Devanagari"}


class FontNotFound(RuntimeError):
    pass


def _dirs() -> list[Path]:
    dirs = []
    env = os.environ.get("REELCORE_FONT_DIR")
    if env:
        dirs.append(Path(env))
    dirs.append(Path("fonts"))
    dirs.extend(Path(d) for d in SYSTEM_DIRS)
    return dirs


def resolve_font(family: str, weight: int = 700) -> Path:
    files = KNOWN.get(family)
    if not files:
        raise FontNotFound(
            f"unknown font family {family!r}; known: {', '.join(sorted(KNOWN))}. "
            f"Add it to reelcore.fonts.KNOWN and put the file in $REELCORE_FONT_DIR.")
    name = files.get(weight) or files[max(files)]
    for d in _dirs():
        if (d / name).is_file():
            return d / name
    raise FontNotFound(
        f"{name} not found (looked in {', '.join(str(d) for d in _dirs())}). "
        f"Install fonts-noto-core or set REELCORE_FONT_DIR.")


def needs_devanagari(*texts: str) -> bool:
    return any(DEVANAGARI.search(t or "") for t in texts)


def effective_family(family: str, texts: list[str]) -> str:
    """Swap a Latin-only family for the Devanagari one when the text needs it."""
    if needs_devanagari(*texts) and family not in DEVANAGARI_FAMILIES:
        return "Noto Sans Devanagari"
    return family
