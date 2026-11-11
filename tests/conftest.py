import json
import shutil
from pathlib import Path

import pytest

from clipcrow.edl import EDL

ROOT = Path(__file__).resolve().parent.parent
GSAP = ROOT / "vendor" / "gsap.min.js"


def base_edl(**over) -> dict:
    d = {
        "job_id": "t",
        "source": {"uri": "src.mp4", "duration": 20, "width": 1080, "height": 1920, "fps": 30},
        "language": {"primary": "mr", "script": "Deva"},
        "transcript": [
            {"w": "क्षणात", "s": 1.0, "e": 1.5}, {"w": "बदलणारे", "s": 1.5, "e": 2.0},
            {"w": "आयुष्य.", "s": 2.0, "e": 2.6}, {"w": "ज्ञान", "s": 3.5, "e": 4.0},
            {"w": "शक्ती", "s": 4.0, "e": 4.5},
        ],
        "clips": [{"id": "c1", "source_in": 0, "source_out": 10}],
    }
    d.update(over)
    return d


@pytest.fixture
def edl_dict():
    return base_edl()


@pytest.fixture
def make_edl():
    return lambda **over: EDL.model_validate(base_edl(**over))


@pytest.fixture
def gsap(tmp_path):
    """A stand-in gsap file so HTML-only compiles do not need the vendored copy."""
    p = tmp_path / "gsap.min.js"
    p.write_text("/* test */")
    return p


@pytest.fixture
def fonts_available():
    from clipcrow import fonts
    try:
        fonts.resolve_font("Noto Serif Devanagari", 700)
        fonts.resolve_font("Noto Serif", 700)
    except fonts.FontNotFound:
        pytest.skip("Noto Serif Devanagari / Noto Serif are not installed (fonts-noto-core)")
