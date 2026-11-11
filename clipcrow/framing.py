"""Fill in each clip's framing (face-tracked crop, blurred fit, or plain cover) by looking at the video."""
from __future__ import annotations

from .edl import EDL, Keyframe
from .reframe import FaceDetector, plan_framing


def apply_framing(edl: EDL, video: str, *, detector: FaceDetector | None = None, say=lambda _m: None) -> EDL:
    """A new EDL whose clips carry ``layout.framing`` / ``layout.reframe``. A clip whose layout already
    asks for a framing other than ``cover`` is left alone (a person's choice wins over the analysis)."""
    clips = []
    for c in edl.clips:
        if c.layout.framing != "cover" or c.layout.reframe:
            clips.append(c)
            continue
        f = plan_framing(video, c.source_in, c.source_out, detector=detector)
        say(f"{c.id}: {f.framing}" + (f" (face in {f.coverage:.0%} of samples)" if f.framing != "cover" else ""))
        layout = c.layout.model_copy(update={
            "framing": f.framing, "reframe": [Keyframe(t=t, cx=cx) for t, cx in f.keyframes]})
        clips.append(c.model_copy(update={"layout": layout}))
    return edl.model_copy(update={"clips": clips})
