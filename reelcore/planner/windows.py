"""Sentence-aligned scoring windows.

A window must be able to CONTAIN a clip of the maximum length (so the detail pass can
pick a clip inside it), and consecutive windows overlap so a good moment that straddles
a boundary is still whole in one of them.
"""
from __future__ import annotations

from dataclasses import dataclass

from .transcript import Sentence


@dataclass
class Window:
    id: int
    first: int      # first sentence id
    last: int       # last sentence id (inclusive)
    start: float
    end: float

    def sentences(self, all_sentences: list[Sentence]) -> list[Sentence]:
        return all_sentences[self.first:self.last + 1]


def build_windows(sentences: list[Sentence], *, window_seconds: float, stride_ratio: float = 0.6) -> list[Window]:
    if not sentences:
        return []
    out: list[Window] = []
    i = 0
    while i < len(sentences):
        j = i
        while j + 1 < len(sentences) and sentences[j + 1].end - sentences[i].start <= window_seconds:
            j += 1
        out.append(Window(len(out), i, j, sentences[i].start, sentences[j].end))
        if j >= len(sentences) - 1:
            break
        # advance to the first sentence that starts after stride_ratio of this window
        cut = sentences[i].start + (sentences[j].end - sentences[i].start) * stride_ratio
        nxt = next((k for k in range(i + 1, j + 1) if sentences[k].start >= cut), j)
        i = max(nxt, i + 1)
    return out
