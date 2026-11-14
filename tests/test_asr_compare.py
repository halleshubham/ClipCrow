from types import SimpleNamespace as NS

import numpy as np

from clipcrow import asr, asr_compare, indic
from clipcrow.cli import main
from clipcrow.planner import FakeLLM
from clipcrow.textfix import Fixed, Fixes


class Model:
    """Whole-file calls (a path) say one thing, windowed calls (an array) say another."""
    def detect_language(self, samples):
        return ("mr", 0.9)

    def transcribe(self, media, **kw):
        if isinstance(media, str):
            return iter([NS(text="x", words=[NS(word=" नहीं", start=1.0, end=2.0), NS(word=" आहे", start=17.0, end=18.0)])]), NS(language="mr")
        return iter([NS(text="x", words=[NS(word=" नाही", start=1.0, end=2.0)])]), NS(language="mr")


def setup(monkeypatch):
    monkeypatch.setattr(indic, "media_duration", lambda p: 30.0)
    monkeypatch.setattr(indic, "audio_window", lambda p, off, secs=30: np.zeros(16000, dtype=np.float32))
    monkeypatch.setattr(asr, "load_audio", lambda media: np.zeros(20 * 16000, dtype=np.float32))


def run(monkeypatch, **kw):
    setup(monkeypatch)
    return asr_compare.compare("x.mp4", 60, 90, "mr", config=asr.AsrConfig(model="small", indic_model=None),
                               model_loader=lambda cfg, size: Model(), cutter=lambda m, a, b, d: d, **kw)


def test_both_settings_run_on_the_same_stretch_and_are_grouped_by_15_seconds(monkeypatch):
    res = run(monkeypatch)
    assert res["old"]["text"] == ["नहीं", "आहे"] and res["new"]["text"][0].startswith("नाही")
    assert res["old"]["words"] == 2 and res["new"]["words"] >= 1 and "refined" not in res
    out = asr_compare.render(res)
    assert "old" in out and "new" in out and "[60-75 s]" in out and "नहीं" in out and "नाही" in out


def test_refine_adds_a_third_row(monkeypatch):
    llm = FakeLLM(lambda p, prompt, schema: Fixes(items=[Fixed(id=0, text="नाही ठीक")]))
    res = run(monkeypatch, llm=llm)
    assert res["refined"]["changed"] >= 0 and "refined" in asr_compare.render(res)


def test_a_backwards_range_is_refused():
    import pytest
    with pytest.raises(ValueError):
        asr_compare.compare("x.mp4", 50, 10, "mr")


def test_the_command_prints_and_writes_json(monkeypatch, tmp_path, capsys):
    setup(monkeypatch)
    monkeypatch.setattr(asr, "_load", lambda cfg, size: Model())
    monkeypatch.setattr(asr_compare, "cut", lambda m, a, b, d: d)
    out = tmp_path / "c.json"
    assert main(["compare-asr", "x.mp4", "--from", "0", "--to", "30", "--out", str(out)]) == 0
    assert "old" in capsys.readouterr().out and out.is_file()
