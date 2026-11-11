"""AnthropicLLM against a stub client: no network, no key."""
from types import SimpleNamespace

import pytest

anthropic = pytest.importorskip("anthropic")

from reelcore.planner.llm import AnthropicLLM, LLMError
from reelcore.planner.models import ScoreResult


class Stub:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.kwargs = resp, exc, None
        self.messages = self

    def parse(self, **kw):
        self.kwargs = kw
        if self.exc:
            raise self.exc
        return self.resp


def resp(stop="end_turn", parsed="ok", inp=10, out=5):
    return SimpleNamespace(stop_reason=stop, parsed_output=parsed, usage=SimpleNamespace(input_tokens=inp, output_tokens=out))


def test_success_passes_schema_and_counts_tokens():
    parsed = ScoreResult(windows=[])
    stub = Stub(resp(parsed=parsed))
    llm = AnthropicLLM(client=stub)
    assert llm.generate("sys", "user", ScoreResult) is parsed
    assert stub.kwargs["output_format"] is ScoreResult and stub.kwargs["system"] == "sys"
    assert stub.kwargs["model"] == "claude-opus-5-5"
    assert (llm.input_tokens, llm.output_tokens) == (10, 5)


@pytest.mark.parametrize("stop,msg", [("refusal", "declined"), ("max_tokens", "cut off")])
def test_refusal_and_truncation_raise_llm_error(stop, msg):
    with pytest.raises(LLMError, match=msg):
        AnthropicLLM(client=Stub(resp(stop=stop))).generate("s", "u", ScoreResult)


def test_no_parsed_output_raises():
    with pytest.raises(LLMError, match="no structured"):
        AnthropicLLM(client=Stub(resp(parsed=None))).generate("s", "u", ScoreResult)


def test_api_errors_become_llm_errors():
    import httpx2 as httpx
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    rl = anthropic.RateLimitError("slow", response=httpx.Response(429, request=req), body=None)
    with pytest.raises(LLMError, match="rate limited"):
        AnthropicLLM(client=Stub(exc=rl)).generate("s", "u", ScoreResult)


def test_model_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("REELCORE_MODEL", "claude-sonnet-5-5")
    assert AnthropicLLM(client=Stub()).model == "claude-sonnet-5-5"
    assert AnthropicLLM("claude-haiku-5-5", client=Stub()).model == "claude-haiku-5-5"
