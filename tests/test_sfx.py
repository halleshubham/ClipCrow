"""The sound-effect library (generated once with ElevenLabs by scripts/make_sfx.py): every file is there, matches its provenance row, and is audible."""
import hashlib
import json
from pathlib import Path

import pytest

SFX = Path(__file__).resolve().parent.parent / "clipcrow" / "sfx"
M = json.loads((SFX / "manifest.json").read_text(encoding="utf-8"))


def test_every_sound_has_a_file_and_a_provenance_row():
    assert M["tool"] and len(M["sounds"]) >= 8
    files = {p.name for p in SFX.glob("*.mp3")}
    assert files == {v["file"] for v in M["sounds"].values()}
    for sid, v in M["sounds"].items():
        assert (SFX / v["file"]).is_file() and v["prompt"] and v["use"] and v["generated"] and sid == v["file"][:-4]


@pytest.mark.parametrize("sid", sorted(M["sounds"]))
def test_a_sound_is_the_file_that_was_recorded_short_and_not_silent(sid):
    v = M["sounds"][sid]
    assert hashlib.sha256((SFX / v["file"]).read_bytes()).hexdigest() == v["sha256"]      # nobody swapped it without regenerating the row
    assert 0.3 <= v["seconds"] <= 3.0                                                     # a cue under speech, never a track
    assert v["peak_db"] is not None and v["peak_db"] > -20                                # a silent generation is a failed one (the first "click" was)


def test_the_library_is_shipped_with_the_package():
    assert '"sfx/*"' in (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
