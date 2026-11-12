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

Several people: ``speaker_tracks`` keeps every persistent face, ``active_speakers`` decides who is talking from
how much each mouth region moves (normalised per person, so a calm speaker is not drowned out by an animated
one) with hysteresis so the picture does not flicker, and ``plan_framing`` then either cuts between speakers
(``multi="switch"``: a hard cut, no pan, because a camera swinging across the room looks wrong) or stacks the
two of them (``multi="split"``). Two people close enough to share one crop window are just framed together.

Known limits: the speaker is whoever's mouth moves, not a voice match (no diarisation model), so a laugh or a
nod can steal a turn for a moment; at most two people are shown at once; Haar needs reasonably frontal faces.
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
    patches: list = field(default_factory=list)   # a small grey mouth-region patch per box (when asked for)


@dataclass
class Framing:
    framing: str                   # cover | track | fit_blur | split
    keyframes: list[tuple[float, float]] = field(default_factory=list)   # (clip time, cx as fraction of source width)
    coverage: float = 0.0
    split: list[tuple[float, float]] = field(default_factory=list)       # two (cx, cy) fractions for "split"
    people: int = 1


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


PATCH = (24, 12)                   # mouth-region patch size (w, h)


def mouth_patch(frame_bgr, box: Box):
    """The lower half of a face box as a tiny grey array: what moves there when somebody talks."""
    import cv2
    h, w = frame_bgr.shape[:2]
    x0, x1 = max(0, int(box.x)), min(w, int(box.x + box.w))
    y0, y1 = max(0, int(box.y + box.h * 0.5)), min(h, int(box.y + box.h))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    grey = cv2.cvtColor(frame_bgr[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    return cv2.resize(grey, PATCH, interpolation=cv2.INTER_AREA).astype("float32")


def sample_faces(video: str, start: float, end: float, detector: FaceDetector, *,
                 rate: float = SAMPLE_RATE, patches: bool = False) -> list[Sample]:
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
            boxes = detector.detect(frame)
            out.append(Sample(t, boxes, [mouth_patch(frame, b) for b in boxes] if patches else []))
    finally:
        cap.release()
    return out


# --- choosing and smoothing the subject ---------------------------------------------------

def _build_tracks(samples: list[Sample], src_w: float, src_h: float) -> list[dict]:
    """Chain detections into tracks by proximity. Each track: hits, area, pts {t: cx fraction},
    boxes {t: Box}, patches {t: mouth patch}."""
    tracks: list[dict] = []
    for s in samples:
        taken: set[int] = set()
        for k, b in sorted(enumerate(s.boxes), key=lambda kb: kb[1].w * kb[1].h, reverse=True):
            best, best_d = None, None
            for i, tr in enumerate(tracks):
                if i in taken:
                    continue
                d = max(abs(b.cx - tr["cx"]) / src_w, abs(b.cy - tr["cy"]) / src_h)
                if d <= TRACK_RADIUS and (best_d is None or d < best_d):
                    best, best_d = i, d
            if best is None:
                tracks.append({"cx": b.cx, "cy": b.cy, "hits": 0, "area": 0.0, "pts": {}, "boxes": {}, "patches": {}})
                best = len(tracks) - 1
            tr = tracks[best]
            tr.update(cx=b.cx, cy=b.cy)
            tr["hits"] += 1
            tr["area"] += b.w * b.h
            tr["pts"][s.t] = b.cx / src_w
            tr["boxes"][s.t] = b
            if k < len(s.patches) and s.patches[k] is not None:
                tr["patches"][s.t] = s.patches[k]
            taken.add(best)
    return tracks


def pick_subject(samples: list[Sample], src_w: float, src_h: float) -> list[tuple[float, Optional[float]]]:
    """One continuous track of the main face: ``[(t, cx_fraction | None)]``.

    Detections are chained into tracks by proximity; the track with the most detections is the
    subject, so a stray false positive (which shows up in a few samples at changing places) loses.
    """
    tracks = _build_tracks(samples, src_w, src_h)
    if not tracks:
        return [(s.t, None) for s in samples]
    main = max(tracks, key=lambda tr: (tr["hits"], tr["area"]))
    return [(s.t, main["pts"].get(s.t)) for s in samples]


# --- several people ------------------------------------------------------------------------

MIN_PERSON = 0.25          # a person must be seen in this fraction of the samples to count (false positives flicker)
MIN_HOLD = 1.5             # seconds a speaker keeps the picture before it may change
SWITCH_RATIO = 1.4         # a challenger must be this much more active than the holder
GROUP_SPAN = 0.7           # two people closer than this many crop-widths share one window


def speaker_tracks(samples: list[Sample], src_w: float, src_h: float) -> list[dict]:
    """The persistent faces, left to right. A track seen in under ``MIN_PERSON`` of the samples is noise."""
    need = max(3, MIN_PERSON * len(samples))
    tracks = [tr for tr in _build_tracks(samples, src_w, src_h) if tr["hits"] >= need]
    return sorted(tracks, key=lambda tr: sum(tr["pts"].values()) / len(tr["pts"]))


def normalise_activity(raw: dict[float, float]) -> dict[float, float]:
    """Scale one person's mouth movement by their own typical movement (the 90th percentile), so a
    soft-spoken person with small mouth movements still counts as talking when they do."""
    if not raw:
        return {}
    vals = sorted(raw.values())
    ref = vals[min(len(vals) - 1, int(0.9 * len(vals)))]
    ref = max(ref, 1e-6)
    return {t: min(v / ref, 2.0) for t, v in raw.items()}


def mouth_activity(track: dict) -> dict[float, float]:
    """Mean absolute change of the mouth patch between consecutive samples of a track."""
    import numpy as np
    ts = sorted(track["patches"])
    raw = {}
    for a, b in zip(ts, ts[1:]):
        raw[b] = float(np.abs(track["patches"][b] - track["patches"][a]).mean())
    return normalise_activity(raw)


def active_speakers(tracks: list[dict], times: list[float], *, min_hold: float = MIN_HOLD,
                    ratio: float = SWITCH_RATIO, window: float = 1.0) -> list[int]:
    """Index of the talking person at each sample time. Activity is averaged over ``window`` seconds,
    the holder keeps the picture for ``min_hold`` seconds, and a challenger must beat them by ``ratio``."""
    acts = [mouth_activity(tr) for tr in tracks]

    def level(i: int, t: float) -> float:
        vals = [v for tt, v in acts[i].items() if t - window < tt <= t]
        return sum(vals) / len(vals) if vals else 0.0

    # start with whoever is most active over the first stretch
    holder = max(range(len(tracks)), key=lambda i: level(i, times[min(len(times) - 1, 4)]) if times else 0)
    out, since = [], times[0] if times else 0.0
    for t in times:
        if t - since >= min_hold:
            challenger = max((i for i in range(len(tracks)) if i != holder), key=lambda i: level(i, t))
            if level(challenger, t) > ratio * max(level(holder, t), 1e-3):
                holder, since = challenger, t
        out.append(holder)
    return out


def segments(times: list[float], who: list[int], duration: float) -> list[tuple[float, float, int]]:
    """Runs of the same speaker as ``(start, end, track index)``; the first starts at 0, the last ends at ``duration``."""
    runs: list[list] = []
    for t, i in zip(times, who):
        if runs and runs[-1][2] == i:
            continue
        runs.append([0.0 if not runs else t, 0.0, i])
    for a, b in zip(runs, runs[1:]):
        a[1] = b[0]
    if runs:
        runs[-1][1] = duration
    return [(a, b, i) for a, b, i in runs]


def cut_path(tracks: list[dict], segs: list[tuple[float, float, int]], crop_frac: float,
             src_w: float) -> list[tuple[float, float]]:
    """A camera path that follows each speaker inside their own turn and CUTS between turns: the first
    keyframe of a turn is already on the new speaker."""
    out: list[tuple[float, float]] = []
    for t0, t1, i in segs:
        tr = tracks[i]
        mean = sum(tr["pts"].values()) / len(tr["pts"])
        subject = [(t - t0, tr["pts"].get(t)) for t in sorted({*(tt for tt in tr["pts"] if t0 <= tt <= t1)})]
        subject = [(0.0, mean)] if not subject else subject
        path = smooth_path(subject, max(t1 - t0, 0.1), crop_frac) or [(0.0, min(max(mean, crop_frac / 2), 1 - crop_frac / 2))]
        if out and out[-1][0] >= round(t0, 3):      # the old speaker's last keyframe lands on the cut: drop it
            out.pop()
        out += [(round(t0 + t, 3), cx) for t, cx in path]
    return out


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
                 out_aspect: float = OUT_ASPECT, min_coverage: float = MIN_COVERAGE,
                 multi: str = "switch") -> Framing:
    """``multi``: what to do when two or more people are on screen: "switch" (cut to whoever talks),
    "split" (stack two people) or "off" (follow the main face only, as before)."""
    src_w, src_h = probe_size(video)
    if src_w / src_h <= out_aspect * 1.05:               # already vertical (or nearly): a centre crop is right
        return Framing("cover")
    samples = sample_faces(video, start, end, detector or HaarDetector(), patches=multi != "off")
    crop_frac = (src_h * out_aspect) / src_w
    people = speaker_tracks(samples, src_w, src_h) if multi != "off" else []
    if len(people) >= 2:
        f = _plan_people(people, samples, end - start, crop_frac, src_w, src_h, multi)
        if f:
            return f
    subject = pick_subject(samples, src_w, src_h)
    coverage = sum(1 for _, c in subject if c is not None) / max(1, len(subject))
    if coverage < min_coverage:
        return Framing("fit_blur", coverage=coverage)    # no face to follow: show the whole frame
    path = smooth_path(subject, end - start, crop_frac)
    return Framing("track", [(round(t, 3), round(cx, 4)) for t, cx in path], coverage)


def _plan_people(people: list[dict], samples: list[Sample], duration: float, crop_frac: float,
                 src_w: float, src_h: float, multi: str) -> Optional[Framing]:
    """Framing for two or more people, or None to fall back to the single-subject path."""
    people = sorted(people, key=lambda tr: -tr["hits"])[:2]           # at most two are shown
    people.sort(key=lambda tr: sum(tr["pts"].values()) / len(tr["pts"]))   # left to right
    means = [sum(tr["pts"].values()) / len(tr["pts"]) for tr in people]
    coverage = sum(1 for s in samples if s.boxes) / max(1, len(samples))
    if abs(means[1] - means[0]) < GROUP_SPAN * crop_frac:             # they fit one window: frame them together
        both = [(s.t, (sum(tr["pts"][s.t] for tr in people if s.t in tr["pts"]) /
                       max(1, sum(1 for tr in people if s.t in tr["pts"]))) if any(s.t in tr["pts"] for tr in people)
                 else None) for s in samples]
        path = smooth_path(both, duration, crop_frac)
        return Framing("track", [(round(t, 3), round(cx, 4)) for t, cx in path], coverage, people=2) if path else None
    if multi == "split":
        def centre(tr):
            bs = list(tr["boxes"].values())
            return (sum(b.cx for b in bs) / len(bs) / src_w, sum(b.cy for b in bs) / len(bs) / src_h)
        return Framing("split", coverage=coverage, split=[centre(tr) for tr in people], people=2)
    times = [s.t for s in samples]
    segs = segments(times, active_speakers(people, times), duration)
    path = cut_path(people, segs, crop_frac, src_w)
    return Framing("track", [(round(t, 3), round(cx, 4)) for t, cx in path], coverage, people=2)
