"""Spending fewer tokens: a model per purpose, stored answers, correcting only doubtful sentences, and the operator's usage table."""
import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from clipcrow.edl import CallUsage
from clipcrow.planner import CachingLLM, FakeLLM, parse_transcript
from clipcrow.planner.llm import AnthropicLLM, clear_cache, purpose_models
from clipcrow.service.aiusage import summarize
from clipcrow.service.jobs import JobStore, Params
from clipcrow import textfix


class Ans(BaseModel):
    text: str


class FakeClient:
    """Stands in for the SDK client: records the model of every call."""
    def __init__(self):
        self.models = []
        self.messages = SimpleNamespace(parse=self.parse)

    def parse(self, *, model, max_tokens, system, messages, output_format):
        self.models.append(model)
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=100, output_tokens=10), stop_reason="end_turn",
                               parsed_output=output_format(text=messages[0]["content"]))


@pytest.fixture(autouse=True)
def no_model_env(monkeypatch):
    for k in ("CLIPCROW_MODEL", "CLIPCROW_MODEL_SCORE", "CLIPCROW_MODEL_REFINE", "CLIPCROW_MODEL_VERIFY", "CLIPCROW_MODEL_JUDGE", "CLIPCROW_REFINE_DOUBTFUL"):
        monkeypatch.delenv(k, raising=False)


def test_a_cheaper_model_only_for_the_purposes_that_name_one(monkeypatch):
    monkeypatch.setenv("CLIPCROW_MODEL_SCORE", "claude-haiku-5-5")
    c = FakeClient()
    llm = AnthropicLLM(client=c)
    llm.generate("s", "a", Ans, purpose="score")
    llm.generate("s", "b", Ans, purpose="detail:3")
    assert c.models == ["claude-haiku-5-5", llm.model]
    assert [u.model for u in llm.usage()] == ["claude-haiku-5-5", llm.model]
    assert llm.last_call().purpose == "detail:3"


def test_no_setting_changes_nothing_and_a_bad_name_is_refused(monkeypatch):
    assert purpose_models({}) == {}
    with pytest.raises(ValueError):
        purpose_models({"CLIPCROW_MODEL_SCORE": "x; rm -rf"})
    assert purpose_models({"CLIPCROW_MODEL_REFINE": "claude-sonnet-5-5"}) == {"text-refine": "claude-sonnet-5-5"}


def test_the_same_question_is_paid_for_once(tmp_path):
    c = FakeClient()
    llm = CachingLLM(AnthropicLLM(client=c), tmp_path / "llm_cache")
    a = llm.generate("s", "q1", Ans, purpose="score")
    b = llm.generate("s", "q1", Ans, purpose="score")
    assert a == b and len(c.models) == 1 and len(llm.usage()) == 1           # the second call was not billed
    llm.generate("s", "q2", Ans, purpose="score")                            # another prompt asks again
    llm.generate("s", "q1", Ans, purpose="detail:1")                         # another purpose asks again
    assert len(c.models) == 3
    stats = json.loads((tmp_path / "llm_cache" / "_stats.json").read_text())
    assert stats["score"] == {"hits": 1, "input_tokens": 100, "output_tokens": 10}


def test_another_model_asks_again_and_a_damaged_entry_is_ignored(tmp_path, monkeypatch):
    c = FakeClient()
    CachingLLM(AnthropicLLM(client=c), tmp_path).generate("s", "q", Ans, purpose="score")
    monkeypatch.setenv("CLIPCROW_MODEL_SCORE", "claude-haiku-5-5")
    CachingLLM(AnthropicLLM(client=c), tmp_path).generate("s", "q", Ans, purpose="score")
    assert len(c.models) == 2
    for f in tmp_path.glob("*.json"):
        if not f.name.startswith("_"):
            f.write_text("{not json")
    CachingLLM(AnthropicLLM(client=c), tmp_path).generate("s", "q", Ans, purpose="score")
    assert len(c.models) == 3


def test_a_failed_answer_is_not_kept_and_starting_over_clears(tmp_path):
    def respond(purpose, prompt, schema):
        return RuntimeError("boom") if prompt == "bad" else {"text": prompt}
    llm = CachingLLM(FakeLLM(respond), tmp_path / "c")
    with pytest.raises(RuntimeError):
        llm.generate("s", "bad", Ans, purpose="x")
    llm.generate("s", "ok", Ans, purpose="x")
    assert len(list((tmp_path / "c").glob("*.json"))) == 1
    clear_cache(tmp_path / "c")
    assert not (tmp_path / "c").exists()


def _tr(sentences, language="mr"):
    words, t = [], 0.0
    for sent in sentences:
        for w, conf in sent:
            words.append({"w": w, "s": t, "e": t + 0.5, **({"conf": conf} if conf is not None else {})})
            t += 0.5
        t += 2.0
    return parse_transcript({"language": language, "words": words})


def test_only_sentences_the_recogniser_doubted_are_sent(monkeypatch):
    monkeypatch.setenv("CLIPCROW_REFINE_DOUBTFUL", "1")
    tr = _tr([[("मी", 0.95), ("घरी", 0.97), ("गेलो", 0.9)],          # sure
              [("तो", 0.95), ("काल", 0.3), ("आला", 0.9)],            # one unsure word
              [("मी", 0.99), ("नहीं", 0.99), ("गेलो", 0.99)]])       # Hindi in Marathi
    assert [s.id for s in textfix.doubtful(tr)] == [1, 2]
    calls = []
    textfix.refine(tr, FakeLLM(lambda p, u, s: calls.append(u) or {"items": []}))
    assert len(calls) == 1 and "0:" not in calls[0] and "1:" in calls[0] and "2:" in calls[0]


def test_by_default_everything_is_sent_and_without_confidences_too(monkeypatch):
    sure0 = _tr([[("मी", 0.99), ("घरी", 0.99)], [("तो", 0.99), ("आला", 0.99)]])
    assert len(textfix.doubtful(sure0)) == 2                                  # the switch is off: nothing is skipped
    monkeypatch.setenv("CLIPCROW_REFINE_DOUBTFUL", "1")
    tr = _tr([[("मी", None), ("घरी", None)], [("तो", None), ("आला", None)]])
    assert len(textfix.doubtful(tr)) == 2
    sure = _tr([[("मी", 0.99), ("घरी", 0.99)], [("तो", 0.99), ("आला", 0.99)]])
    assert textfix.doubtful(sure) == []
    monkeypatch.delenv("CLIPCROW_REFINE_DOUBTFUL")
    assert len(textfix.doubtful(sure)) == 2


def _job(store, calls, cached=None):
    j = store.create("t", Params())
    out = store.dir(j.id) / "out"
    (out / "edl.json").write_text(json.dumps({"ai": {"calls": calls}}))
    if cached:
        (out / "llm_cache").mkdir()
        (out / "llm_cache" / "_stats.json").write_text(json.dumps(cached))
    return j


def test_the_operator_table_groups_calls_by_kind_and_model(tmp_path):
    store = JobStore(tmp_path)
    c = lambda p, m, i, o: CallUsage(purpose=p, model=m, input_tokens=i, output_tokens=o).model_dump()    # noqa: E731
    _job(store, [c("score", "claude-haiku-5-5", 1000, 100), c("score", "claude-haiku-5-5", 1000, 100), c("detail:1", "claude-opus-5-5", 2000, 500), c("detail:2", "claude-opus-5-5", 2000, 500)],
         {"score": {"hits": 2, "input_tokens": 3000, "output_tokens": 300}})
    _job(store, [])                                                    # no plan, nothing cached: not counted
    r = summarize(store, 30)
    assert r["jobs"] == 1
    by = {(x["purpose"], x["model"]): x for x in r["rows"]}
    assert by[("score", "claude-haiku-5-5")]["calls"] == 2 and by[("score", "claude-haiku-5-5")]["input_tokens"] == 2000
    assert by[("detail", "claude-opus-5-5")]["calls"] == 2
    assert r["total_usd"] == pytest.approx(sum(x["est_usd"] for x in r["rows"]))
    assert r["cache"]["hits"] == 2 and r["cache"]["est_usd"] > 0
    assert summarize(store, 0)["days"] == 1
