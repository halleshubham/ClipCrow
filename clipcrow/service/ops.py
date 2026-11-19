"""Running ClipCrow as a service: per-user cost limits, the operator's view, and an alert watchdog.

Pure functions where possible (so they are tested without a clock or a disk); the wiring is in ``api.py``. Nothing here prints or mails a secret: alerts
say what is wrong in words and carry counts, never an address, a token or a file name.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

log = logging.getLogger("clipcrow.ops")

# a short list of throw-away mailbox domains: a sign-up with one of these asks for a permanent address. Extend with CLIPCROW_BLOCKED_EMAIL_DOMAINS.
DISPOSABLE = frozenset("""mailinator.com guerrillamail.com guerrillamail.net sharklasers.com 10minutemail.com 10minutemail.net tempmail.com temp-mail.org
yopmail.com yopmail.fr trashmail.com getnada.com dispostable.com throwawaymail.com maildrop.cc fakeinbox.com mintemail.com mohmal.com emailondeck.com
tempail.com burnermail.io moakt.com discard.email spamgourmet.com mailnesia.com""".split())


@dataclass(frozen=True)
class Limits:
    """Per-person limits that keep one account from costing the operator money (all from the environment, all overridable)."""
    active_jobs: int = 3                 # queued plus running at once
    jobs_per_day: int = 30               # jobs started in the last 24 hours
    job_minutes: int = 240               # source minutes in one job
    signups_per_day: int = 200           # new accounts a day; beyond it only existing accounts sign in

    @classmethod
    def from_env(cls) -> "Limits":
        def num(name: str, default: int) -> int:
            try:
                return max(0, int(os.environ.get(name, "") or default))
            except ValueError:
                return default
        return cls(num("CLIPCROW_MAX_ACTIVE_JOBS", cls.active_jobs), num("CLIPCROW_MAX_JOBS_PER_DAY", cls.jobs_per_day),
                   num("CLIPCROW_MAX_JOB_MINUTES", cls.job_minutes), num("CLIPCROW_MAX_SIGNUPS_PER_DAY", cls.signups_per_day))


def blocked_domains() -> frozenset:
    extra = {d.strip().lower() for d in (os.environ.get("CLIPCROW_BLOCKED_EMAIL_DOMAINS") or "").split(",") if d.strip()}
    return DISPOSABLE | extra


def is_throwaway(email: str) -> bool:
    domain = (email or "").strip().lower().rsplit("@", 1)[-1]
    bad = blocked_domains()
    return domain in bad or any(domain.endswith("." + d) for d in bad)


def job_refusal(limits: Limits, *, active: int, today: int, minutes: float) -> Optional[tuple[int, str]]:
    """(status code, message for the person) when a new job must be refused, else None. 0 disables a limit."""
    if limits.job_minutes and minutes > limits.job_minutes:
        return 413, f"This video is {minutes:.0f} min long; one job can be at most {limits.job_minutes} min. Split it into parts."
    if limits.active_jobs and active >= limits.active_jobs:
        return 429, f"You already have {active} jobs running or waiting. Wait for one to finish, then start the next."
    if limits.jobs_per_day and today >= limits.jobs_per_day:
        return 429, f"You have started {today} jobs in the last 24 hours, which is the daily limit. Try again tomorrow, or write to support@shackyapps.in."
    return None


def admin_emails() -> frozenset:
    return frozenset(e.strip().lower() for e in (os.environ.get("CLIPCROW_ADMINS") or "").split(",") if e.strip())


# ---- health and alerts -------------------------------------------------------------------------------------------------------------------------

@dataclass
class Snapshot:
    disk_free_gb: float = 999.0
    disk_free_pct: float = 100.0
    queued: int = 0
    running: int = 0
    oldest_queued_s: float = 0.0           # how long the oldest waiting job has waited
    failed_last_hour: int = 0
    unserved_tasks: int = 0                # GPU-worker tasks nobody can take
    mail_failures_hour: int = 0


def health_alerts(s: Snapshot, *, now_running_stuck_s: float = 900.0) -> list[tuple[str, str]]:
    """(key, message) for every condition the operator should hear about. The key is stable so the watchdog can send each at most once an hour."""
    out: list[tuple[str, str]] = []
    if s.disk_free_gb < 5 or s.disk_free_pct < 10:
        out.append(("disk", f"The data disk is nearly full: {s.disk_free_gb:.1f} GB ({s.disk_free_pct:.0f}%) free. Jobs will start to fail."))
    if s.queued and not s.running and s.oldest_queued_s > now_running_stuck_s:
        out.append(("queue", f"{s.queued} job(s) are waiting and nothing is running; the oldest has waited {s.oldest_queued_s / 60:.0f} min. The runner may be stuck."))
    if s.failed_last_hour >= 5:
        out.append(("failures", f"{s.failed_last_hour} jobs failed in the last hour."))
    if s.unserved_tasks:
        out.append(("workers", f"{s.unserved_tasks} GPU task(s) are waiting and no worker can take them."))
    if s.mail_failures_hour >= 3:
        out.append(("mail", f"{s.mail_failures_hour} mails could not be sent in the last hour. People may not get their sign-in codes."))
    return out


def disk_snapshot(path: str) -> tuple[float, float]:
    u = shutil.disk_usage(path)
    return u.free / 2 ** 30, 100.0 * u.free / max(u.total, 1)


class Watchdog:
    """Every minute: gather a Snapshot, mail the operator about each new problem (at most once an hour per problem) and again when it clears."""

    def __init__(self, snapshot: Callable[[], Snapshot], send: Callable[[str, str], None], *, interval: float = 60.0, repeat_s: float = 3600.0,
                 clock: Callable[[], float] = time.time):
        self.snapshot, self.send, self.interval, self.repeat_s, self.clock = snapshot, send, interval, repeat_s, clock
        self._last: dict[str, float] = {}
        self._stop = threading.Event()

    def check(self) -> list[tuple[str, str]]:
        try:
            alerts = health_alerts(self.snapshot())
        except Exception:                                              # noqa: BLE001 - a broken check must not stop the watchdog
            log.exception("watchdog: could not gather a snapshot")
            return []
        now = self.clock()
        keys = {k for k, _ in alerts}
        sent = []
        for key, message in alerts:
            if now - self._last.get(key, -1e18) >= self.repeat_s:
                self._last[key] = now
                sent.append((key, message))
                self.send(f"ClipCrow alert: {key}", message)
        for key in [k for k in self._last if k not in keys]:           # cleared
            del self._last[key]
            self.send(f"ClipCrow recovered: {key}", f"The {key} problem has cleared.")
        return sent

    def start(self) -> None:
        def run() -> None:
            while not self._stop.wait(self.interval):
                self.check()
        threading.Thread(target=run, name="clipcrow-watchdog", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


def jobs_since(jobs: Iterable, hours: float, owner: Optional[str] = None, statuses: Optional[tuple] = None) -> int:
    cut = datetime.now(timezone.utc) - timedelta(hours=hours)
    n = 0
    for j in jobs:
        if owner is not None and j.owner != owner:
            continue
        if statuses is not None and j.status not in statuses:
            continue
        try:
            made = datetime.fromisoformat(j.created_at)
        except ValueError:
            continue
        if made.tzinfo is None:
            made = made.replace(tzinfo=timezone.utc)
        if made >= cut:
            n += 1
    return n
