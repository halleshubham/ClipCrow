"""LLM access for the planner: a one-method protocol, the Claude client, and a test double.

Every call returns a validated pydantic object; refusals and truncation raise LLMError so the
planner can skip one window instead of failing the whole plan.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Callable, Optional, Protocol, Type, TypeVar, Union

from pydantic import BaseModel

from ..edl import CallUsage

T = TypeVar("T", bound=BaseModel)
DEFAULT_MODEL = "claude-opus-5-5"


class LLMError(RuntimeError):
    pass


# A cheaper model for the easy work: ``CLIPCROW_MODEL_SCORE`` (window scoring), ``_REFINE`` (Marathi/Hindi text correction), ``_VERIFY`` (the final read-through
# of a cleanup), ``_JUDGE`` (which takes to cut). Unset means the default model, so nothing changes until an operator sets one. The creative calls (titles, hooks,
# picture beats) always use the main model.
PURPOSE_ENV = {"score": "SCORE", "text-refine": "REFINE", "retake-verify": "VERIFY", "retake-judge": "JUDGE"}
MODEL_NAME = re.compile(r"[a-z0-9][a-z0-9.\-]{0,60}(\[1m\])?\Z")


def purpose_models(env=None) -> dict[str, str]:
    env = os.environ if env is None else env
    out: dict[str, str] = {}
    for purpose, key in PURPOSE_ENV.items():
        name = (env.get(f"CLIPCROW_MODEL_{key}") or "").strip()
        if name:
            if not MODEL_NAME.match(name):
                raise ValueError(f"CLIPCROW_MODEL_{key} is not a model name")
            out[purpose] = name
    return out


Content = Union[str, list]      # a list is Anthropic content blocks (the style reader sends images)


class LLM(Protocol):
    def generate(self, system: str, user: Content, schema: Type[T], *, purpose: str = "") -> T: ...

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
        self.by_purpose = purpose_models()
        self._calls: list[CallUsage] = []
        self._lock = threading.Lock()
        self._local = threading.local()

    def model_for(self, purpose: str) -> str:
        return self.by_purpose.get(purpose.partition(":")[0], self.model)

    def last_call(self) -> Optional[CallUsage]:
        """The call this thread made last (what a cache needs to know about what an answer cost)."""
        return getattr(self._local, "last", None)

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
        model = self.model_for(purpose)
        try:
            resp = self.client.messages.parse(
                model=model, max_tokens=self.max_tokens, system=system,
                messages=[{"role": "user", "content": user}], output_format=schema)
        except a.RateLimitError as e:
            raise LLMError(f"rate limited ({purpose})") from e
        except a.APIStatusError as e:
            raise LLMError(f"Claude API error {e.status_code} ({purpose}): {e.message}") from e
        except a.APIConnectionError as e:
            raise LLMError(f"could not reach the Claude API ({purpose})") from e
        call = CallUsage(purpose=purpose, model=model, input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens)
        self._local.last = call
        with self._lock:      # billed whether or not the answer is usable
            self._calls.append(call)
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
        self.calls: list[tuple[str, Content]] = []
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


class CachingLLM:
    """Answers kept in the job's folder, so the same question is never paid for twice: a retry, a restart or a "continue from" that asks what an earlier try already
    asked gets the stored answer. The key is the model, the purpose, the schema and the exact prompt (pictures by their bytes), so any change to the recording, the
    options or the model asks again. Only answers that validated are stored; a damaged file is ignored and asked again. Starting a job over clears the folder
    (``clear``), because someone who runs it again wants a new try. ``hits`` and the tokens saved are counted in ``stats.json`` next to the answers."""

    STATS = "_stats.json"

    def __init__(self, inner, directory: str | Path):
        self.inner, self.dir = inner, Path(directory)
        self._lock = threading.Lock()

    def _key(self, system, user, schema, purpose: str) -> str:
        model = self.inner.model_for(purpose) if hasattr(self.inner, "model_for") else getattr(self.inner, "model", "")
        blob = json.dumps([model, purpose, schema.__name__, system, user], ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _count(self, kind: str, call: Optional[CallUsage]) -> None:
        path = self.dir / self.STATS
        with self._lock:
            try:
                d = json.loads(path.read_text()) if path.is_file() else {}
                row = d.setdefault(kind, {"hits": 0, "input_tokens": 0, "output_tokens": 0})
                row["hits"] += 1
                if call:
                    row["input_tokens"] += call.input_tokens
                    row["output_tokens"] += call.output_tokens
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(d))
                os.replace(tmp, path)
            except (OSError, ValueError):
                pass

    def generate(self, system, user, schema, *, purpose=""):
        path = self.dir / f"{self._key(system, user, schema, purpose)}.json"
        try:
            if path.is_file():
                d = json.loads(path.read_text(encoding="utf-8"))
                got = schema.model_validate(d["answer"])
                self._count(purpose.partition(":")[0], CallUsage(purpose=purpose, model=str(d.get("model", "")),
                                                                 input_tokens=int(d.get("in", 0)), output_tokens=int(d.get("out", 0))))
                return got
        except (OSError, ValueError, KeyError, TypeError):
            pass                                                   # a damaged or old entry: ask again
        got = self.inner.generate(system, user, schema, purpose=purpose)
        last = self.inner.last_call() if hasattr(self.inner, "last_call") else None
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"v": 1, "model": last.model if last else "", "in": last.input_tokens if last else 0,
                                       "out": last.output_tokens if last else 0, "answer": got.model_dump(mode="json")}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass                                                   # a full disk costs a repeat call, not the job
        return got

    def usage(self):
        return self.inner.usage()

    def __getattr__(self, name):
        return getattr(self.inner, name)


def clear_cache(directory: str | Path) -> None:
    import shutil
    shutil.rmtree(directory, ignore_errors=True)


class Interruptible:
    """Wraps an LLM so a stop request is noticed WHILE a call is in flight, not after it: each call runs in a helper thread and the caller polls ``check``
    (which raises when the job was cancelled) every ``poll`` seconds. An abandoned call finishes in the background and its answer is dropped (it is still billed
    and still counted in ``usage``). Everything else is passed through."""

    def __init__(self, inner, check: Callable[[], None], poll: float = 0.5):
        self.inner, self.check, self.poll = inner, check, poll

    def generate(self, system, user, schema, *, purpose=""):
        self.check()
        box: dict = {}
        done = threading.Event()

        def run():
            try:
                box["result"] = self.inner.generate(system, user, schema, purpose=purpose)
            except BaseException as e:               # handed back to the caller's thread
                box["error"] = e
            finally:
                done.set()
        threading.Thread(target=run, daemon=True, name=f"llm-{purpose or 'call'}").start()
        while not done.wait(self.poll):
            self.check()
        if "error" in box:
            raise box["error"]
        return box["result"]

    def usage(self):
        return self.inner.usage()

    def __getattr__(self, name):                       # model, input_tokens, ... of the wrapped LLM
        return getattr(self.inner, name)
