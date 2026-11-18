#!/usr/bin/env python3
"""Install the font families listed in clipcrow/fonts_catalog.json into a folder, from the @expo-google-fonts packages on npm (SIL Open Font License).

    python3 scripts/install_fonts.py <dest dir>

Each family becomes ``<Name without spaces>-Regular.ttf`` and ``-Bold.ttf`` (the Regular file twice when the family has no bold: the browser then
draws a synthetic bold). Package versions are pinned in the catalogue. Used by the Dockerfile and by CI through scripts/install_fonts.sh.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CATALOG = Path(__file__).resolve().parent.parent / "clipcrow" / "fonts_catalog.json"


def main(dest: str) -> None:
    entries = json.loads(CATALOG.read_text(encoding="utf-8"))
    out = Path(dest)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["npm", "init", "-y"], cwd=tmp, check=True, capture_output=True)
        subprocess.run(["npm", "i", "--silent", "--no-audit", "--no-fund", *[f"@expo-google-fonts/{e['pkg']}@{e['ver']}" for e in entries]], cwd=tmp, check=True)
        for e in entries:
            base = Path(tmp) / "node_modules" / "@expo-google-fonts" / e["pkg"]
            name = re.sub(r"\s+", "", e["name"])
            reg = base / e["reg"]
            bold = base / e["bold"] if e["bold"] else reg
            for src in (reg, bold):
                if not src.is_file():
                    raise SystemExit(f"{e['name']}: {src.name} is not in @expo-google-fonts/{e['pkg']}@{e['ver']}")
            shutil.copyfile(reg, out / f"{name}-Regular.ttf")
            shutil.copyfile(bold, out / f"{name}-Bold.ttf")
    print(f"{len(entries)} families, {len(list(out.glob('*.ttf')))} files in {out}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: install_fonts.py <dest dir>")
    main(sys.argv[1])
