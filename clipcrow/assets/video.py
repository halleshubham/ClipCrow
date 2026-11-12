"""AI video behind the same kind of interface, with NO provider shipped: ``get_video_provider`` returns ``None`` unless
one is registered, so the planner never asks for generated video it cannot make. A provider is a class with
``name``, ``model`` and ``generate(prompt, dest, *, seconds) -> Path``; register it with ``register``.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Protocol


class VideoProvider(Protocol):
    name: str
    model: str

    def generate(self, prompt: str, dest: Path, *, seconds: float = 5.0) -> Path: ...


_REGISTRY: dict[str, Callable[[], VideoProvider]] = {}


def register(name: str, factory: Callable[[], VideoProvider]) -> None:
    _REGISTRY[name] = factory


def get_video_provider() -> VideoProvider | None:
    name = os.environ.get("CLIPCROW_VIDEO_PROVIDER", "")
    return _REGISTRY[name]() if name in _REGISTRY else None
