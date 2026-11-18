"""Who is who on screen: the seats in a fixed shot, and which voice sits in which seat.

    seats    = find_seats(video)                    # faces over the whole video, chained by position: one seat per person
    turns    = voices.diarize(audio)                # who speaks when, from the voice alone
    mapping  = associate(video, turns, seats)       # a voice belongs to the seat whose mouth moves when that voice is heard

The picture alone cannot say who is talking in a wide shot of four people (a nod, a gesture or a re-focus all move pixels), and the voice
alone does not say where the speaker is. Together over a whole recording they do: for every voice, the seat whose mouth is busier while
that voice is heard than at other times is the one it comes from. A voice with no such seat is off screen (an interviewer holding the microphone).

The result is a ``SpeakerMap``, saved as ``speakers.json`` next to the video it describes, so every clip, every re-render and the person's
corrections (``overrides``) reuse it. It assumes a camera that stays put; a cut between camera angles makes the seats meaningless, and then
the map is rejected (too few seats seen often enough) and the single-person path takes over.
"""
from __future__ import annotations

import itertools
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import voices as vc
from .reframe import Box, HaarDetector, Sample, _build_tracks, mouth_patch, probe_size

FACE_MODEL = Path(__file__).parent / "models" / "face_detection_yunet_2023mar.onnx"    # YuNet, MIT (OpenCV zoo)
MAP_VERSION = 1
DISCOVERY_RATE = 1.0        # frames a second used to find the seats
DISCOVERY_MAX = 600         # ... and at most this many frames, however long the video
MIN_SEAT_SHARE = 0.10       # a seat is a face seen in at least this share of the frames
SEAT_RADIUS = 0.07          # a face belongs to a seat within this fraction of the frame
ACT_RATE = 8.0              # frames a second used to watch mouths
PER_VOICE_TURNS = 6         # the longest turns of each voice that are watched
TURN_SECONDS = 4.0          # ... for at most this long each (from the middle of the turn)
MIN_LIFT = 1.15             # a seat's mouth must be this much busier while the voice is heard
MIN_SAMPLES = 8             # ... measured on at least this many frame pairs in each group
MIN_SEEN = 0.5              # ... and the seat must be in view in at least this share of the voice's frames
FRAME_WIDTH = 960
MIN_FACE_SCORE = 0.8        # a face the detector is less sure of is not a seat (captions, icons and sculpted faces score lower than people)
MIN_FACE_WIDTH, MAX_FACE_WIDTH = 0.025, 0.4     # a face is this share of the frame width: a speck is an icon, a half-frame box is not a head
MIN_ASPECT, MAX_ASPECT = 0.6, 1.7               # height / width of a face box
STATIC_LIVE = 1.5           # a face whose picture changes less than this (grey levels, 1 s apart) is not alive: a statue, a poster, a caption on the picture
ACT_FLOOR = 1.0             # a seat whose mouth region never changes more than this (its 90th percentile) is no speaker, whatever the ratios say
SAME_PLACE = 0.035          # seats (or faces) this close, as a fraction of the frame, are one place


class YuNetDetector:
    """OpenCV's YuNet face detector (a small ONNX network): finds the small, turned and half-hidden faces the Haar cascade misses."""

    def __init__(self, score: float = 0.7, max_width: int = FRAME_WIDTH):
        self.score, self.max_width = score, max_width
        self._net = None

    def _load(self, w: int, h: int):
        import cv2
        if self._net is None:
            self._net = cv2.FaceDetectorYN.create(str(FACE_MODEL), "", (w, h), self.score, 0.3, 5000)
        self._net.setInputSize((w, h))
        return self._net

    def detect(self, frame_bgr) -> list[Box]:
        import cv2
        h, w = frame_bgr.shape[:2]
        scale = min(1.0, self.max_width / w)
        small = cv2.resize(frame_bgr, (int(w * scale), int(h * scale))) if scale < 1 else frame_bgr
        _, found = self._load(small.shape[1], small.shape[0]).detect(small)
        if found is None:
            return []
        return [Box(float(f[0]) / scale, float(f[1]) / scale, float(f[2]) / scale, float(f[3]) / scale, float(f[-1])) for f in found if f[2] > 8 and f[3] > 8]


def default_detector():
    """YuNet when OpenCV has it and the model file is there, else the Haar cascade."""
    try:
        import cv2
        if hasattr(cv2, "FaceDetectorYN") and FACE_MODEL.is_file():
            return YuNetDetector()
    except ImportError:
        pass
    return HaarDetector()


@dataclass
class Seat:
    cx: float                  # centre of the face, as a fraction of the frame
    cy: float
    w: float                   # size of the face box, as a fraction of the frame
    h: float
    seen: float = 0.0          # share of the frames the face was found in
    live: float = -1.0         # how much the face's picture changes from one second to the next (-1: not measured)


@dataclass
class SpeakerMap:
    seats: list[Seat]
    turns: list[vc.Turn]
    voice_seat: dict[int, Optional[int]]       # voice -> seat index (None: off screen / no match)
    lift: dict[int, float] = field(default_factory=dict)
    duration: float = 0.0
    overrides: dict[int, Optional[int]] = field(default_factory=dict)       # a person's corrections: win over ``voice_seat``
    signature: str = ""
    picture_t: Optional[float] = None          # where in the video the most people were seen together (the picture is cut from there)

    def seat_of(self, voice: int) -> Optional[int]:
        if voice in self.overrides:
            return self.overrides[voice]
        return self.voice_seat.get(voice)

    @property
    def usable(self) -> bool:
        """At least two seats and one voice that sits in one of them."""
        return len(self.seats) >= 2 and any(self.seat_of(v) is not None for v in {t.voice for t in self.turns})

    def to_json(self) -> dict:
        return {"version": MAP_VERSION, "signature": self.signature, "duration": round(self.duration, 2),
                "seats": [{"cx": round(s.cx, 4), "cy": round(s.cy, 4), "w": round(s.w, 4), "h": round(s.h, 4), "seen": round(s.seen, 3), "live": round(s.live, 2)} for s in self.seats],
                "turns": [[t.start, t.end, t.voice] for t in self.turns],
                "voice_seat": {str(k): v for k, v in self.voice_seat.items()}, "lift": {str(k): round(v, 3) for k, v in self.lift.items()},
                "overrides": {str(k): v for k, v in self.overrides.items()}, "picture_t": self.picture_t}

    @staticmethod
    def from_json(d: dict) -> "SpeakerMap":
        return SpeakerMap([Seat(**s) for s in d["seats"]], [vc.Turn(float(a), float(b), int(v)) for a, b, v in d["turns"]],
                          {int(k): (None if v is None else int(v)) for k, v in d["voice_seat"].items()}, {int(k): float(v) for k, v in d.get("lift", {}).items()},
                          float(d.get("duration", 0.0)), {int(k): (None if v is None else int(v)) for k, v in d.get("overrides", {}).items()}, str(d.get("signature", "")),
                          None if d.get("picture_t") is None else float(d["picture_t"]))


def video_signature(video: str | Path) -> str:
    st = os.stat(video)
    return f"{st.st_size}:{int(st.st_mtime)}"


# --- frames ---------------------------------------------------------------------------------

def read_frames(video: str, t0: float, t1: float, rate: float, *, width: int = FRAME_WIDTH, check: Callable[[], None] = lambda: None):
    """``(t, frame_bgr)`` at ``rate`` frames a second from ``t0`` to ``t1``, scaled to at most ``width`` pixels wide (ffmpeg does the seeking and the scaling)."""
    import numpy as np
    w, h = probe_size(video)
    sw = min(width, w)
    sh = max(2, int(round(h * sw / w / 2)) * 2)
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t0):.3f}", "-t", f"{max(0.1, t1 - t0):.3f}", "-i", str(video),
           "-vf", f"fps={rate},scale={sw}:{sh}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size = sw * sh * 3
    k = 0
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            if k % 20 == 0:
                check()
            yield t0 + k / rate, np.frombuffer(buf, np.uint8).reshape(sh, sw, 3)
            k += 1
    finally:
        proc.kill()
        proc.wait()


# --- what is a face worth a seat ----------------------------------------------------------

def plausible(box: Box, w: float, h: float) -> bool:
    """Sure enough, and the size and shape of a head in a shot: not a speck on an icon, not a text box."""
    return (box.score >= MIN_FACE_SCORE and MIN_FACE_WIDTH <= box.w / w <= MAX_FACE_WIDTH and MIN_ASPECT <= box.h / max(box.w, 1.0) <= MAX_ASPECT)


def face_patch(frame_bgr, box: Box):
    """The whole face as a tiny grey array with its mean taken off (a change of light is not life)."""
    import cv2
    h, w = frame_bgr.shape[:2]
    x0, x1, y0, y1 = max(0, int(box.x)), min(w, int(box.x + box.w)), max(0, int(box.y)), min(h, int(box.y + box.h))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    p = cv2.resize(cv2.cvtColor(frame_bgr[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY), (16, 16), interpolation=cv2.INTER_AREA).astype("float32")
    return p - p.mean()


def liveness(patches: list) -> float:
    """The typical change of a face's picture between neighbouring samples; -1 when there are too few to say."""
    import numpy as np
    diffs = [float(np.abs(b - a).mean()) for a, b in zip(patches, patches[1:])]
    return float(np.median(diffs)) if len(diffs) >= 2 else -1.0


def static_ones(lives: list[float]) -> set[int]:
    """Indices of the places that do not move at all while another one does: sculpture, pictures, graphics. Never all of them."""
    top = max(lives, default=-1.0)
    if top < 2 * STATIC_LIVE:
        return set()
    return {i for i, v in enumerate(lives) if 0 <= v < STATIC_LIVE}


# --- one person or several? -------------------------------------------------------------------

QUICK_SAMPLES = 16          # frames looked at to tell a one-person video from a group shot
QUICK_GROUP_SHARE = 0.2     # a group shot has two or more faces in at least this share of them (and in at least two frames)


def count_faces(video: str, duration: float, detector, *, samples: int = QUICK_SAMPLES, check: Callable[[], None] = lambda: None) -> list[int]:
    """How many faces are in each of ``samples`` frames spread over the video (a handful of quick seeks, not a pass over the whole file)."""
    rows: list[list[tuple[int, object]]] = []                       # per sampled frame: (place number, face patch) of each plausible face
    places: list[tuple[float, float]] = []                          # where faces were seen, as fractions of the frame
    for k in range(samples):
        check()
        t = duration * (k + 0.5) / samples
        for _, frame in read_frames(video, t, t + 1.0, 1.0, check=check):
            h, w = frame.shape[:2]
            row = []
            for b in detector.detect(frame):
                if not plausible(b, w, h):
                    continue
                fx, fy = b.cx / w, b.cy / h
                i = next((i for i, c in enumerate(places) if abs(c[0] - fx) < SAME_PLACE and abs(c[1] - fy) < SAME_PLACE), None)
                if i is None:
                    places.append((fx, fy))
                    i = len(places) - 1
                row.append((i, face_patch(frame, b)))
            rows.append(row)
            break
    series = [[p for row in rows for j, p in row if j == i and p is not None] for i in range(len(places))]
    still = static_ones([liveness(sr) for sr in series])             # a statue seen in every frame is not a third person
    return [sum(1 for j, _ in row if j not in still) for row in rows]


def is_one_person(counts: list[int]) -> bool:
    """No group shot among the sampled frames: faces are found, but never two at once more than now and then."""
    if not counts:
        return False
    return sum(1 for c in counts if c >= 2) < max(2, int(QUICK_GROUP_SHARE * len(counts) + 0.999))


def is_small_group(counts: list[int]) -> bool:
    """One or two people: three faces are never seen together more than now and then. The people search, the voice listening and the mouth watching
    are made for a table of three or more; one or two people are framed the older, cheaper way (the face in front of the speaker)."""
    if not counts:
        return False
    return sum(1 for c in counts if c >= 3) < max(2, int(QUICK_GROUP_SHARE * len(counts) + 0.999))


# --- seats ----------------------------------------------------------------------------------

def find_seats(video: str, duration: float, detector, *, check: Callable[[], None] = lambda: None,
               say: Callable[[str], None] = lambda _m: None) -> tuple[list[Seat], object]:
    """The seats of a fixed shot: faces at ~1 frame a second over the whole video, chained into tracks by position. Returns the seats (left to right)
    and the frame where the most seats were visible together (for the picture the person labels voices on)."""
    rate = min(DISCOVERY_RATE, DISCOVERY_MAX / max(duration, 1.0))
    samples: list[Sample] = []
    best_frame, best_n, size = None, 0, None
    quarter = 0
    for t, frame in read_frames(video, 0.0, duration, rate, check=check):
        if duration > 120 and int(t * 4 / duration) > quarter:                            # a long video: say how far the search has got
            quarter = int(t * 4 / duration)
            say(f"looking for the people on screen: {int(t // 60)} of {int(duration // 60)} minutes")
        size = (frame.shape[1], frame.shape[0])
        boxes = [b for b in detector.detect(frame) if plausible(b, size[0], size[1])]
        samples.append(Sample(t, boxes, [face_patch(frame, b) for b in boxes]))
        if len(boxes) > best_n:
            best_n, best_frame = len(boxes), (frame.copy(), boxes, t)
    if not samples or size is None:
        return [], None
    w, h = size
    tracks = _build_tracks(samples, w, h)
    need = max(3, MIN_SEAT_SHARE * len(samples))
    seats = []
    for tr in tracks:
        if tr["hits"] < need:
            continue
        bs = sorted(tr["boxes"].values(), key=lambda b: b.cx)
        mid = bs[len(bs) // 2]
        cx = sorted(b.cx for b in bs)[len(bs) // 2]
        cy = sorted(b.cy for b in bs)[len(bs) // 2]
        ts = sorted(tr["patches"])
        seats.append(Seat(cx / w, cy / h, mid.w / w, mid.h / h, tr["hits"] / len(samples), liveness([tr["patches"][t] for t in ts])))
    seats = merge_seats(seats)
    gone = static_ones([s.live for s in seats])
    if gone:
        say(f"{len(gone)} face(s) that never move (a statue, a picture or a caption) are not people")
    return [s for i, s in enumerate(seats) if i not in gone], best_frame


def merge_seats(seats: list[Seat]) -> list[Seat]:
    """One seat per person: two tracks whose faces sit on top of each other (the same head found twice, or after it moved) are one seat."""
    seats = sorted(seats, key=lambda s: -s.seen)                                 # the most seen is the anchor
    kept: list[Seat] = []
    for s in seats:
        twin = next((k for k in kept if abs(k.cx - s.cx) < max(0.6 * max(k.w, s.w), SAME_PLACE) and abs(k.cy - s.cy) < max(1.0 * max(k.h, s.h), SAME_PLACE)), None)
        if twin is None:
            kept.append(Seat(s.cx, s.cy, s.w, s.h, s.seen, s.live))
        else:
            twin.seen = min(1.0, twin.seen + s.seen)
            twin.live = max(twin.live, s.live)
    return sorted(kept, key=lambda s: s.cx)


def _seat_of_box(box: Box, seats: list[Seat], w: float, h: float) -> Optional[int]:
    best, best_d = None, None
    for i, s in enumerate(seats):
        d = max(abs(box.cx / w - s.cx), abs(box.cy / h - s.cy))
        if d <= SEAT_RADIUS and (best_d is None or d < best_d):
            best, best_d = i, d
    return best


# --- voice -> seat --------------------------------------------------------------------------

def _watch_windows(turns: list[vc.Turn]) -> list[tuple[float, float, int]]:
    out = []
    for v in sorted({t.voice for t in turns}):
        mine = sorted((t for t in turns if t.voice == v), key=lambda t: t.end - t.start, reverse=True)[:PER_VOICE_TURNS]
        for t in mine:
            mid = (t.start + t.end) / 2
            half = min(TURN_SECONDS, t.end - t.start) / 2
            if half >= 0.75:
                out.append((mid - half, mid + half, v))
    return sorted(out)


def mouth_series(video: str, windows: list[tuple[float, float, int]], seats: list[Seat], detector, *,
                 check: Callable[[], None] = lambda: None, say: Callable[[str], None] = lambda _m: None) -> tuple[list[tuple[int, int, float]], dict[int, tuple[int, int]]]:
    """Rows ``(voice, seat, activity)`` (how much a seat's mouth region changed between two frames 1/ACT_RATE s apart while a voice was heard) and, per seat,
    ``(frames it was in view, frames looked at)``."""
    import numpy as np
    rows: list[tuple[int, int, float]] = []
    seen: dict[int, list[int]] = {}
    quarter = 0
    for k, (t0, t1, v) in enumerate(windows):
        if len(windows) >= 8 and k * 4 // len(windows) > quarter:
            quarter = k * 4 // len(windows)
            say(f"matching voices to people: {k} of {len(windows)} stretches watched")
        prev: dict[int, object] = {}
        for _, frame in read_frames(video, t0, t1, ACT_RATE, check=check):
            h, w = frame.shape[:2]
            cur: dict[int, object] = {}
            for b in detector.detect(frame):
                i = _seat_of_box(b, seats, w, h)
                if i is None or i in cur:
                    continue
                p = mouth_patch(frame, b)
                if p is not None:
                    cur[i] = p - p.mean()                                     # a change of light on the face is not a mouth moving
            for i in range(len(seats)):
                s = seen.setdefault(i, [0, 0])
                s[1] += 1
                s[0] += i in cur
                if i in cur and i in prev and cur[i].shape == prev[i].shape:
                    rows.append((v, i, float(np.abs(cur[i] - prev[i]).mean())))
            prev = cur
    return rows, {i: (a, b) for i, (a, b) in seen.items()}


def assign(rows: list[tuple[int, int, float]], seen: dict[int, tuple[int, int]], voices: list[int], n_seats: int) -> tuple[dict[int, Optional[int]], dict[int, float]]:
    """The seat each voice comes from (None when no seat's mouth follows it), and the lift that decided it."""
    import numpy as np
    by_seat: dict[int, list[float]] = {s: [] for s in range(n_seats)}
    for _, s, a in rows:
        by_seat[s].append(a)
    p90 = {s: float(np.percentile(a, 90)) if a else 0.0 for s, a in by_seat.items()}
    ref = {s: max(p90[s], ACT_FLOOR) for s in by_seat}                    # each person's own scale (a still sitter is not drowned by a lively one), but never below the noise: a statue's flicker is not a mouth
    acts: dict[tuple[int, int], list[float]] = {}
    for v, s, a in rows:
        acts.setdefault((v, s), []).append(min(a / ref[s], 2.0))
    lift: dict[tuple[int, int], float] = {}
    for v in voices:
        for s in range(n_seats):
            mine = acts.get((v, s), [])
            others = [x for (vv, ss), xs in acts.items() if ss == s and vv != v for x in xs]
            a_seen, a_all = seen.get(s, (0, 0))
            if len(mine) < MIN_SAMPLES or a_all == 0 or a_seen / a_all < MIN_SEEN or p90[s] < ACT_FLOOR:
                continue
            if len(voices) > 1 and len(others) >= MIN_SAMPLES:                       # one voice alone has nothing to be compared with: the picture decides
                lift[(v, s)] = (sum(mine) / len(mine)) / max(sum(others) / len(others), 0.05)
    # one voice per seat: the best assignment by total lift, brute force (a few voices, a few seats)
    best, best_score = {}, 0.0
    options = [[None] + [s for s in range(n_seats) if lift.get((v, s), 0.0) >= MIN_LIFT] for v in voices]
    for combo in itertools.product(*options):
        chosen = [s for s in combo if s is not None]
        if len(chosen) != len(set(chosen)):
            continue
        score = sum(lift[(v, s)] - 1.0 for v, s in zip(voices, combo) if s is not None)
        if score > best_score + 1e-9:
            best, best_score = dict(zip(voices, combo)), score
    result = {v: best.get(v) for v in voices}
    return result, {v: lift.get((v, s), 0.0) for v, s in result.items() if s is not None}


def cached_map(video: str | Path, out_dir: str | Path) -> tuple[bool, Optional[SpeakerMap]]:
    """``(found, map)``: whether ``out_dir/speakers.json`` was made from this very video, and its usable map (None for "one person")."""
    try:
        sig = video_signature(video)
        sm = SpeakerMap.from_json(json.loads((Path(out_dir) / "speakers.json").read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        return False, None
    return (True, sm if sm.usable else None) if sm.signature == sig else (False, None)


def analyse(video: str | Path, *, detector=None, embedder=None, audio_loader: Callable | None = None,
            say: Callable[[str], None] = lambda _m: None, check: Callable[[], None] = lambda: None) -> tuple[Optional[SpeakerMap], Optional[tuple]]:
    """Look at the video and listen to it: ``(map, best_frame)``. ``map`` is None when the video is not one this applies to (vertical, short, unreadable);
    an empty map (no seats) says "one person". ``best_frame`` is ``(frame, boxes, t)`` where the most seats were visible together. Nothing is read from or
    written to disk: the server caches the result, a worker sends it back."""
    video = str(video)
    try:
        sig = video_signature(video)
        w, h = probe_size(video)
    except (OSError, ValueError):
        return None, None
    if w / h <= 9 / 16 * 1.05:
        return None, None
    from .indic import media_duration
    duration = media_duration(video) or 0.0
    if duration < 20:
        return None, None
    detector = detector or default_detector()
    counts = count_faces(video, duration, detector, check=check)
    if is_small_group(counts):                                                                # one or two people: seconds, not a pass over the whole video
        say("one person on screen: nobody to tell apart" if is_one_person(counts) else "two people on screen: framed the usual way, no voice search")
        return SpeakerMap([], [], {}, {}, duration, {}, sig), None
    say("looking for the people on screen")
    seats, frame = find_seats(video, duration, detector, check=check, say=say)
    sm = SpeakerMap(seats, [], {}, {}, duration, {}, sig, picture_t=(frame[2] if frame else None))
    if len(seats) >= 2:
        say(f"{len(seats)} people on screen")
        embedder = embedder or vc.default_embedder()
        if embedder is None:
            say("voices cannot be told apart here: the picture alone decides who is shown")
        else:
            from .asr import load_audio
            try:
                audio = (audio_loader or load_audio)(video)
                sm.turns = vc.diarize(audio, embedder, say=say, check=check)
            finally:
                if hasattr(embedder, "release"):
                    embedder.release()
            voices = sorted({t.voice for t in sm.turns})
            if voices:
                say(f"{len(voices)} voice(s) heard; matching each voice to a person")
                rows, seen = mouth_series(video, _watch_windows(sm.turns), seats, detector, check=check, say=say)
                sm.voice_seat, sm.lift = assign(rows, seen, voices, len(seats))
                say("who is who: " + ", ".join(f"voice {v + 1} = person {s + 1}" if s is not None else f"voice {v + 1} = off screen" for v, s in sm.voice_seat.items()))
    return sm, frame


def build_speaker_map(video: str | Path, out_dir: str | Path, *, detector=None, embedder=None, audio_loader: Callable | None = None,
                      say: Callable[[str], None] = lambda _m: None, check: Callable[[], None] = lambda: None) -> Optional[SpeakerMap]:
    """The ``SpeakerMap`` of ``video`` (cached in ``out_dir/speakers.json``), or None when there is nobody to tell apart or no way to hear
    voices. Single-person videos cost a few seconds of face finding and nothing else."""
    found, sm = cached_map(video, out_dir)
    if found:
        return sm
    sm, frame = analyse(video, detector=detector, embedder=embedder, audio_loader=audio_loader, say=say, check=check)
    if sm is None:
        return None
    return store_map(out_dir, video, sm, frame)


def store_map(out_dir: str | Path, video: str | Path, sm: SpeakerMap, frame: Optional[tuple]) -> Optional[SpeakerMap]:
    """Save a map and its numbered picture (``frame`` is the best frame, or None: the picture is cut from the video at ``sm.picture_t`` when it has one)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if len(sm.seats) >= 2:
        if frame is None and sm.picture_t is not None:
            for _, img in read_frames(str(video), sm.picture_t, sm.picture_t + 1.0, 1.0):
                frame = (img, [], sm.picture_t)
                break
        if frame is not None:
            _save_picture(out_dir / "speakers.jpg", frame, sm.seats)
    (out_dir / "speakers.json").write_text(json.dumps(sm.to_json()), encoding="utf-8")
    return sm if sm.usable else None


def _save_picture(path: Path, frame_boxes, seats: list[Seat]) -> None:
    """One frame with every seat numbered: what the person looks at to say who is who."""
    import cv2
    frame = frame_boxes[0]
    img = frame.copy()
    h, w = img.shape[:2]
    for i, s in enumerate(seats):
        x0, y0 = int((s.cx - s.w / 2) * w), int((s.cy - s.h / 2) * h)
        x1, y1 = int((s.cx + s.w / 2) * w), int((s.cy + s.h / 2) * h)
        cv2.rectangle(img, (x0, y0), (x1, y1), (60, 200, 255), 3)
        cv2.putText(img, str(i + 1), (max(0, x0), max(24, y0 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (60, 200, 255), 3, cv2.LINE_AA)
    cv2.imwrite(str(path), img, [cv2.IMWRITE_JPEG_QUALITY, 82])


def load_map(out_dir: str | Path, *, usable_only: bool = True) -> Optional[SpeakerMap]:
    """The saved map of a job (for a re-plan), or None."""
    try:
        sm = SpeakerMap.from_json(json.loads((Path(out_dir) / "speakers.json").read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return sm if (sm.usable or not usable_only) else None


def seat_centres(sm: SpeakerMap, src_w: float, src_h: float) -> list[float]:
    """Where the crop window sits when it follows each seat (kept inside the frame)."""
    crop = (src_h * 9 / 16) / src_w
    return [round(min(max(s.cx, crop / 2), 1 - crop / 2), 4) for s in sm.seats]


def save_map(out_dir: str | Path, sm: SpeakerMap) -> None:
    (Path(out_dir) / "speakers.json").write_text(json.dumps(sm.to_json()), encoding="utf-8")
