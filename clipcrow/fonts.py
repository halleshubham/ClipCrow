"""Resolve a font family to a file we can ship inside the render project.

HyperFrames' bundled fonts are Latin only; any other family (every Indic script)
must be an @font-face to a file in the project, or the render draws boxes.
Search order: $CLIPCROW_FONT_DIR, ./fonts, the system Noto directories.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

DEVANAGARI = re.compile("[ऀ-ॿ꣠-ꣿ]")

# The families ClipCrow can draw: every Devanagari family on Google Fonts and the best-known Latin ones (``fonts_catalog.json``, made from the
# @expo-google-fonts packages; scripts/install_fonts.py installs them as ``<Name without spaces>-Regular.ttf`` / ``-Bold.ttf``, the Regular file
# standing in for Bold when the family has no bold). All licensed under the SIL Open Font License (see NOTICE).
CATALOG = json.loads((Path(__file__).parent / "fonts_catalog.json").read_text(encoding="utf-8"))


def stem(name: str) -> str:
    return re.sub(r"\s+", "", name)


# family -> {weight: file name}
KNOWN = {c["name"]: {400: f"{stem(c['name'])}-Regular.ttf", 700: f"{stem(c['name'])}-Bold.ttf"} for c in CATALOG}
DEFAULT_DEVANAGARI = "Noto Serif Devanagari"
# The Devanagari faces cover Devanagari, digits and punctuation but NOT Latin letters, so words like
# "WhatsApp" or the x in "10x" need a companion face, shipped with the project and named in the font stack.
LATIN_COMPANION = {"Noto Serif Devanagari": "Noto Serif", "Noto Sans Devanagari": "Noto Sans"}
LATIN = re.compile("[A-Za-z]")
SYSTEM_DIRS = ["/usr/share/fonts/truetype/noto", "/usr/share/fonts/noto", "/usr/local/share/fonts"]
DEVANAGARI_FAMILIES = {c["name"] for c in CATALOG if c["deva"]}      # families whose own letters cover Devanagari (some cover Latin too: Poppins, Hind, Mukta ...)


class FontNotFound(RuntimeError):
    pass


def installed() -> list[dict]:
    """The catalogue entries whose files are on this machine, as ``{"name", "deva", "latin"}`` (what a person may choose here)."""
    out = []
    for c in CATALOG:
        try:
            resolve_font(c["name"], 400)
        except FontNotFound:
            continue
        out.append({"name": c["name"], "deva": c["deva"], "latin": c["latin"]})
    return out


def _dirs() -> list[Path]:
    dirs = []
    env = os.environ.get("CLIPCROW_FONT_DIR")
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
            f"Add it to clipcrow.fonts.KNOWN and put the file in $CLIPCROW_FONT_DIR.")
    name = files.get(weight) or files[max(files)]
    for d in _dirs():
        if (d / name).is_file():
            return d / name
    raise FontNotFound(
        f"{name} not found (looked in {', '.join(str(d) for d in _dirs())}). "
        f"Install fonts-noto-core or set CLIPCROW_FONT_DIR.")


def needs_devanagari(*texts: str) -> bool:
    return any(DEVANAGARI.search(t or "") for t in texts)


def effective_family(family: str, texts: list[str]) -> str:
    """Swap a Latin-only family for the Devanagari one when the text needs it."""
    if needs_devanagari(*texts) and family not in DEVANAGARI_FAMILIES:
        return DEFAULT_DEVANAGARI
    return family


def needs_latin(*texts: str) -> bool:
    return any(LATIN.search(t or "") for t in texts)


def companion(family: str, *texts: str) -> str | None:
    """The Latin companion to ship with ``family`` for these texts, or None when it is not needed."""
    name = LATIN_COMPANION.get(family)
    return name if name and needs_latin(*texts) else None
