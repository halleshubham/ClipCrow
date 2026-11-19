"""Settings an operator can change from the Operator page, without a redeploy.

Precedence: what an operator set here > the environment variable or ``plans.json`` the server started with > the built-in default. The overrides live in
``<data>/settings.json`` (written atomically). **No secret is ever a setting**: API keys, tokens and passwords stay in the environment; the page only shows
whether each is set (``services``), never a value. Every value is validated (type, range, choices) before anything is saved, a bad value saves nothing, and
every change is kept in a short history (who, which key, old and new value).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

HISTORY = 50


@dataclass(frozen=True)
class Setting:
    key: str
    group: str
    label: str
    help: str
    kind: str                          # int | float | bool | text | enum | json
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    choices: tuple = ()
    unit: str = ""
    max_length: int = 400


GROUPS = [
    ("pricing", "Pricing", "What a minute of video costs, and what a recharge gives."),
    ("economics", "Margin and costs", "The target the Money section judges real prices against, and the rates used for costs."),
    ("access", "Sign-up and access", "Who can create an account and who can see this page."),
    ("limits", "Cost limits per person", "What one account may start. An operator is exempt."),
    ("retention", "Keeping files", "How long finished jobs and uploaded originals are kept."),
    ("gpu", "GPU workers and on-demand pods", "Where heavy work goes and what the rented GPU may spend."),
    ("alerts", "Alerts", "Who is mailed when something needs attention."),
    ("ai", "AI and processing", "The model used for planning and how many threads frame clips."),
]

SCHEMA: list[Setting] = [
    # --- pricing
    Setting("price_per_minute", "pricing", "Price per minute of video", "What one minute of uploaded video costs. Changing it never changes minutes already bought.", "float", 0.5, 10000, unit="₹"),
    Setting("min_job_minutes", "pricing", "Minimum charge per job", "The least any job is charged, however short the video: a tiny job costs the AI almost as much as a bigger one.", "int", 1, 600, unit="minutes"),
    Setting("signup_bonus_minutes", "pricing", "Free minutes for a new account", "Added once, when an address signs in for the first time. 0 turns the free trial off.", "float", 0, 10000, unit="minutes"),
    Setting("min_recharge", "pricing", "Smallest recharge", "The least a person can pay in one recharge.", "int", 10, 1_000_000, unit="₹"),
    Setting("max_recharge", "pricing", "Largest recharge", "The most a person can pay in one recharge.", "int", 10, 10_000_000, unit="₹"),
    Setting("tier1_from", "pricing", "Bonus tier 1: from", "A recharge of at least this much earns the bonus below. 0 turns the tier off.", "int", 0, 10_000_000, unit="₹"),
    Setting("tier1_percent", "pricing", "Bonus tier 1: extra minutes", "Extra minutes added on top, as a percent of what the recharge buys.", "int", 0, 100, unit="%"),
    Setting("tier2_from", "pricing", "Bonus tier 2: from", "A bigger recharge earns this bonus instead. 0 turns the tier off.", "int", 0, 10_000_000, unit="₹"),
    Setting("tier2_percent", "pricing", "Bonus tier 2: extra minutes", "Extra minutes for the bigger recharge.", "int", 0, 100, unit="%"),
    Setting("plan_name", "pricing", "Plan name shown to people", "The name on the Account page.", "text", max_length=40),
    # --- economics
    Setting("target_markup_percent", "economics", "Target markup on cost", "100 means the price is twice what a job costs to make. The Money section marks jobs below it and suggests a price.", "int", 0, 1000, unit="%"),
    Setting("usd_inr", "economics", "Rupees per US dollar", "Turns the AI's dollar prices into rupees in every cost.", "float", 1, 1000, unit="₹"),
    Setting("gpu_rates", "economics", "GPU rates (₹ per hour)", 'A JSON object of the hourly price of each machine, for example {"*": 45, "race-3090": 45, "home-gpu": 45}. "*" is the fallback.', "json", max_length=2000),
    # --- access
    Setting("signup_open", "access", "New sign-ups are open", "Off: only people who already have an account can sign in.", "bool"),
    Setting("signups_per_day", "access", "New accounts per day", "After this many new accounts today, new addresses get no code until tomorrow. Existing accounts still sign in. 0 means no limit.", "int", 0, 1_000_000),
    Setting("blocked_email_domains", "access", "Extra blocked mail domains", "Comma-separated domains refused at sign-up (throw-away mailboxes). A built-in list is always blocked too.", "text", max_length=1000),
    Setting("extra_admins", "access", "Other operators (emails)", "Comma-separated emails that can open this page, in addition to the CLIPCROW_ADMINS in the environment (which cannot be removed here).", "text", max_length=1000),
    # --- limits
    Setting("max_active_jobs", "limits", "Jobs running or waiting at once", "Per person. 0 means no limit.", "int", 0, 1000),
    Setting("max_jobs_per_day", "limits", "Jobs per 24 hours", "Per person. 0 means no limit.", "int", 0, 100000),
    Setting("max_job_minutes", "limits", "Longest video in one job", "Per job. 0 means no limit.", "int", 0, 100000, unit="minutes"),
    Setting("max_upload_mb", "limits", "Largest upload", "The biggest file accepted.", "int", 10, 200000, unit="MB"),
    # --- retention
    Setting("retention_days", "retention", "Keep finished jobs for", "Then they are deleted. 0 keeps them for ever. Applies to the plan people are on.", "int", 0, 3650, unit="days"),
    Setting("source_days", "retention", "Keep the uploaded original for", "The big original goes sooner than the clips; a clip can no longer be edited without it. 0 keeps it for ever.", "int", 0, 3650, unit="days"),
    # --- gpu
    Setting("gpu_fallback", "gpu", "If the chosen GPU cannot take a task", "Any: another connected worker may take it before the server does. Strict: a job never leaves the kind of GPU it chose.", "enum", choices=("any", "strict")),
    Setting("disabled_workers", "gpu", "Workers switched off", "Comma-separated worker names the server gives no work to (for example home-gpu).", "text", max_length=500),
    Setting("worker_pickup_seconds", "gpu", "How long a task waits for a worker", "Then it falls back. Waking a sleeping pod needs about 90 seconds.", "int", 10, 3600, unit="seconds"),
    Setting("pod_idle_minutes", "gpu", "Pause the pod after idle for", "A rented GPU bills while it is awake, so it is paused after this many minutes with no work.", "int", 1, 240, unit="minutes"),
    Setting("pod_max_daily_inr", "gpu", "Most the pods may spend in a day", "Beyond it the pod is paused and not woken until tomorrow. 0 means no limit.", "float", 0, 1_000_000, unit="₹"),
    # --- alerts
    Setting("alert_email", "alerts", "Send alerts to", "Disk nearly full, jobs stuck, repeated failures, GPU tasks nobody can take, mail failing. Empty turns alerts off.", "text", max_length=200),
    # --- ai
    Setting("model", "ai", "Planning model", "The model that picks the moments and writes titles. Applies to the next job.", "text", max_length=80),
    Setting("framing_threads", "ai", "Clips framed in parallel", "Threads used to find the speaker in each clip. More is faster on a bigger server.", "int", 1, 16),
]

BY_KEY = {s.key: s for s in SCHEMA}
EMAIL = re.compile(r"^[^@\s,;<>]{1,64}@[^@\s,;<>]{1,190}\.[^@\s,;<>]{2,24}\Z")
DOMAIN = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+\Z")
WORKER = re.compile(r"^[A-Za-z0-9._-]{1,60}\Z")


def coerce(s: Setting, raw: Any) -> Any:
    """The validated value, or a ValueError with a sentence for the operator."""
    if s.kind == "bool":
        if isinstance(raw, bool):
            return raw
        if str(raw).strip().lower() in ("1", "true", "yes", "on"):
            return True
        if str(raw).strip().lower() in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"{s.label}: yes or no")
    if s.kind in ("int", "float"):
        if isinstance(raw, bool) or raw is None or str(raw).strip() == "":
            raise ValueError(f"{s.label}: enter a number")
        try:
            v = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{s.label}: enter a number") from None
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError(f"{s.label}: enter a number")
        if s.kind == "int":
            if v != int(v):
                raise ValueError(f"{s.label}: a whole number")
            v = int(v)
        if s.minimum is not None and v < s.minimum or s.maximum is not None and v > s.maximum:
            raise ValueError(f"{s.label}: between {s.minimum:g} and {s.maximum:g}")
        return v
    text = "" if raw is None else str(raw).strip()
    if len(text) > s.max_length:
        raise ValueError(f"{s.label}: at most {s.max_length} characters")
    if re.search(r"[\x00-\x08\x0b-\x1f]", text):
        raise ValueError(f"{s.label}: plain text only")
    if s.kind == "enum":
        if text not in s.choices:
            raise ValueError(f"{s.label}: one of {', '.join(s.choices)}")
        return text
    if s.kind == "json":
        if not text:
            return ""
        try:
            data = json.loads(text)
        except ValueError:
            raise ValueError(f"{s.label}: not valid JSON") from None
        if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 for k, v in data.items()):
            raise ValueError(f"{s.label}: an object of names and non-negative numbers")
        return json.dumps(data, sort_keys=True)
    # text
    if s.key == "alert_email":
        if text and not EMAIL.match(text):
            raise ValueError(f"{s.label}: not an email address")
    elif s.key in ("blocked_email_domains",):
        parts = [p.strip().lower() for p in text.split(",") if p.strip()]
        if not all(DOMAIN.match(p) for p in parts):
            raise ValueError(f"{s.label}: domains like example.com, separated by commas")
        text = ", ".join(parts)
    elif s.key == "extra_admins":
        parts = [p.strip().lower() for p in text.split(",") if p.strip()]
        if not all(EMAIL.match(p) for p in parts):
            raise ValueError(f"{s.label}: email addresses separated by commas")
        text = ", ".join(parts)
    elif s.key == "disabled_workers":
        parts = [p.strip() for p in text.split(",") if p.strip()]
        if not all(WORKER.match(p) for p in parts):
            raise ValueError(f"{s.label}: worker names separated by commas")
        text = ", ".join(parts)
    elif s.key == "model" and text and not re.fullmatch(r"[A-Za-z0-9._:/\[\]-]{1,80}", text):
        raise ValueError(f"{s.label}: a model name")
    return text


def cross_check(values: dict) -> None:
    """Rules between settings (checked on the whole set that would result)."""
    if values["min_recharge"] > values["max_recharge"]:
        raise ValueError("Smallest recharge must not be more than the largest")
    for n in ("1", "2"):
        if (values[f"tier{n}_from"] > 0) != (values[f"tier{n}_percent"] > 0) and (values[f"tier{n}_from"] or values[f"tier{n}_percent"]):
            raise ValueError(f"Bonus tier {n}: set both the amount and the percent, or neither")
    if values["tier1_from"] and values["tier2_from"] and values["tier2_from"] <= values["tier1_from"]:
        raise ValueError("Bonus tier 2 must start at a bigger amount than tier 1")
    if values["max_recharge"] < values["price_per_minute"]:
        raise ValueError("The largest recharge must buy at least one minute")


class SettingsStore:
    """The operator's overrides, on disk. ``base`` is what the server started with (environment, plans.json, defaults), by key."""

    def __init__(self, path: str | Path, base: dict):
        self.path, self.base = Path(path), dict(base)
        self._lock = threading.RLock()
        self._data = {"values": {}, "history": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for k, v in (data.get("values") or {}).items():
                if k in BY_KEY:
                    try:
                        self._data["values"][k] = coerce(BY_KEY[k], v)           # a hand-edited or old file cannot put a bad value in
                    except ValueError:
                        continue
            self._data["history"] = [h for h in (data.get("history") or []) if isinstance(h, dict)][-HISTORY:]
        except (OSError, ValueError):
            pass

    def values(self) -> dict:
        with self._lock:
            return {k: self._data["values"].get(k, self.base[k]) for k in BY_KEY}

    def overridden(self) -> set:
        with self._lock:
            return set(self._data["values"])

    def history(self) -> list:
        with self._lock:
            return list(reversed(self._data["history"]))[:20]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".settings-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def update(self, changes: dict, who: str) -> dict:
        """Validate every change and the result as a whole; save only if all of it is good. Returns the values after."""
        with self._lock:
            new = {}
            for k, raw in changes.items():
                if k not in BY_KEY:
                    raise ValueError(f"Unknown setting: {k[:40]}")
                new[k] = coerce(BY_KEY[k], raw)
            merged = {**self.values(), **new}
            cross_check(merged)
            now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
            for k, v in new.items():
                old = merged_old = self.values()[k]
                if v == merged_old and k in self._data["values"]:
                    continue
                self._data["history"].append({"at": now, "by": who[:8], "key": k, "old": old, "new": v})
                self._data["values"][k] = v
            del self._data["history"][:-HISTORY]
            self._save()
            return self.values()

    def reset(self, key: str, who: str) -> dict:
        with self._lock:
            if key not in BY_KEY:
                raise ValueError("Unknown setting")
            if key in self._data["values"]:
                old = self._data["values"].pop(key)
                self._data["history"].append({"at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), "by": who[:8], "key": key, "old": old, "new": self.base[key]})
                del self._data["history"][:-HISTORY]
                self._save()
            return self.values()

    def view(self) -> list[dict]:
        vals, over = self.values(), self.overridden()
        return [{"key": s.key, "group": s.group, "label": s.label, "help": s.help, "kind": s.kind, "unit": s.unit, "min": s.minimum, "max": s.maximum,
                 "choices": list(s.choices), "value": vals[s.key], "default": self.base[s.key], "source": "operator" if s.key in over else "default"} for s in SCHEMA]
