"""LLM access for the planner: a one-method protocol, the Claude client, and a test double.

Every call returns a validated pydantic object; refusals and truncation raise LLMError so the
planner can skip one window instead of failing the whole plan.
"""
from __future__ import annotations

import os
import threading
from typing import Callable, Protocol, Type, TypeVar

from pydantic import BaseModel

from ..edl import CallUsage

T = TypeVar("T", bound=BaseModel)
DEFAULT_MODEL = "claude-opus-5-5"


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    def generate(self, system: str, user: str, schema: Type[T], *, purpose: str = "") -> T: ...

    def usage(self) -> list[CallUsage]:
        """Every call made so far, with tokens as billed."""


class AnthropicLLM:
    """Claude via the official SDK, structured output through ``messages.parse``.

    Auth comes from the environment (ANTHROPIC_API_KEY or an `ant auth login` profile).
    The model is ``$CLIPCROW_MODEL`` or claude-opus-5-5; pass ``model=`` per instance to use a
    cheaper one for the scoring pass.
    """

    def __init__(self, model: str | None = None, *, max_tokens: int = 8000, client=None):
        import anthropic  # imported lazily: the planner's tests need no SDK or key
        self._anthropic = anthropic
        self.client = client or anthropic.Anthropic()
        self.model = model or os.environ.get("CLIPCROW_MODEL") or DEFAULT_MODEL
        self.max_tokens = max_tokens
        self._calls: list[CallUsage] = []
        self._lock = threading.Lock()

    @property
    def input_tokens(self) -> int:
        return sum(c.input_tokens for c in self._calls)

    @property
    def output_tokens(self) -> int:
        return sum(c.output_tokens for c in self._calls)

    def usage(self) -> list[CallUsage]:
        with self._lock:
            return list(self._calls)

    def generate(self, system, user, schema, *, purpose=""):
        a = self._anthropic
        try:
            resp = self.client.messages.parse(
                model=self.model, max_tokens=self.max_tokens, system=system,
                messages=[{"role": "user", "content": user}], output_format=schema)
        except a.RateLimitError as e:
            raise LLMError(f"rate limited ({purpose})") from e
        except a.APIStatusError as e:
            raise LLMError(f"Claude API error {e.status_code} ({purpose}): {e.message}") from e
        except a.APIConnectionError as e:
            raise LLMError(f"could not reach the Claude API ({purpose})") from e
        with self._lock:      # billed whether or not the answer is usable
            self._calls.append(CallUsage(purpose=purpose, model=self.model,
                                         input_tokens=resp.usage.input_tokens,
                                         output_tokens=resp.usage.output_tokens))
        if resp.stop_reason == "refusal":
            raise LLMError(f"the model declined this request ({purpose})")
        if resp.stop_reason == "max_tokens":
            raise LLMError(f"the answer was cut off at max_tokens ({purpose})")
        if resp.parsed_output is None:
            raise LLMError(f"no structured answer ({purpose})")
        return resp.parsed_output


class FakeLLM:
    """Scripted answers for tests: ``respond(purpose, user_prompt, schema) -> BaseModel | dict``."""

    def __init__(self, respond: Callable):
        self.respond = respond
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def usage(self):
        return [CallUsage(purpose=p, model="fake") for p, _ in self.calls]

    def generate(self, system, user, schema, *, purpose=""):
        with self._lock:
            self.calls.append((purpose, user))
        out = self.respond(purpose, user, schema)
        if isinstance(out, Exception):
            raise out
        return out if isinstance(out, schema) else schema.model_validate(out)
