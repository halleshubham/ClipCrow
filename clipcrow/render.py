"""Thin, pinned wrapper around the HyperFrames CLI.

The CLI version is pinned: a different Chrome/CLI build changes pixels, and the
composition contract changes between releases. Override the command with
$CLIPCROW_HF_BIN (e.g. a globally installed binary) to skip npx start-up.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

HYPERFRAMES_VERSION = "0.9.29"


class RenderError(RuntimeError):
    pass


def _hf_cmd() -> list[str]:
    custom = os.environ.get("CLIPCROW_HF_BIN")
    if custom:
        return shlex.split(custom)
    return ["npx", "--yes", f"hyperframes@{HYPERFRAMES_VERSION}"]


def _run(args: list[str], cwd: Path, timeout: int, env: dict | None = None):
    full_env = {**os.environ, **(env or {})}
    try:
        return subprocess.run([*_hf_cmd(), *args], cwd=cwd, capture_output=True, text=True,
                              timeout=timeout, env=full_env)
    except subprocess.TimeoutExpired as e:
        raise RenderError(f"hyperframes {args[0]} timed out after {timeout}s") from e
    except FileNotFoundError as e:
        raise RenderError("npx/hyperframes not found; install Node 22+ and FFmpeg") from e


def _tail(text: str, n: int = 12) -> str:
    return "\n".join(x for x in text.strip().splitlines()[-n:])


def check(project: str | Path, timeout: int = 180) -> dict:
    """`hyperframes check --json`: lint + layout + contrast + audio. Returns the report."""
    r = _run(["check", "--json"], Path(project), timeout)
    try:
        report = json.loads(r.stdout)
    except ValueError:
        raise RenderError(f"hyperframes check gave no JSON:\n{_tail(r.stderr or r.stdout)}")
    return report


def check_problems(report: dict) -> list[str]:
    """Human-readable error findings from a check report (warnings are not errors)."""
    out = []
    for section in ("lint", "runtime", "layout", "contrast", "audio"):
        s = report.get(section) or {}
        for f in s.get("findings", []):
            if f.get("severity", "error") == "error":
                out.append(f"{section}: {f.get('message') or f.get('code') or f}")
    return out


def render(project: str | Path, output: str | Path, *, fmt: str = "mp4", quality: str = "standard",
           workers: int | None = None, browser_path: str | None = None, timeout: int = 1800) -> Path:
    project, output = Path(project), Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    args = ["render", "--format", fmt, "--quality", quality, "--output", str(output), "--quiet"]
    if workers:
        args += ["--workers", str(workers)]
    env = {"HYPERFRAMES_BROWSER_PATH": browser_path} if browser_path else None
    r = _run(args, project, timeout, env)
    if r.returncode != 0 or not output.is_file():
        raise RenderError(f"render failed (exit {r.returncode}):\n{_tail(r.stderr or r.stdout)}")
    return output
