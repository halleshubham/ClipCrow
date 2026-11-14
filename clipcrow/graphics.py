"""Find the stretches of a clip where the source shows a slide, a title card or other on-screen graphics instead of the speaker.

A vertical crop of such a frame cuts the text in half, so these stretches are shown whole (``Layout.fit_spans``). Nothing here is learned or
sent to a model: it groups the sampled frames into SHOTS by how they look (a tiny thumbnail), finds the speaker's shot, and calls a shot a graphic
when it is neither the speaker's shot nor another live camera:

  * the speaker's shot is the biggest shot in which a face is found most of the time and that face moves a little (a person is never perfectly still);
  * another live camera is a shot with a moving face in nearly every sample (a second angle of an interview stays tracked);
  * everything else is a graphic: no face (a slide, a chart, b-roll), or a face that does not move at all (a portrait ON a slide).

Spans shorter than ``MIN_SPAN`` are dropped, so a hand wave or a missed face never flickers the picture.
"""
from __future__ import annotations

from dataclasses import dataclass

THUMB = (32, 18)               # thumbnail size (w, h)
SAME_SHOT = 28.0               # mean absolute difference (0-255) under which two thumbnails are the same shot
MIN_SPAN = 1.2                 # seconds: shorter stretches are not worth a cut
MERGE_GAP = 0.6                # seconds: two graphics this close are one
MIN_ANCHOR_SHARE = 0.25        # the speaker's shot must hold at least this share of the samples, or we do not know who is who
FACE_SHARE = 0.5               # a shot has "a face" when it is found in at least this share of its samples
LIVE_CAMERA_SHARE = 0.8        # ... and is another live camera at this share (with a moving face)
STILL = 0.004                  # the face centre's spread (fraction of the frame) under which a face is a picture, not a person
MIN_STILL_SAMPLES = 4          # samples needed before "it does not move" means anything


def thumbnail(frame_bgr):
    """A tiny float colour image of a frame; the only thing the shot grouping looks at."""
    import cv2
    return cv2.resize(frame_bgr, THUMB, interpolation=cv2.INTER_AREA).astype("float32")


def _dist(a, b) -> float:
    return float(abs(a - b).mean())


@dataclass
class _Shot:
    idx: list
    centre: object

    def add(self, i: int, thumb) -> None:
        n = len(self.idx)
        self.centre = (self.centre * n + thumb) / (n + 1)
        self.idx.append(i)


def _shots(thumbs: list) -> list[_Shot]:
    shots: list[_Shot] = []
    for i, th in enumerate(thumbs):
        best = min(shots, key=lambda s: _dist(s.centre, th), default=None)
        if best is not None and _dist(best.centre, th) < SAME_SHOT:
            best.add(i, th)
        else:
            shots.append(_Shot([i], th.copy()))
    return shots


def _face_stats(samples: list, idx: list, src_w: float, src_h: float) -> tuple[float, bool]:
    """(share of samples with a face, does the biggest face move?) for one shot."""
    pts = []
    for i in idx:
        boxes = samples[i].boxes
        if boxes:
            b = max(boxes, key=lambda b: b.w * b.h)
            pts.append((b.cx / src_w, b.cy / src_h))
    share = len(pts) / max(1, len(idx))
    if len(pts) < MIN_STILL_SAMPLES:
        return share, True                  # too few to call it a picture: give the face the benefit of the doubt
    mean = [sum(p[k] for p in pts) / len(pts) for k in (0, 1)]
    spread = max((sum((p[k] - mean[k]) ** 2 for p in pts) / len(pts)) ** 0.5 for k in (0, 1))
    return share, spread >= STILL


def graphic_spans(samples: list, duration: float, src_w: float, src_h: float) -> list[tuple[float, float]]:
    """``(t0, t1)`` clip-time spans where the frame is a graphic. ``samples`` carry ``.t``, ``.boxes`` and ``.thumb``."""
    if len(samples) < 6 or any(getattr(s, "thumb", None) is None for s in samples):
        return []
    shots = _shots([s.thumb for s in samples])
    stats = [_face_stats(samples, sh.idx, src_w, src_h) for sh in shots]
    live_faces = [k for k, (share, moves) in enumerate(stats) if share >= FACE_SHARE and moves]
    if not live_faces:
        return []
    anchor = max(live_faces, key=lambda k: len(shots[k].idx))
    if len(shots[anchor].idx) < MIN_ANCHOR_SHARE * len(samples):
        return []
    live = {anchor} | {k for k in live_faces if stats[k][0] >= LIVE_CAMERA_SHARE}
    is_graphic = [True] * len(samples)
    for k in live:
        for i in shots[k].idx:
            is_graphic[i] = False
    spans: list[list[float]] = []
    i = 0
    while i < len(samples):
        if not is_graphic[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(samples) and is_graphic[j + 1]:
            j += 1
        t0 = 0.0 if i == 0 else (samples[i - 1].t + samples[i].t) / 2          # the cut lies between the last speaker sample and the first graphic
        t1 = duration if j == len(samples) - 1 else (samples[j].t + samples[j + 1].t) / 2
        if spans and t0 - spans[-1][1] < MERGE_GAP:
            spans[-1][1] = t1
        else:
            spans.append([t0, t1])
        i = j + 1
    return [(round(a, 3), round(b, 3)) for a, b in spans if b - a >= MIN_SPAN]
