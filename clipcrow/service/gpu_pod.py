"""A rented GPU pod that sleeps when there is nothing to do and wakes when a job needs it (pause / resume per job execution).

A paused pod is not billed for compute; a resume starts a NEW container (only ``/workspace`` survives), so after waking the controller runs the
boot command on the pod (``post_resume.sh`` then ``start_worker.sh``, see ``scripts/rented-gpu/``) through the pod's Jupyter terminal, and the
worker there starts polling this server again.

Rules: the API key and the pod's address (its Jupyter URL carries a token) come from the environment or the provider and are never logged or sent
to a browser; a day's spend is capped (``CLIPCROW_POD_MAX_DAILY_INR``); the controller never terminates a pod; every network piece is injected so the
logic is tested without any network.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("clipcrow.pod")

API_BASE = "https://api.raceengineering.ai/v1/gpu/"
DEFAULT_BOOT = "bash /workspace/post_resume.sh; bash /workspace/start_worker.sh"
CAN = ("transcribe", "render", "speakers")


class PodError(RuntimeError):
    """A sentence safe to log (never contains the key or the pod's address)."""


class RaceClient:
    """The few calls the controller needs: list, resume, pause, resume-status. ``http`` is injectable (method, path, body) -> dict."""

    def __init__(self, api_key: str, base: str = API_BASE, http: Optional[Callable] = None, timeout: float = 40.0):
        self._key, self.base, self.timeout = api_key, base, timeout
        self._http = http or self._real

    def _real(self, method: str, path: str, body: Optional[dict]) -> dict:
        req = urllib.request.Request(self.base + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json", "Accept": "application/json",
                                              "User-Agent": "clipcrow-pod/1", "Idempotency-Key": uuid.uuid4().hex})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise PodError(f"the GPU provider answered {e.code} to {path.split('?')[0]}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise PodError(f"the GPU provider could not be reached ({type(e).__name__})") from None

    def status(self, pod_id: str) -> str:
        """``"running"`` or whatever the provider calls the other states (paused, stopped ...); ``"missing"`` when the pod is not on the account."""
        for p in (self._http("GET", "pod-list/", None).get("data") or []):
            if isinstance(p, dict) and str(p.get("id")) == pod_id:
                return str(p.get("status") or "unknown").lower()
        return "missing"

    def resume(self, pod_id: str, wait: float = 240.0, sleep: Callable[[float], None] = time.sleep) -> str:
        """Resume and wait until it is ready. Returns the pod's web address (secret: it carries a token)."""
        r = self._http("POST", "resume-pod/", {"pod_id": pod_id})
        deadline = time.monotonic() + wait
        while True:
            if r.get("web_url"):
                return str(r["web_url"])
            if str(r.get("status")) == "failed" or str(r.get("deploy_state") or r.get("resume_state")) == "failed":
                raise PodError("the pod would not resume")
            if time.monotonic() > deadline:
                raise PodError("the pod did not become ready in time")
            sleep(3.0)
            r = self._http("GET", f"resume-status/?pod_id={pod_id}", None)

    def pause(self, pod_id: str) -> None:
        r = self._http("POST", "pause-pod/", {"pod_id": pod_id})
        if str(r.get("status")) not in ("success", "ok"):
            raise PodError("the pod could not be paused")


def _open_terminal(base: str, token: str, wait: float, sleep: Callable[[float], None], opener, clock: Callable[[], float]) -> str:
    """A new terminal on the pod's Jupyter. A freshly resumed pod answers 404/502/503 (or refuses the connection) for a while before Jupyter is up, so this asks again
    every few seconds for up to ``wait`` seconds."""
    hdr = {"Authorization": f"token {token}"}
    end, last = clock() + wait, "no answer"
    while True:
        try:
            r = urllib.request.Request(base + "/api/terminals", method="POST", headers=hdr)
            with opener(r, timeout=20) as resp:
                return json.loads(resp.read())["name"]
        except urllib.error.HTTPError as e:
            if e.code not in (404, 408, 425, 429, 500, 502, 503, 504):
                raise PodError(f"the pod's Jupyter answered {e.code}") from None
            last = f"HTTP {e.code}"
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            last = type(e).__name__
        if clock() >= end:
            raise PodError(f"the pod's Jupyter did not come up in time ({last})")
        sleep(5.0)


def jupyter_run(web_url: str, command: str, timeout: float = 180.0, *, wait_ready: float = 150.0, sleep: Callable[[float], None] = time.sleep,
                opener=urllib.request.urlopen, connect=None, clock: Callable[[], float] = time.monotonic) -> str:
    """Run ``command`` in a terminal of the pod's Jupyter and return its output (waiting up to ``wait_ready`` seconds for a just-resumed pod's Jupyter).
    Needs ``websocket-client``."""
    if connect is None:
        import websocket                                              # noqa: PLC0415  (only needed when a pod is configured)
        connect = websocket.create_connection
    u = urlparse(web_url)
    token = (parse_qs(u.query).get("token") or [""])[0]
    base = f"{u.scheme}://{u.netloc}"
    term = _open_terminal(base, token, wait_ready, sleep, opener, clock)
    ws = connect(base.replace("http", "ws", 1) + f"/terminals/websocket/{term}?token={token}", timeout=timeout)
    tag = uuid.uuid4().hex[:8]
    ws.send(json.dumps(["stdin", f"({command}) 2>&1; echo __EN''D_{tag}__ $?\n"]))
    out, end = "", time.time() + timeout
    try:
        while time.time() < end and f"__END_{tag}__" not in out:
            m = json.loads(ws.recv())
            if m and m[0] == "stdout":
                out += m[1]
    finally:
        ws.close()
    return out.split(f"__END_{tag}__")[0]


class PodController:
    RETRY_SECONDS = 40.0                            # wait this long before asking a provider with no free GPU again
    MAX_ATTEMPTS = 6                                # then the job goes on without it (the server does the work)
    WORKER_GRACE = 150.0                            # after the boot command the worker needs a little while to connect: a job keeps waiting for it

    def __init__(self, client, hub, pod_id: str, *, hourly: float = 0.0, idle_seconds: float = 600.0, max_daily: float = 500.0,
                 fallbacks: tuple = (), boot: Optional[Callable[[str], None]] = None, can: tuple = CAN, clock: Callable[[], float] = time.monotonic,
                 today: Callable[[], str] = lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d"), threaded: bool = True):
        self.client, self.hub, self.pod_id = client, hub, pod_id
        self.hourly, self.idle_seconds, self.max_daily = max(0.0, hourly), idle_seconds, max_daily
        self.pods = [(pod_id, self.hourly)] + [(str(i), max(0.0, float(h))) for i, h in fallbacks]      # in order of preference: the next is tried when one will not wake
        self.boot, self.can, self._clock, self._today, self.threaded = boot, tuple(can), clock, today, threaded
        self.state = "unknown"                      # unknown | asleep | waking | awake
        self.error = ""
        self._awake_since = 0.0
        self._booted_at = -1e9                          # when the pod's worker was last started
        self._retry_at, self._attempts = 0.0, 0         # a provider with no free GPU right now is asked again, a few times
        self._last_busy = clock()
        self._spent_day, self._spent = today(), 0.0
        self._lock = threading.RLock()
        self._stop = threading.Event()
        hub.standby = self.can_wake
        hub.why_unavailable = lambda: self.error

    # what the hub asks ------------------------------------------------------------------------------------------------
    def can_wake(self, kind: Optional[str], direct: bool = False) -> bool:
        """A job may be queued for the pod while it sleeps, is waking, or has just woken and its worker has not connected yet (``WORKER_GRACE``)."""
        with self._lock:
            up = self.state in ("asleep", "waking") or (self.state == "awake" and self._clock() - self._booted_at < self.WORKER_GRACE)
            return up and (kind is None or kind in self.can) and not self._over_budget()

    def _over_budget(self) -> bool:
        return self.max_daily > 0 and self.hourly > 0 and self._spent_total() >= self.max_daily

    def _spent_total(self) -> float:
        now = self._clock()
        live = (now - self._awake_since) / 3600.0 * self.hourly if self.state in ("waking", "awake") and self._awake_since else 0.0
        return self._spent + live

    def status(self) -> dict:
        with self._lock:
            return {"state": self.state, "spent_today": round(self._spent_total(), 2), "daily_cap": self.max_daily, "currency": "INR",
                    "error": self.error}

    # the loop ---------------------------------------------------------------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="gpu-pod").start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(5.0):
            try:
                self.tick()
            except Exception:                                          # noqa: BLE001  (the controller must keep running)
                log.exception("pod controller step failed")

    def _roll_day(self) -> None:
        d = self._today()
        if d != self._spent_day:                                       # a new day starts with a fresh budget
            self._spent_day, self._spent = d, 0.0
            if self.state in ("waking", "awake"):
                self._awake_since = self._clock()

    def tick(self) -> None:
        with self._lock:
            self._roll_day()
            if self.state == "unknown":
                try:
                    states = {pid: self.client.status(pid) for pid, _ in self.pods}
                except PodError as e:
                    self.error = str(e)
                    return
                running = next(((pid, hr) for pid, hr in self.pods if states.get(pid) == "running"), None)
                if running is not None:                                # a pod that is already up (adopted, whichever it is)
                    (self.pod_id, self.hourly), self.state, self._awake_since = running, "awake", self._clock()
                elif all(s == "missing" for s in states.values()):
                    self.error = "the pod is not on this account"
                    return
                else:
                    self.state = "asleep"
            load, unserved = self.hub.load(), self.hub.unserved()
            busy = bool(unserved) or load["pending"] or load["leased"]
            now = self._clock()
            if busy:
                self._last_busy = now
            if self.state == "awake" and unserved and now - self._awake_since > 180 and max(u["age"] for u in unserved) > 120:
                self.state = "unknown"                                  # it should be serving but nobody answers: ask the provider what it is
                return
            if not unserved:
                self._attempts = 0
            if self.state == "asleep" and unserved and now >= self._retry_at and not self._over_budget() and any(u["kind"] in self.can and u.get("pool") != "home" for u in unserved):
                self._wake()
            elif self.state == "awake" and not busy and (now - self._last_busy >= self.idle_seconds or self._over_budget()):
                self._sleep()

    def _wake(self) -> None:
        self.state, self._awake_since = "waking", self._clock()
        log.info("waking the GPU pod for a waiting job")
        if self.threaded:
            threading.Thread(target=self._wake_work, daemon=True).start()
        else:
            self._wake_work()

    def _wake_work(self) -> None:
        try:
            last: Optional[Exception] = None
            for pid, hr in list(self.pods):                            # the preferred pod first; the next one when it has no free GPU or will not start
                with self._lock:
                    self.pod_id, self.hourly, self._awake_since = pid, hr, self._clock()
                try:
                    url = self.client.resume(pid)
                except Exception as e:                                 # noqa: BLE001
                    last = e
                    log.warning("pod %s would not wake: %s", pid, e)
                    continue
                try:
                    if self.boot is not None:
                        self.boot(url)
                except Exception as e:                                 # noqa: BLE001
                    last = e
                    log.warning("pod %s woke but its worker did not start: %s", pid, type(e).__name__)
                    try:
                        self.client.pause(pid)                         # do not leave a pod running that serves nothing
                    except Exception:                                  # noqa: BLE001
                        pass
                    continue
                with self._lock:
                    self.state, self._last_busy, self.error, self._booted_at = "awake", self._clock(), "", self._clock()
                log.info("the GPU pod is awake")
                return
            with self._lock:
                self.pod_id, self.hourly = self.pods[0]
            raise last if last is not None else PodError("no pod is configured")
        except Exception as e:                                         # noqa: BLE001
            with self._lock:
                self.error = str(e) if isinstance(e, PodError) else f"waking the pod failed ({type(e).__name__}: {str(e)[:80]})"
                self._spent += (self._clock() - self._awake_since) / 3600.0 * self.hourly
                self._attempts += 1
                self._awake_since = 0.0
                if "503" in self.error and self._attempts < self.MAX_ATTEMPTS:      # no free GPU right now: stay asleep, ask again soon
                    self.state, self._retry_at = "asleep", self._clock() + self.RETRY_SECONDS
                else:
                    self.state = "unknown"                              # look at the provider again before trying anything else
            log.warning("waking the GPU pod failed: %s", self.error)

    def _sleep(self) -> None:
        try:
            self.client.pause(self.pod_id)
        except PodError as e:
            self.error = str(e)
            log.warning("pausing the GPU pod failed: %s", e)
            return
        self._spent += (self._clock() - self._awake_since) / 3600.0 * self.hourly
        self.state, self._awake_since, self.error = "asleep", 0.0, ""
        log.info("the GPU pod is paused (nothing to do)")


def from_env(hub, env=os.environ) -> Optional[PodController]:
    """A controller when ``RACE_GPU_API_KEY`` and ``RACE_POD_ID`` (or ``RACE_POD_IDS``) are set, else None (no pod, nothing changes).
    ``RACE_POD_IDS`` lists pods in order of preference, each ``id`` or ``id:hourly-INR`` (e.g. ``7401:45,8123:91``): the next is tried when one has no free GPU."""
    key = (env.get("RACE_GPU_API_KEY") or "").strip()
    default_rate = 45.0
    try:
        default_rate = max(0.0, float(env.get("CLIPCROW_POD_HOURLY") or 45.0))
    except ValueError:
        pass
    pods: list[tuple[str, float]] = []
    for part in (env.get("RACE_POD_IDS") or env.get("RACE_POD_ID") or "").split(","):
        pid, _, rate = part.strip().partition(":")
        if pid.isdigit() or (pid and pid.isalnum()):
            try:
                pods.append((pid, max(0.0, float(rate)) if rate else default_rate))
            except ValueError:
                pods.append((pid, default_rate))
    if not key or not pods:
        return None
    pod = pods[0][0]
    command = env.get("CLIPCROW_POD_BOOT") or DEFAULT_BOOT

    def boot(url: str) -> None:
        jupyter_run(url, command)

    def num(name: str, default: float) -> float:
        try:
            return max(0.0, float(env.get(name) or default))
        except ValueError:
            return default
    ctl = PodController(RaceClient(key), hub, pod, hourly=pods[0][1], fallbacks=tuple(pods[1:]), idle_seconds=num("CLIPCROW_POD_IDLE_MINUTES", 10.0) * 60,
                        max_daily=num("CLIPCROW_POD_MAX_DAILY_INR", 500.0), boot=boot)
    return ctl
