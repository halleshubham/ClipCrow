"""Fill in each clip's framing (face-tracked crop, blurred fit, or plain cover) by looking at the video."""
from __future__ import annotations

from .edl import EDL, FitSpan, Keyframe, SplitPoint
from .reframe import FaceDetector, plan_framing


def apply_framing(edl: EDL, video: str, *, detector: FaceDetector | None = None, say=lambda _m: None,
                  multi: str = "switch", graphics: bool = False) -> EDL:
    """A new EDL whose clips carry ``layout.framing`` / ``layout.reframe``. A clip whose layout already
    asks for a framing other than ``cover`` is left alone (a person's choice wins over the analysis)."""
    clips = []
    for c in edl.clips:
        if c.layout.framing != "cover" or c.layout.reframe:
            clips.append(c)
            continue
        f = plan_framing(video, c.source_in, c.source_out, detector=detector, multi=multi, graphics=graphics)
        say(f"{c.id}: {f.framing}" + (f" (face in {f.coverage:.0%} of samples)" if f.framing != "cover" else "")
            + (f", {f.people} people" if f.people > 1 else "")
            + (f", {len(f.fit_spans)} graphic(s) shown whole ({sum(b - a for a, b in f.fit_spans):.0f} s)" if f.fit_spans else ""))
        layout = c.layout.model_copy(update={
            "framing": f.framing, "reframe": [Keyframe(t=t, cx=cx) for t, cx in f.keyframes],
            "split": [SplitPoint(cx=cx, cy=cy) for cx, cy in f.split],
            "fit_spans": [FitSpan(t0=a, t1=b) for a, b in f.fit_spans]})
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
