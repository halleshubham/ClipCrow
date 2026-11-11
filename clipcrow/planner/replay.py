"""Answer the planner's prompts from files instead of an API: record, edit, replay.

    clipcrow plan t.json ... --answers dir/      # first run: every prompt the planner needs is written to
                                                 # dir/pending/<key>.json; nothing is answered yet
    (write dir/<key>.json for each: the model's JSON answer)
    clipcrow plan t.json ... --answers dir/      # re-run: answered prompts are used, new ones are queued

The key is a hash of the prompt, so an answer is replayed only for the exact prompt it was written
for. Uses: running the planner where no API is reachable (a person or another model answers the
queued prompts), deterministic regression tests on real transcripts, and inspecting exactly what the
planner asks. A missing answer raises LLMError, which the planner already treats as "skip this
window", so a pass with nothing answered ends in a PlanError listing what is pending.
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Type

from ..edl import CallUsage
from .llm import LLMError, T


def prompt_key(purpose: str, user: str) -> str:
    return hashlib.sha256(f"{purpose}\n{user}".encode("utf-8")).hexdigest()[:12]


class ReplayLLM:
    def __init__(self, directory: str | Path, model: str = "replay"):
        self.dir = Path(directory)
        self.pending_dir = self.dir / "pending"
        self.model = model
        self.pending: list[str] = []
        self._calls: list[CallUsage] = []
        self._lock = threading.Lock()

    def usage(self) -> list[CallUsage]:
        return list(self._calls)

    def generate(self, system: str, user: str, schema: Type[T], *, purpose: str = "") -> T:
        key = prompt_key(purpose, user)
        answer = self.dir / f"{key}.json"
        if not answer.is_file():
            self.pending_dir.mkdir(parents=True, exist_ok=True)
            (self.pending_dir / f"{key}.json").write_text(json.dumps(
                {"key": key, "purpose": purpose, "system": system, "prompt": user,
                 "answer_schema": schema.model_json_schema(), "write_answer_to": str(answer)},
                ensure_ascii=False, indent=1), encoding="utf-8")
            with self._lock:
                self.pending.append(key)
            raise LLMError(f"no answer yet for {purpose or 'prompt'} {key}")
        try:
            out = schema.model_validate_json(answer.read_text(encoding="utf-8"))
        except ValueError as e:
            raise LLMError(f"answer {key}.json does not fit the schema: {e}") from e
        with self._lock:
            self._calls.append(CallUsage(purpose=purpose, model=self.model))
        return out
