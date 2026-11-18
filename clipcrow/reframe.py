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

from .graphics import graphic_spans, thumbnail

OUT_ASPECT = 9 / 16
SAMPLE_RATE = 5.0          # detections per second
KEY_RATE = 10.0            # camera keyframes per second
MIN_COVERAGE = 0.30        # fraction of samples with a subject needed to follow it
DEADBAND = 0.15            # the camera ignores the subject within this many crop-widths of centre
MAX_SPEED = 0.60           # crop-widths per second the camera may travel
CUT_DIST = 25.0           # two samples whose tiny thumbnails differ by more than this (0-255, mean) are two camera shots ...
CUT_RATIO = 4.0           # ... and by more than this many times the usual change between samples
MIN_SHOT = 0.5            # a stretch shorter than this (seconds) is not a shot of its own
TRACK_RADIUS = 0.12        # a detection joins a track if within this fraction of the frame of its last position


@dataclass(frozen=True)
class Box:
    x: float
    y: float
    w: float
    h: float
    score: float = 1.0               # the detector's own confidence (1.0 when it gives none)

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
    thumb: object = None                          # a tiny colour thumbnail of the frame (when asked for: finds slides and graphics)


@dataclass
class Framing:
    framing: str                   # cover | track | fit_blur | split
    keyframes: list[tuple[float, float]] = field(default_factory=list)   # (clip time, cx as fraction of source width)
    coverage: float = 0.0
    split: list[tuple[float, float]] = field(default_factory=list)       # two (cx, cy) fractions for "split"
    people: int = 1
    fit_spans: list[tuple[float, float]] = field(default_factory=list)   # "track" only: stretches shown whole (a slide the crop would cut)
    face_zone: Optional[tuple[float, float, float, float]] = None        # "track" only: where the face is in the vertical frame over the clip (x0, y0, x1, y1 as fractions)


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
                 rate: float = SAMPLE_RATE, patches: bool = False, thumbs: bool = False) -> list[Sample]:
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
            out.append(Sample(t, boxes, [mouth_patch(frame, b) for b in boxes] if patches else [], thumbnail(frame) if thumbs else None))
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


def _camera_at(keys: list[tuple[float, float]], t: float) -> float:
    """Where the crop is at clip time ``t``: keyframes are commands, so the crop holds the last one until the next."""
    cam = keys[0][1]
    for kt, kc in keys:
        if kt <= t + 1e-6:
            cam = kc
        else:
            break
    return cam


def _union_zone(rects: list[tuple[float, float, float, float]]) -> Optional[tuple[float, float, float, float]]:
    rects = [(max(0.0, a), max(0.0, b), min(1.0, c), min(1.0, d)) for a, b, c, d in rects if c > 0.0 and a < 1.0 and d > 0.0 and b < 1.0]
    rects = [r for r in rects if r[2] - r[0] > 0.01 and r[3] - r[1] > 0.01]
    if not rects:
        return None
    return (round(min(r[0] for r in rects), 4), round(min(r[1] for r in rects), 4), round(max(r[2] for r in rects), 4), round(max(r[3] for r in rects), 4))


def face_zone_of_path(samples: list[Sample], subject: list[tuple[float, Optional[float]]], keys: list[tuple[float, float]],
                      src_w: float, src_h: float, crop_frac: float) -> Optional[tuple[float, float, float, float]]:
    """Where the followed face is in the OUTPUT frame over the whole clip: each sampled face nearest the subject, placed in the crop window as it was
    then. What a logo must keep clear of."""
    if not keys:
        return None
    rects = []
    cx_by_t = {t: cx for t, cx in subject if cx is not None}
    for sm in samples:
        cx = cx_by_t.get(sm.t)
        if cx is None or not sm.boxes:
            continue
        b = min(sm.boxes, key=lambda bb: abs(bb.cx / src_w - cx))
        if abs(b.cx / src_w - cx) > TRACK_RADIUS:
            continue
        left = min(max(_camera_at(keys, sm.t) - crop_frac / 2, 0.0), 1.0 - crop_frac)
        rects.append(((b.x / src_w - left) / crop_frac, b.y / src_h, ((b.x + b.w) / src_w - left) / crop_frac, (b.y + b.h) / src_h))
    return _union_zone(rects)


def shot_cuts(samples: list[Sample]) -> list[float]:
    """Clip times of the camera cuts: where the picture changes far more than it does from one sample to the next (a cut from the guest to the host,
    a different camera angle). A shot shorter than ``MIN_SHOT`` is folded into the one before it. Empty when the samples carry no thumbnails."""
    th = [sm.thumb for sm in samples]
    if len(samples) < 3 or any(t is None for t in th):
        return []
    import numpy as np
    d = [float(abs(th[i] - th[i - 1]).mean()) for i in range(1, len(th))]
    usual = max(float(np.median(d)), 1.0)
    cuts: list[float] = []
    for i, v in enumerate(d, 1):
        if v > CUT_DIST and v > CUT_RATIO * usual:
            cuts.append(round((samples[i - 1].t + samples[i].t) / 2, 3))
    changed = True
    while changed and cuts:                                       # a blip (a flash, one odd frame) cuts away and straight back: both cuts go
        changed = False
        for i in range(len(cuts) - 1):
            if cuts[i + 1] - cuts[i] < MIN_SHOT:
                del cuts[i:i + 2]
                changed = True
                break
    return [c for c in cuts if c >= MIN_SHOT]                     # a first shot shorter than that is not one either


def pick_subject_per_shot(samples: list[Sample], cuts: list[float], src_w: float, src_h: float) -> list[tuple[float, Optional[float]]]:
    """``pick_subject`` run on each camera shot by itself: after a cut the person on screen is a different one, and the face that was the main one
    before the cut is not there. With no cuts this is ``pick_subject``."""
    if not cuts:
        return pick_subject(samples, src_w, src_h)
    bounds = [-1.0] + cuts + [float("inf")]
    out: list[tuple[float, Optional[float]]] = []
    for a, b in zip(bounds, bounds[1:]):
        out += pick_subject([sm for sm in samples if a < sm.t <= b], src_w, src_h)
    return out


def smooth_shots(subject: list[tuple[float, Optional[float]]], cuts: list[float], duration: float, crop_frac: float) -> list[tuple[float, float]]:
    """``smooth_path`` for each shot, joined with a hard cut at every camera cut: the crop is on the new person from the first frame of the shot (no pan
    across the room). A shot with no face holds where the last one was. With no cuts this is ``smooth_path``."""
    if not cuts:
        return smooth_path(subject, duration, crop_frac)
    bounds = [0.0] + cuts + [duration]
    keys: list[tuple[float, float]] = []
    last: Optional[float] = None
    for a, b in zip(bounds, bounds[1:]):
        seg = [(t - a, cx) for t, cx in subject if a <= t < b or (b >= duration and t == b)]
        path = smooth_path(seg, b - a, crop_frac)
        if b < duration and len(path) > 1:
            path = [k for k in path if k[0] < (b - a) - 1e-6] or path[:1]      # the closing key would share the cut's time with the new shot's first
        if not path:
            if last is None:
                continue
            path = [(0.0, last)]
        keys += [(round(a + t, 3), cx) for t, cx in path]
        last = path[-1][1]
    if keys and keys[0][0] > 0:
        keys.insert(0, (0.0, keys[0][1]))
    return keys


def _default_detector():
    from .seats import default_detector          # YuNet when available (finds small and turned faces), else the Haar cascade
    return default_detector()


def plan_framing(video: str, start: float, end: float, *, detector: FaceDetector | None = None,
                 out_aspect: float = OUT_ASPECT, min_coverage: float = MIN_COVERAGE,
                 multi: str = "switch", graphics: bool = False, speakers=None) -> Framing:
    """``speakers``: a ``seats.SpeakerMap`` (who sits where, which voice is whose). With one, the picture follows the VOICE: it cuts to the seat of whoever is
    heard (see ``plan_voices``); the mouth-movement guess below is only the fallback for a recording with no usable map.
    ``multi``: what to do when two or more people are on screen: "switch" (cut to whoever talks),
    "split" (stack two people) or "off" (follow the main face only, as before).
    ``graphics``: the source has slides, cards or b-roll between shots of the speaker. Those stretches are found (``graphics.graphic_spans``)
    and shown whole; the camera ignores faces on them (a portrait on a slide is not a speaker)."""
    src_w, src_h = probe_size(video)
    if src_w / src_h <= out_aspect * 1.05:               # already vertical (or nearly): a centre crop is right
        return Framing("cover")
    if speakers is not None and multi != "off" and not graphics:                     # the voices decide: no need to watch mouths at all
        f = plan_voices(speakers, start, end, (src_h * out_aspect) / src_w, multi)
        if f:
            return f
    samples = sample_faces(video, start, end, detector or _default_detector(), patches=multi != "off", thumbs=True)           # thumbnails: graphics and camera cuts are both found from them
    spans = graphic_spans(samples, end - start, src_w, src_h) if graphics else []
    if spans:
        if sum(b - a for a, b in spans) >= 0.9 * (end - start):
            return Framing("fit_blur", coverage=0.0)             # it is all graphics: nothing to track
        samples = [Sample(s.t, [], [], s.thumb) if any(a <= s.t < b for a, b in spans) else s for s in samples]
    crop_frac = (src_h * out_aspect) / src_w
    if speakers is not None and multi != "off" and spans == []:
        f = plan_voices(speakers, start, end, crop_frac, multi)
        if f:
            return f
    people = speaker_tracks(samples, src_w, src_h) if multi != "off" else []
    if len(people) >= 2:
        f = _plan_people(people, samples, end - start, crop_frac, src_w, src_h, multi)
        if f:
            if f.framing == "track":
                f.fit_spans = spans
            return f
    cuts = shot_cuts(samples)
    subject = pick_subject_per_shot(samples, cuts, src_w, src_h)
    live = max(1, sum(1 for s in samples if not any(a <= s.t < b for a, b in spans)))    # graphics are not "no face found"
    coverage = sum(1 for _, c in subject if c is not None) / live
    if coverage < min_coverage:
        return Framing("fit_blur", coverage=coverage)    # no face to follow: show the whole frame
    path = smooth_shots(subject, cuts, end - start, crop_frac)
    return Framing("track", [(round(t, 3), round(cx, 4)) for t, cx in path], coverage, fit_spans=spans,
                   face_zone=face_zone_of_path(samples, subject, path, src_w, src_h, crop_frac))


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


# --- the voice decides ---------------------------------------------------------------------------

MIN_TURN = 1.0             # a turn shorter than this ("हो", a laugh) does not move the picture
OFFSCREEN_WHOLE = 4.0      # a voice with no seat (the interviewer) for longer than this: show the whole frame; shorter: stay on the last speaker


def _seat_groups(seats, crop_frac: float) -> list[list[int]]:
    """Seats that fit one crop window are one picture (no cut between two people sitting shoulder to shoulder)."""
    groups: list[list[int]] = []
    for i in sorted(range(len(seats)), key=lambda k: seats[k].cx):
        if groups and seats[i].cx - seats[groups[-1][0]].cx < GROUP_SPAN * crop_frac:
            groups[-1].append(i)
        else:
            groups.append([i])
    return groups


def voice_segments(sm, start: float, end: float, crop_frac: float) -> tuple[list[tuple[float, float, Optional[int]]], list[list[int]]]:
    """The clip cut into ``(t0, t1, picture)`` by who is heard (clip time). ``picture`` indexes the seat groups; None is "nobody on screen"."""
    groups = _seat_groups(sm.seats, crop_frac)
    group_of = {i: g for g, members in enumerate(groups) for i in members}
    dur = end - start
    raw: list[list] = []
    for t in sm.turns:
        a, b = max(t.start, start) - start, min(t.end, end) - start
        if b - a <= 0.05:
            continue
        seat = sm.seat_of(t.voice)
        raw.append([a, b, group_of.get(seat) if seat is not None else None])
    raw.sort()
    if not raw:
        return [], groups
    for a, b in zip(raw, raw[1:]):                                          # the pause between two turns goes to the one that just spoke
        if b[0] > a[1]:
            a[1] = b[0]
    raw[0][0] = 0.0
    raw[-1][1] = dur
    segs: list[list] = []
    for a, b, g in raw:                                                     # a short turn is absorbed by the neighbour before it
        if segs and (segs[-1][2] == g or b - a < MIN_TURN):
            segs[-1][1] = b
        else:
            segs.append([a, b, g])
    merged: list[list] = []
    for a, b, g in segs:
        if merged and merged[-1][2] == g:
            merged[-1][1] = b
        else:
            merged.append([a, b, g])
    return [(a, b, g) for a, b, g in merged], groups


def plan_voices(sm, start: float, end: float, crop_frac: float, multi: str) -> Optional[Framing]:
    """Framing from who is heard: cut to the seat of the speaker at every change of voice (no pan: a hard cut), hold the last speaker through
    a short off-screen voice, show the whole frame through a long one. None when the map says nothing about this stretch."""
    if not sm.usable:
        return None
    segs, groups = voice_segments(sm, start, end, crop_frac)
    if not segs or all(g is None for _, _, g in segs):
        return None
    dur = end - start
    lo, hi = crop_frac / 2, 1 - crop_frac / 2

    def centre(g: int) -> float:
        xs = [sm.seats[i].cx for i in groups[g]]
        return min(max(sum(xs) / len(xs), lo), hi)
    shown: list[list] = []                                                  # [t0, t1, group | None, whole]
    for a, b, g in segs:
        if g is None:
            if b - a >= OFFSCREEN_WHOLE:
                shown.append([a, b, None, True])
                continue
            g = shown[-1][2] if shown and shown[-1][2] is not None else None
        if g is None and not shown:                                         # starts off screen: take the first seat that speaks
            g = next((x for _, _, x in segs if x is not None), None)
        if shown and shown[-1][2] == g and not shown[-1][3] and not (g is None):
            shown[-1][1] = b
        else:
            shown.append([a, b, g, False])
    if multi == "split":                                                    # two seats stacked: the two that are heard most
        time_by = {}
        for a, b, g in segs:
            if g is not None:
                time_by[g] = time_by.get(g, 0.0) + (b - a)
        top = sorted(time_by, key=lambda g: -time_by[g])[:2]
        if len(top) == 2:
            top.sort(key=lambda g: centre(g))
            pts = [(sum(sm.seats[i].cx for i in groups[g]) / len(groups[g]), sum(sm.seats[i].cy for i in groups[g]) / len(groups[g])) for g in top]
            return Framing("split", coverage=1.0, split=pts, people=2)
    keys: list[tuple[float, float]] = []
    wholes: list[tuple[float, float]] = []
    for a, b, g, whole in shown:
        if whole or g is None:
            wholes.append((round(a, 3), round(b, 3)))
            if not keys:
                keys.append((0.0, 0.5))
            continue
        cx = round(centre(g), 4)
        if not keys or keys[-1][1] != cx:
            t = round(a, 3)
            if keys and keys[-1][0] >= t:
                keys.pop()
            keys.append((t, cx))
    if not keys:
        return None
    if keys[0][0] > 0:
        keys.insert(0, (0.0, keys[0][1]))
    heard = sum(b - a for a, b, g, whole in shown if not whole and g is not None)
    people = len({g for _, _, g, whole in shown if g is not None and not whole})
    rects = []
    for a, b, g, whole in shown:                                            # the seats that were shown, as they sit in the crop window of that moment
        if whole or g is None:
            continue
        left = min(max(centre(g) - crop_frac / 2, 0.0), 1.0 - crop_frac)
        for i in groups[g]:
            st = sm.seats[i]
            rects.append(((st.cx - st.w / 2 - left) / crop_frac, st.cy - st.h / 2, (st.cx + st.w / 2 - left) / crop_frac, st.cy + st.h / 2))
    return Framing("track", keys, heard / max(dur, 0.001), people=max(1, people), fit_spans=wholes, face_zone=_union_zone(rects))
