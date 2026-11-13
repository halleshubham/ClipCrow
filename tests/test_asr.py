from types import SimpleNamespace as NS

import numpy as np
import pytest

from clipcrow import asr, indic
from clipcrow.planner.transcript import TranscriptError


def W(word, s, e):
    return NS(word=word, start=s, end=e)


def seg(text, *words):
    return NS(text=text, words=list(words))


class FakeModel:
    def __init__(self, segments, language="mr", detect=("mr", 0.9)):
        self.segments, self.language, self.detect = segments, language, detect
        self.transcribe_kwargs = None
        self.detect_calls = 0

    def detect_language(self, samples):
        self.detect_calls += 1
        return self.detect

    def transcribe(self, media, **kw):
        self.transcribe_kwargs = kw
        return iter(self.segments), NS(language=self.language)


def loader(models):
    return lambda cfg, size: models[size]


@pytest.fixture(autouse=True)
def fake_audio(monkeypatch):
    monkeypatch.setattr(indic, "media_duration", lambda p: 100.0)
    monkeypatch.setattr(indic, "audio_window", lambda p, off, secs=30: np.zeros(16000, dtype=np.float32))


# --- indic.py -------------------------------------------------------------------------

def test_vote_takes_the_majority_across_windows():
    answers = iter([("hi", 0.55), ("mr", 0.8), ("mr", 0.7)])

    class M:
        def detect_language(self, s): return next(answers)
    got = indic.vote_language(M(), "x.mp4", duration=lambda p: 300, window=lambda p, o, s=30: np.zeros(16000, np.float32))
    assert got == ("mr", 0.8)


@pytest.mark.parametrize("shape,expected", [(("mr", 0.9), ("mr", 0.9)), ([("hi", 0.7), ("mr", 0.2)], ("hi", 0.7)),
                                            ([["mr", 0.6]], ("mr", 0.6)), ([], None)])
def test_detect_language_answer_shapes_from_different_versions(shape, expected):
    assert indic._parse(shape) == expected


def test_decode_params_seed_indic_and_confident_only():
    ok = lambda lang, p: (lambda m, x: (lang, p))
    assert indic.decode_params(None, "x", vote=ok("mr", 0.4))["initial_prompt"].startswith("नमस्कार")
    assert indic.decode_params(None, "x", vote=ok("de", 0.9)) == {"language": "de"}
    assert indic.decode_params(None, "x", vote=ok("de", 0.3)) == {}
    assert indic.decode_params(None, "x", vote=lambda m, x: (_ for _ in ()).throw(RuntimeError())) == {}


# --- asr.py ---------------------------------------------------------------------------

def test_transcribe_votes_seeds_and_returns_a_transcript():
    m = FakeModel([seg("क्षणात आयुष्य", W(" क्षणात", 0.0, 0.5), W(" आयुष्य।", 0.5, 1.1))])
    tr = asr.transcribe("x.mp4", config=asr.AsrConfig(model="small", indic_model=None), model_loader=loader({"small": m}))
    assert [w.w for w in tr.words] == ["क्षणात", "आयुष्य।"] and tr.language == "mr"
    kw = m.transcribe_kwargs
    assert kw["language"] == "mr" and kw["initial_prompt"].startswith("नमस्कार")
    assert kw["word_timestamps"] and kw["vad_filter"] and kw["condition_on_previous_text"] is False
    assert tr.sentences[0].text == "क्षणात आयुष्य।"


def test_a_pinned_language_skips_the_vote_but_still_gets_the_seed():
    m = FakeModel([seg("x", W(" नमस्ते", 0, 1))], language="hi")
    asr.transcribe("x.mp4", "hi", config=asr.AsrConfig(model="small", indic_model=None), model_loader=loader({"small": m}))
    assert m.detect_calls == 0 and m.transcribe_kwargs["language"] == "hi" and "initial_prompt" in m.transcribe_kwargs


def test_indic_audio_switches_to_the_larger_model_but_english_does_not():
    small = FakeModel([seg("a", W(" a", 0, 1))], detect=("mr", 0.9))
    big = FakeModel([seg("आ", W(" आ", 0, 1))])
    cfg = asr.AsrConfig(model="small", indic_model="large-v3")
    asr.transcribe("x.mp4", config=cfg, model_loader=loader({"small": small, "large-v3": big}))
    assert big.transcribe_kwargs is not None and small.transcribe_kwargs is None

    small2 = FakeModel([seg("a", W(" a", 0, 1))], language="en", detect=("en", 0.95))
    big2 = FakeModel([])
    asr.transcribe("x.mp4", config=cfg, model_loader=loader({"small": small2, "large-v3": big2}))
    assert small2.transcribe_kwargs is not None and big2.transcribe_kwargs is None


def test_fragments_without_a_leading_space_join_the_previous_word():
    words = asr.merge_fragments([{"w": " YouTube", "s": 0, "e": 1}, {"w": "-Kanal", "s": 1, "e": 1.4},
                                 {"w": " ok", "s": 1.5, "e": 2}])
    assert [(w["w"], w["e"]) for w in words] == [("YouTube-Kanal", 1.4), ("ok", 2)]


def test_repetition_loops_are_collapsed():
    same = [seg("Thank you.", W(" Thank", i, i + 0.5)) for i in range(6)]
    kept = asr.collapse_loops([seg("real", W(" real", -1, 0))] + same)
    assert [s.text for s in kept] == ["real", "Thank you.", "Thank you."]      # first two repeats kept, rest dropped


def test_no_speech_is_an_error_not_an_empty_transcript():
    m = FakeModel([])
    with pytest.raises(TranscriptError, match="no speech"):
        asr.transcribe("x.mp4", config=asr.AsrConfig(model="small", indic_model=None), model_loader=loader({"small": m}))


def test_transcribe_command_writes_a_transcript_the_planner_can_load(tmp_path, monkeypatch):
    from clipcrow.cli import main
    from clipcrow.planner import load_transcript
    m = FakeModel([seg("a", W(" क्षणात", 0, 1), W(" आयुष्य।", 1, 2))])
    monkeypatch.setattr(asr, "_load", lambda cfg, size: m)
    out = tmp_path / "t.json"
    assert main(["transcribe", "x.mp4", "--out", str(out)]) == 0
    tr = load_transcript(out)
    assert tr.language == "mr" and len(tr.words) == 2


# --- device selection: use the GPU when there is one -------------------------------------------------------------------------

def test_auto_picks_the_gpu_when_one_is_visible_and_the_cpu_when_not():
    assert asr.resolve_device("auto", "auto", lambda: 1) == ("cuda", "float16")
    assert asr.resolve_device("auto", "auto", lambda: 2) == ("cuda", "float16")
    assert asr.resolve_device("auto", "auto", lambda: 0) == ("cpu", "int8")


def test_a_forced_device_and_compute_type_are_respected():
    assert asr.resolve_device("cpu", "auto", lambda: 1) == ("cpu", "int8")             # forced to the CPU although a GPU exists
    assert asr.resolve_device("cuda", "auto", lambda: 0) == ("cuda", "float16")        # forced: it fails loudly if there is no GPU
    assert asr.resolve_device("cuda", "int8_float16", lambda: 1) == ("cuda", "int8_float16")
    assert asr.resolve_device("auto", "float32", lambda: 0) == ("cpu", "float32")


def test_a_machine_without_ctranslate2_or_a_gpu_just_uses_the_cpu(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "ctranslate2", None)               # import fails
    assert asr._cuda_devices() == 0


def test_the_default_config_is_auto_and_an_empty_env_value_means_auto(monkeypatch):
    monkeypatch.delenv("CLIPCROW_WHISPER_DEVICE", raising=False)
    monkeypatch.delenv("CLIPCROW_WHISPER_COMPUTE", raising=False)
    assert (asr.AsrConfig().device, asr.AsrConfig().compute_type) == ("auto", "auto")
    monkeypatch.setenv("CLIPCROW_WHISPER_DEVICE", "")                                  # compose passes empty strings for unset variables
    monkeypatch.setenv("CLIPCROW_WHISPER_COMPUTE", "")
    assert (asr.AsrConfig().device, asr.AsrConfig().compute_type) == ("auto", "auto")
    monkeypatch.setenv("CLIPCROW_WHISPER_DEVICE", "cpu")
    assert asr.AsrConfig().device == "cpu"


def test_faster_whisper_can_really_decode_audio(tmp_path):
    """An import is not enough: PyAV 19 imports fine and then fails on every file (it dropped metadata_errors)."""
    import subprocess
    pytest = __import__("pytest")
    pytest.importorskip("faster_whisper")
    from faster_whisper.audio import decode_audio
    wav = tmp_path / "tone.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=2", str(wav)], check=True)
    assert len(decode_audio(str(wav), sampling_rate=16000)) == 32000


def test_the_pyproject_keeps_pyav_below_the_version_that_broke_faster_whisper():
    import tomllib
    from pathlib import Path
    deps = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())["project"]["optional-dependencies"]["asr"]
    assert any(d.replace(" ", "").startswith("av") and "<19" in d.replace(" ", "") for d in deps), deps
