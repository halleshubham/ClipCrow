"""Fill in each clip's framing (face-tracked crop, blurred fit, or plain cover) by looking at the video."""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from .edl import EDL, FitSpan, Keyframe, SplitPoint
from .reframe import FaceDetector, Framing, plan_framing

MAX_PARALLEL = 4          # clips analysed at once (OpenCV decodes and detects without holding the GIL)


def plan_clips(video: str, spans: list[tuple[str, float, float]], *, detector: FaceDetector | None = None, multi: str = "switch",
               graphics: bool = False, speakers=None, workers: Optional[int] = None,
               done: Callable[[str, float], None] = lambda _id, _s: None) -> dict[str, tuple[Framing, float]]:
    """``{clip id: (framing, seconds it took)}`` for ``(id, start, end)`` spans of one video. The clips are independent, so several run at once
    (each with its own detector: OpenCV's cascade is not safe to share); a detector handed in is used one clip at a time."""
    n = 1 if detector is not None else max(1, min(workers or MAX_PARALLEL, os.cpu_count() or 1, len(spans) or 1))

    def one(span):
        cid, a, b = span
        t0 = time.monotonic()
        f = plan_framing(video, a, b, detector=detector, multi=multi, graphics=graphics, speakers=speakers)
        took = time.monotonic() - t0
        done(cid, took)
        return cid, (f, took)
    if n == 1:
        return dict(one(x) for x in spans)
    with ThreadPoolExecutor(max_workers=n) as pool:
        return dict(pool.map(one, spans))


def apply_framing(edl: EDL, video: str, *, detector: FaceDetector | None = None, say=lambda _m: None,
                  multi: str = "switch", graphics: bool = False, speakers=None, framing_fn: Optional[Callable] = None) -> EDL:
    """A new EDL whose clips carry ``layout.framing`` / ``layout.reframe``. A clip whose layout already
    asks for a framing other than ``cover`` is left alone (a person's choice wins over the analysis).
    ``framing_fn(video, spans, multi=, graphics=, speakers=)`` may plan some or all of the clips elsewhere (a worker): it returns
    ``{clip id: (Framing, seconds)}`` for the ones it did; the rest are planned here, several at once."""
    todo = [(c.id, c.source_in, c.source_out) for c in edl.clips if not (c.layout.framing != "cover" or c.layout.reframe)]
    planned: dict[str, tuple[Framing, float]] = {}
    if framing_fn is not None and todo:
        planned = dict(framing_fn(video, todo, multi=multi, graphics=graphics, speakers=speakers) or {})
    rest = [x for x in todo if x[0] not in planned]
    if rest:
        planned.update(plan_clips(video, rest, detector=detector, multi=multi, graphics=graphics, speakers=speakers))
    clips = []
    for c in edl.clips:
        if c.layout.framing != "cover" or c.layout.reframe:
            clips.append(c)
            continue
        f, took = planned[c.id]
        say(f"{c.id}: {f.framing}" + (f" ({'a voice is heard' if speakers is not None and f.people else 'face'} in {f.coverage:.0%} of samples)" if f.framing != "cover" else "")
            + (f", {f.people} people" if f.people > 1 else "")
            + (f", {len(f.fit_spans)} graphic(s) shown whole ({sum(b - a for a, b in f.fit_spans):.0f} s)" if f.fit_spans else "")
            + f" [{took:.0f} s]")
        layout = c.layout.model_copy(update={
            "framing": f.framing, "reframe": [Keyframe(t=t, cx=cx) for t, cx in f.keyframes],
            "split": [SplitPoint(cx=cx, cy=cy) for cx, cy in f.split],
            "fit_spans": [FitSpan(t0=a, t1=b) for a, b in f.fit_spans],
            "face_zone": f.face_zone if f.framing == "track" else None})
        if f.fit_spans and layout.punch_ins:       # a push-in must not run over a slide that is shown whole
            layout = layout.model_copy(update={"punch_ins": [
                p for p in layout.punch_ins if not any(p.t < b and a < p.t + p.duration for a, b in f.fit_spans)]})
        if f.framing in ("split", "fit_blur") and layout.punch_ins:   # a push-in on stacked halves or a blurred fit looks wrong
            layout = layout.model_copy(update={"punch_ins": []})
        update = {"layout": layout}
        if f.framing == "split":                         # the captions belong on the seam between the two people
            seam = edl.render.height // 2
            update["captions"] = c.captions.model_copy(update={"top_px": max(0, seam - 90)})
        clips.append(c.model_copy(update=update))
    return edl.model_copy(update={"clips": clips})
