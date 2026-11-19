"""Fill in each clip's framing (face-tracked crop, blurred fit, or plain cover) by looking at the video."""
from __future__ import annotations

from .edl import EDL, FitSpan, Keyframe, SplitPoint
from .reframe import FaceDetector, plan_framing


def _plan_all(todo, video, detector, multi, graphics, speakers) -> dict:
    """``{clip id: (Framing, seconds it took)}``. With the default detector the clips are analysed in parallel (OpenCV releases the GIL while it decodes
    and detects; each call makes its own detector), at most ``CLIPCROW_FRAMING_THREADS`` (default: the CPU count, at most 4) at once. A detector passed in
    (tests, a worker) is used one clip after the other, so its results do not depend on timing."""
    import os
    import time
    from concurrent.futures import ThreadPoolExecutor

    def one(c):
        t0 = time.monotonic()
        f = plan_framing(video, c.source_in, c.source_out, detector=detector, multi=multi, graphics=graphics, speakers=speakers)
        return c.id, (f, time.monotonic() - t0)
    try:
        cap = int(os.environ.get("CLIPCROW_FRAMING_THREADS", "") or min(4, os.cpu_count() or 2))
    except ValueError:
        cap = 2
    workers = 1 if detector is not None else max(1, min(cap, len(todo)))
    if workers == 1:
        return dict(one(c) for c in todo)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="clipcrow-frame") as pool:
        return dict(pool.map(one, todo))


def apply_framing(edl: EDL, video: str, *, detector: FaceDetector | None = None, say=lambda _m: None,
                  multi: str = "switch", graphics: bool = False, speakers=None) -> EDL:
    """A new EDL whose clips carry ``layout.framing`` / ``layout.reframe``. A clip whose layout already
    asks for a framing other than ``cover`` is left alone (a person's choice wins over the analysis)."""
    todo = [c for c in edl.clips if not (c.layout.framing != "cover" or c.layout.reframe)]
    results = _plan_all(todo, video, detector, multi, graphics, speakers)
    clips = []
    for c in edl.clips:
        if c.layout.framing != "cover" or c.layout.reframe:
            clips.append(c)
            continue
        f, took = results[c.id]
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
