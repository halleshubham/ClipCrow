import numpy as np
import pytest

from clipcrow import asr, asr_sravaani as sv
from clipcrow.energy import SAMPLE_RATE, energy_db, speech_threshold


def tone_audio(spans, total):
    """16 kHz audio that is loud only inside ``spans`` (seconds)."""
    x = np.zeros(int(total * SAMPLE_RATE), dtype=np.float32)
    rng = np.random.default_rng(1)
    for a, b in spans:
        i, j = round(a * SAMPLE_RATE), round(b * SAMPLE_RATE)
        x[i:j] = rng.normal(0, 0.3, j - i)
    return x


def test_windows_are_cut_in_a_quiet_place_and_never_longer_than_the_limit():
    spans = [(0, 22), (23.0, 50), (51, 80)]
    audio = tone_audio(spans, 80)
    cuts = sv.split_points(energy_db(audio), 80)
    assert cuts[0] == 0 and cuts[-1] == 80
    assert all(b - a <= sv.WINDOW_SECONDS + 0.01 for a, b in zip(cuts, cuts[1:]))
    assert any(22 <= c <= 23 for c in cuts) and any(50 <= c <= 51 for c in cuts)


def test_a_short_recording_is_one_window():
    assert sv.split_points(energy_db(tone_audio([(0, 10)], 10)), 10) == [0.0, 10]


def test_tighten_ends_a_word_where_the_voice_stops_and_keeps_order():
    audio = tone_audio([(1.0, 1.4), (2.5, 3.0)], 4)
    db = energy_db(audio)
    out = sv.tighten([{"w": "a", "s": 0.8, "e": 2.4}, {"w": "b", "s": 2.4, "e": 4.0}], db, speech_threshold(db))
    assert out[0]["s"] == pytest.approx(0.98, abs=0.05) and out[0]["e"] == pytest.approx(1.4, abs=0.05)
    assert out[1]["s"] == pytest.approx(2.48, abs=0.05) and out[1]["e"] == pytest.approx(3.0, abs=0.05)
    assert out[0]["e"] <= out[1]["s"]


def test_a_word_is_never_longer_than_max_word():
    audio = tone_audio([(0, 10)], 10)
    db = energy_db(audio)
    out = sv.tighten([{"w": "a", "s": 0.0, "e": 9.0}], db, speech_threshold(db))
    assert out[0]["e"] - out[0]["s"] <= sv.MAX_WORD + 1e-6


class Hyp:
    def __init__(self, words):
        self.timestamp = {"word": words}


class FakeModel:
    def transcribe(self, paths, return_hypotheses=True, timestamps=True):
        assert timestamps and len(paths) == 1
        return [Hyp([{"word": "नमस्कार", "start": 0.5, "end": 1.0}, {"word": "  ", "start": 1.0, "end": 1.1},
                     {"word": "मित्रांनो", "start": 1.0, "end": 1.6}])]


def test_words_get_the_window_offset_and_blank_words_are_dropped():
    audio = tone_audio([(0.4, 1.7), (30.4, 31.7)], 40)
    words = sv.transcribe_words("x", model=FakeModel(), audio_loader=lambda m: audio)
    assert [w["w"] for w in words] == ["नमस्कार", "मित्रांनो"] * 2
    assert words[2]["s"] > 18 and all(a["e"] <= b["s"] + 1e-9 for a, b in zip(words, words[1:]))


def test_it_is_off_unless_asked_and_a_failure_falls_back_to_whisper(monkeypatch):
    monkeypatch.delenv("CLIPCROW_ASR", raising=False)
    assert not sv.enabled()
    monkeypatch.setenv("CLIPCROW_ASR", "SraVaani")
    assert sv.enabled()
    monkeypatch.setattr(sv, "transcribe_words", lambda *a, **k: (_ for _ in ()).throw(ImportError("no transformers")))

    class W:
        def transcribe(self, media, **kw):
            seg = type("S", (), {"text": "नमस्कार", "words": [type("Wd", (), {"word": " नमस्कार", "start": 0.0, "end": 0.5})()]})()
            return [seg], type("I", (), {"language": "mr"})()
    said = []
    tr = asr.transcribe("m.wav", "mr", model_loader=lambda cfg, size: W(), audio_loader=lambda m: np.zeros(SAMPLE_RATE * 2, np.float32),
                        windowed=False, progress=said.append)
    assert tr.words[0].w == "नमस्कार" and any("SraVaani unavailable" in m for m in said)


def test_sravaani_output_is_used_when_it_works(monkeypatch):
    monkeypatch.setenv("CLIPCROW_ASR", "sravaani")
    monkeypatch.setattr(sv, "transcribe_words", lambda *a, **k: [{"w": "माहितीये", "s": 0.0, "e": 0.5}, {"w": "आहे", "s": 0.5, "e": 0.8}])
    tr = asr.transcribe("m.wav", "mr", model_loader=lambda cfg, size: object())
    assert [w.w for w in tr.words] == ["माहितीये", "आहे"] and tr.language == "mr"
