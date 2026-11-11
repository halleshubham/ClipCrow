"""Face-tracked 9:16 reframing: where should the crop window sit, second by second?

    samples = sample_faces(video, start, end, detector)      # faces at ~5 fps
    subject = pick_subject(samples, src_w, src_h)             # one continuous track, false positives rejected
    path    = smooth_path(subject, ...)                       # "heavy tripod": dead-band + speed limit
    -> Framing("track", keyframes)  or  Framing("fit_blur") when there is nobody to follow

The keyframes land in the EDL (``layout.reframe``); ``compile.prepare_footage`` turns them into an FFmpeg
crop that moves over time. Design follows the tripod idea of OpenShorts' reframing (the camera should not
chase every small head movement) but is a clean rewrite with no YOLO / MediaPipe dependency: faces come from
OpenCV's bundled Haar cascade, behind a one-method ``FaceDetector`` so a better model (YuNet, ...) can be
dropped in.

Known limits: one main subject per clip (the track with the most detections); no speaker switching and no
two-speaker split layout yet; Haar needs reasonably frontal, well-lit faces.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol

OUT_ASPECT = 9 / 16
SAMPLE_RATE = 5.0          # detections per second
KEY_RATE = 10.0            # camera keyframes per second
MIN_COVERAGE = 0.30        # fraction of samples with a subject needed to follow it
DEADBAND = 0.15            # the camera ignores the subject within this many crop-widths of centre
MAX_SPEED = 0.60           # crop-widths per second the camera may travel
TRACK_RADIUS = 0.12        # a detection joins a track if within this fraction of the frame of its last position


@dataclass(frozen=True)
class Box:
    x: float
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


@dataclass
class Sample:
    t: float                       # seconds from the start of the clip
    boxes: list[Box] = field(default_factory=list)


@dataclass
class Framing:
    framing: str                   # cover | track | fit_blur
    keyframes: list[tuple[float, float]] = field(default_factory=list)   # (clip time, cx as fraction of source width)
    coverage: float = 0.0


class FaceDetector(Protocol):
    def detect(self, frame_bgr) -> list[Box]: ...


class HaarDetector:
    """OpenCV's frontal-face Haar cascade (ships with opencv-python, no download)."""

    def __init__(self, max_width: int = 960, min_face_frac: float = 0.04):
        self.max_width = max_width
        self.min_face_frac = min_face_frac
        self._cascade = None

    def _load(self):
        if self._cascade is None:
            import os

            import cv2
            path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
            self._cascade = cv2.CascadeClassifier(path)
            if self._cascade.empty():
                raise RuntimeError(f"could not load the Haar cascade at {path}")
        return self._cascade

    def detect(self, frame_bgr) -> list[Box]:
        import cv2
        h, w = frame_bgr.shape[:2]
        scale = min(1.0, self.max_width / w)
        small = cv2.resize(frame_bgr, (int(w * scale), int(h * scale))) if scale < 1 else frame_bgr
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        min_px = max(24, int(small.shape[1] * self.min_face_frac))
        found = self._load().detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(min_px, min_px))
        return [Box(x / scale, y / scale, fw / scale, fh / scale) for x, y, fw, fh in found]


# --- sampling ---------------------------------------------------------------------------

def probe_size(video: str) -> tuple[int, int]:
    import cv2
    cap = cv2.VideoCapture(str(video))
    try:
        w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    finally:
        cap.release()
    if not w or not h:
        raise ValueError(f"cannot read the video size of {video}")
    return w, h


def sample_faces(video: str, start: float, end: float, detector: FaceDetector, *,
                 rate: float = SAMPLE_RATE) -> list[Sample]:
    import cv2
    cap = cv2.VideoCapture(str(video))
    out: list[Sample] = []
    try:
        n = max(1, int((end - start) * rate))
        for k in range(n + 1):
            t = min(k / rate, end - start)
            cap.set(cv2.CAP_PROP_POS_MSEC, (start + t) * 1000)
            ok, frame = cap.read()
            if not ok:
                continue
            out.append(Sample(t, detector.detect(frame)))
    finally:
        cap.release()
    return out


# --- choosing and smoothing the subject ---------------------------------------------------

def pick_subject(samples: list[Sample], src_w: float, src_h: float) -> list[tuple[float, Optional[float]]]:
    """One continuous track of the main face: ``[(t, cx_fraction | None)]``.

    Detections are chained into tracks by proximity; the track with the most detections is the
    subject, so a stray false positive (which shows up in a few samples at changing places) loses.
    """
    tracks: list[dict] = []
    for s in samples:
        taken: set[int] = set()
        for b in sorted(s.boxes, key=lambda b: b.w * b.h, reverse=True):
            best, best_d = None, None
            for i, tr in enumerate(tracks):
                if i in taken:
                    continue
                d = max(abs(b.cx - tr["cx"]) / src_w, abs(b.cy - tr["cy"]) / src_h)
                if d <= TRACK_RADIUS and (best_d is None or d < best_d):
                    best, best_d = i, d
            if best is None:
                tracks.append({"cx": b.cx, "cy": b.cy, "hits": 0, "area": 0.0, "pts": {}})
                best = len(tracks) - 1
            tr = tracks[best]
            tr.update(cx=b.cx, cy=b.cy)
            tr["hits"] += 1
            tr["area"] += b.w * b.h
            tr["pts"][s.t] = b.cx / src_w
            taken.add(best)
    if not tracks:
        return [(s.t, None) for s in samples]
    main = max(tracks, key=lambda tr: (tr["hits"], tr["area"]))
    return [(s.t, main["pts"].get(s.t)) for s in samples]


def smooth_path(subject: list[tuple[float, Optional[float]]], duration: float, crop_frac: float, *,
                deadband: float = DEADBAND, max_speed: float = MAX_SPEED,
                rate: float = KEY_RATE) -> list[tuple[float, float]]:
    """A camera that follows the subject lazily: it holds still while the subject stays within a dead
    band around the centre, and when it does move it is speed limited, so a head turn never whips the frame.

    ``crop_frac`` is the crop window's width as a fraction of the source width. Returns
    ``[(t, camera_cx)]`` with the camera kept inside the frame."""
    known = [(t, cx) for t, cx in subject if cx is not None]
    if not known:
        return []
    lo, hi = crop_frac / 2, 1 - crop_frac / 2

    def target(t: float) -> float:              # linear between detections, held outside them
        if t <= known[0][0]:
            return known[0][1]
        for (t0, c0), (t1, c1) in zip(known, known[1:]):
            if t <= t1:
                return c0 if t1 == t0 else c0 + (c1 - c0) * (t - t0) / (t1 - t0)
        return known[-1][1]

    dt = 1 / rate
    cam = min(max(target(0.0), lo), hi)
    dead = deadband * crop_frac
    step = max_speed * crop_frac * dt
    out = [(0.0, cam)]
    n = int(duration * rate)
    for k in range(1, n + 1):
        t = k * dt
        err = target(t) - cam
        if abs(err) > dead:
            move = min(abs(err) - dead, step)
            cam += move if err > 0 else -move
        cam = min(max(cam, lo), hi)
        if abs(cam - out[-1][1]) > 0.0015 or k == n:      # keep the list sparse while the camera is still
            out.append((round(t, 3), cam))
    return out


def plan_framing(video: str, start: float, end: float, *, detector: FaceDetector | None = None,
                 out_aspect: float = OUT_ASPECT, min_coverage: float = MIN_COVERAGE) -> Framing:
    src_w, src_h = probe_size(video)
    if src_w / src_h <= out_aspect * 1.05:               # already vertical (or nearly): a centre crop is right
        return Framing("cover")
    samples = sample_faces(video, start, end, detector or HaarDetector())
    subject = pick_subject(samples, src_w, src_h)
    coverage = sum(1 for _, c in subject if c is not None) / max(1, len(subject))
    if coverage < min_coverage:
        return Framing("fit_blur", coverage=coverage)    # no face to follow: show the whole frame
    crop_frac = (src_h * out_aspect) / src_w
    path = smooth_path(subject, end - start, crop_frac)
    return Framing("track", [(round(t, 3), round(cx, 4)) for t, cx in path], coverage)
