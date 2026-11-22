"""Bulk scheduling: spread chosen reels over days on chosen channels, at the best times of each platform.

The plan is a pure function of its inputs (reels in order, channels, posts per day, the first day, a time zone, the best times per platform and "now"),
so a preview and the real scheduling agree, and every rule can be tested without a network.

The best times are DEFAULTS, not measurements of anyone's audience: they follow the general guidance published for short video in India (evenings and
lunch for Reels and Shorts, working-day mornings for LinkedIn). A person can change them per platform, and the saved choice is used from then on.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TZ = "Asia/Kolkata"
# Local times, best first. Reels, Shorts and Telegram do best in the evening and at lunch; X several times a day; LinkedIn on working days.
BEST_TIMES: dict[str, list[str]] = {
    "instagram": ["19:00", "12:30", "21:00", "08:30"],
    "instagram-standalone": ["19:00", "12:30", "21:00", "08:30"],
    "youtube": ["18:30", "13:00", "21:00", "08:00"],
    "facebook": ["13:00", "19:30", "09:00", "21:00"],
    "x": ["09:00", "12:30", "18:00", "21:00", "07:30", "15:00", "20:00", "10:30"],
    "linkedin": ["08:30", "12:00", "17:30", "10:00"],
    "linkedin-page": ["08:30", "12:00", "17:30", "10:00"],
    "telegram": ["20:00", "12:30", "08:00", "18:00"],
    "tiktok": ["19:00", "21:00", "12:00", "16:00"],
}
WORKING_DAYS_ONLY = {"linkedin", "linkedin-page"}
MAX_PER_DAY = 10
MAX_CLIPS = 30
MAX_CHANNELS = 6
MAX_DAYS = 60
MAX_TIMES = 12
DAY_FROM, DAY_TO = 7 * 60, 22 * 60 + 30           # minutes: extra posting times are spread between these
LEAD = timedelta(minutes=10)                       # a slot sooner than this from now is not used
SAME_PLATFORM_STAGGER = 15                         # minutes between two channels of one platform, so they do not post the same minute
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)\Z")
_OWNER = re.compile(r"^[0-9a-f]{32}\Z")


class ScheduleError(ValueError):
    """A sentence that is safe to show a person."""


@dataclass(frozen=True)
class Chan:
    id: str
    name: str
    platform: str


def zone(name: Optional[str]) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ScheduleError("That time zone is not known.") from None


def clean_times(values, platform: str = "") -> list[str]:
    """``HH:MM`` strings in the person's order of preference, without repeats."""
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= MAX_TIMES:
        raise ScheduleError(f"Give between 1 and {MAX_TIMES} times for each platform.")
    out: list[str] = []
    for v in values:
        s = str(v).strip()
        m = _HHMM.match(s)
        if not m:
            raise ScheduleError("Times look like 18:30 (24-hour).")
        if s not in out:
            out.append(s)
    return out


def _minutes(t: str) -> int:
    return int(t[:2]) * 60 + int(t[3:])


def day_slots(times: list[str], per_day: int) -> list[int]:
    """The minutes of the day (local) at which ``per_day`` posts go: the best times first, and when more are wanted than the platform has best times,
    the biggest gaps of the day are split until there are enough. Chronological."""
    chosen = sorted({_minutes(t) for t in times[:per_day]})
    while len(chosen) < per_day:
        points = [DAY_FROM] + [min(max(p, DAY_FROM), DAY_TO) for p in chosen] + [DAY_TO]
        points.sort()
        gaps = [(points[i + 1] - points[i], points[i]) for i in range(len(points) - 1)]
        size, start = max(gaps)
        new = start + size // 2
        new = (new // 5) * 5
        if new in chosen:                                       # the day is full at this resolution
            break
        chosen.append(new)
        chosen.sort()
    return chosen


def plan(clips: list[str], channels: list[Chan], per_day: int, start: date, tz: ZoneInfo, times: dict[str, list[str]], now: datetime) -> list[dict]:
    """Every (reel, channel) with its time. Each channel posts ``per_day`` reels a day, in the order given, from ``start``, at its platform's best times;
    a slot that is already past (or within ten minutes) is skipped; a working-days-only platform skips Saturday and Sunday."""
    if not clips or not channels:
        raise ScheduleError("Choose at least one reel and one channel.")
    if not 1 <= per_day <= MAX_PER_DAY:
        raise ScheduleError(f"Posts per day: between 1 and {MAX_PER_DAY}.")
    seen: dict[str, int] = {}
    rows: list[dict] = []
    for ch in channels:
        index = seen.get(ch.platform, 0)
        seen[ch.platform] = index + 1
        stagger = timedelta(minutes=SAME_PLATFORM_STAGGER * index)
        slots = day_slots(times.get(ch.platform) or BEST_TIMES.get(ch.platform, ["19:00"]), per_day)
        left = list(clips)
        day = start
        for _ in range(MAX_DAYS + 1):
            if not left:
                break
            if ch.platform in WORKING_DAYS_ONLY and day.weekday() >= 5:
                day += timedelta(days=1)
                continue
            for m in slots:
                if not left:
                    break
                local = datetime.combine(day, time(m // 60, m % 60), tzinfo=tz) + stagger
                utc = local.astimezone(timezone.utc)
                if utc < now + LEAD:
                    continue
                rows.append({"clip": left.pop(0), "channel": ch.id, "name": ch.name, "platform": ch.platform,
                             "when": utc.isoformat(timespec="seconds"), "local": local.strftime("%Y-%m-%d %H:%M")})
            day += timedelta(days=1)
        if left:
            raise ScheduleError(f"That needs more than {MAX_DAYS} days. Choose fewer reels or more posts a day.")
    rows.sort(key=lambda r: (r["when"], r["channel"]))
    return rows


class SchedulePrefs:
    """A person's time zone and best times per platform: ``<data>/schedule/<owner>.json`` (``shared`` without accounts)."""

    def __init__(self, data_dir: str | Path, docs=None, mode: str = "files"):
        self.root = Path(data_dir) / "schedule"
        self._lock = threading.Lock()
        self.docs, self.mode = (docs, mode) if docs is not None else (None, "files")

    def _path(self, owner: str) -> Path:
        if owner != "shared" and not _OWNER.match(owner):
            raise KeyError(owner)
        return self.root / f"{owner}.json"

    def get(self, owner: str) -> dict:
        try:
            self._path(owner)                                # validates the owner
            text = self.docs.text("schedule", owner, "prefs") if self.mode == "db" else self._path(owner).read_text(encoding="utf-8")
            saved = json.loads(text) if text else {}
        except (OSError, ValueError):
            saved = {}
        tz = saved.get("timezone") if isinstance(saved, dict) else None
        try:
            zone(tz)
        except ScheduleError:
            tz = None
        had = bool(tz)
        times = {k: list(v) for k, v in BEST_TIMES.items()}
        for k, v in (saved.get("times", {}) if isinstance(saved, dict) else {}).items():
            if k in BEST_TIMES:
                try:
                    times[k] = clean_times(v)
                except ScheduleError:
                    pass
        return {"timezone": tz or DEFAULT_TZ, "times": times, "defaults": BEST_TIMES, "saved": had}

    def put(self, owner: str, tz: str, times: dict) -> dict:
        zone(tz)
        clean: dict[str, list[str]] = {}
        for k, v in (times or {}).items():
            if k not in BEST_TIMES:
                raise ScheduleError("That platform is not supported.")
            clean[k] = clean_times(v, k)
        text = json.dumps({"timezone": tz, "times": clean})
        with self._lock:
            if self.mode != "db":
                self.root.mkdir(parents=True, exist_ok=True)
                self._path(owner).write_text(text, encoding="utf-8")
            if self.mode != "files":
                self.docs.put("schedule", owner, "prefs", text)
        return self.get(owner)

    def delete_all(self, owner: str) -> None:
        self._path(owner).unlink(missing_ok=True)
        if self.mode != "files":
            self.docs.delete("schedule", owner)
