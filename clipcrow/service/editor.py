"""Editing one clip after the AI planned it.

An edit is a small, closed set of fields (``ClipEdit``), never a raw EDL: a person can change the title, the
hook, the lower third, a stat, the captions, the cut points and how the picture is framed, and nothing else.
``apply_edit`` is a pure function: it returns a new, fully validated EDL, or raises ``ValueError`` with a
message written for a person. Slow work (re-planning the framing, rendering) happens afterwards, in the job
runner, so a request never waits for it.

Honesty: the AI's own decisions stay in ``clip.ai.decided``; what a person changed is added to
``clip.ai.notes`` ("edited by a person: ..."), so the usage record never claims the AI wrote a line it did not.
"""
from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .. import fonts
from ..edl import EDL, Clip, HexColor
from ..punch import plan_punch_ins, sentence_spans

MIN_SECONDS, MAX_SECONDS = 3.0, 90.0
WORD_CONTEXT = 6.0          # seconds of transcript shown around the clip for picking cut points


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CaptionsEdit(_M):
    enabled: Optional[bool] = None
    max_words: Optional[int] = Field(default=None, ge=1, le=8)
    color: Optional[HexColor] = None
    highlight_color: Optional[HexColor] = None
    top_px: Optional[int] = Field(default=None, ge=0, le=1800)
    font_family: Optional[str] = None


class ClipEdit(_M):
    """``None`` leaves a field alone; for the text fields ``""`` removes the overlay."""
    title: Optional[str] = Field(default=None, max_length=120)
    source_in: Optional[float] = Field(default=None, ge=0)
    source_out: Optional[float] = Field(default=None, gt=0)
    captions: Optional[CaptionsEdit] = None
    hook: Optional[str] = Field(default=None, max_length=120)
    speaker_name: Optional[str] = Field(default=None, max_length=60)
    speaker_title: Optional[str] = Field(default=None, max_length=60)
    stat: Optional[str] = Field(default=None, max_length=40)
    framing: Optional[Literal["auto", "cover", "fit_blur"]] = None
    multi: Optional[Literal["switch", "split", "off"]] = None
    punch_ins: Optional[bool] = None            # slow push-ins at sentence starts: on or off
    drop_pictures: Optional[list[str]] = Field(default=None, max_length=8)   # ids of picture overlays to remove


def _first_line(e: ValidationError) -> str:
    err = e.errors()[0]
    where = ".".join(str(x) for x in err["loc"] if x not in ("clips",))
    return f"{where}: {err['msg']}" if where else err["msg"]


def _overlay(overlays: list[dict], kind: str) -> Optional[dict]:
    return next((o for o in overlays if o["kind"] == kind), None)


def _set_text(overlays: list[dict], kind: str, field: str, value: str, new: dict) -> bool:
    """Set/add/remove a one-text overlay. Returns whether anything changed."""
    o = _overlay(overlays, kind)
    value = value.strip()
    if not value:
        if o is None:
            return False
        overlays.remove(o)
        return True
    if o is None:
        overlays.append({**new, field: value})
        return True
    if o[field] == value:
        return False
    o[field] = value
    return True


def apply_edit(edl: EDL, clip_id: str, edit: ClipEdit, *, source_duration: float) -> tuple[EDL, Optional[str]]:
    """The edited EDL and, when the framing must be planned again, the multi-speaker mode to plan with.

    Framing is re-planned when a person asks for ``auto`` (or a ``multi`` mode), or when they move the cut
    points of a clip whose framing followed faces: the old keyframes belong to the old window."""
    clip = next((c for c in edl.clips if c.id == clip_id), None)
    if clip is None:
        raise ValueError("No such clip.")
    d = clip.model_dump(exclude_none=True)
    changed: list[str] = []

    if edit.title is not None and edit.title.strip() != clip.title:
        d["title"] = edit.title.strip()
        changed.append("title")

    t_in = clip.source_in if edit.source_in is None else edit.source_in
    t_out = clip.source_out if edit.source_out is None else edit.source_out
    trimmed = (t_in, t_out) != (clip.source_in, clip.source_out)
    if trimmed:
        if t_out - t_in < MIN_SECONDS:
            raise ValueError(f"A clip needs at least {MIN_SECONDS:.0f} seconds.")
        if t_out - t_in > MAX_SECONDS:
            raise ValueError(f"A clip can be at most {MAX_SECONDS:.0f} seconds.")
        if t_out > source_duration + 0.05:
            raise ValueError(f"The end is past the end of the video ({source_duration:.0f} s).")
        d["source_in"], d["source_out"] = round(t_in, 3), round(t_out, 3)
        changed.append("cut points")
        # overlays that would appear after the new end are dropped; word-anchored ones keep their anchor
        d["overlays"] = [o for o in d["overlays"] if o.get("t_in") is None or o["t_in"] < (t_out - t_in) - 0.3]
        shift = t_in - clip.source_in                            # punch-in times are clip time: they move with the start
        d["layout"]["punch_ins"] = [{**p, "t": round(p["t"] - shift, 3)} for p in d["layout"].get("punch_ins", [])
                                    if p["t"] - shift >= 0 and p["t"] - shift + p["duration"] <= (t_out - t_in) - 0.3]

    if edit.captions is not None:
        cap = d["captions"]
        for name, value in edit.captions.model_dump(exclude_none=True).items():
            if name == "font_family" and value not in fonts.KNOWN:
                raise ValueError(f"Unknown caption font; choose one of: {', '.join(sorted(fonts.KNOWN))}.")
            if cap.get(name) != value:
                cap[name] = value
                if "captions" not in changed:
                    changed.append("captions")

    ov = d["overlays"]
    if edit.hook is not None and _set_text(ov, "hook", "text", edit.hook, {"kind": "hook", "id": "hook", "t_in": 0.3, "duration": 2.5}):
        changed.append("hook")
    if edit.stat is not None and _set_text(ov, "stat_pill", "text", edit.stat, {"kind": "stat_pill", "id": "stat", "t_in": 1.0, "duration": 2.5}):
        changed.append("stat")
    if edit.speaker_name is not None or edit.speaker_title is not None:
        lt = _overlay(ov, "lower_third")
        name = (edit.speaker_name if edit.speaker_name is not None else (lt or {}).get("title", "")).strip()
        role = (edit.speaker_title if edit.speaker_title is not None else (lt or {}).get("subtitle", "")).strip()
        before = [(o.get("title"), o.get("subtitle")) for o in ov if o["kind"] == "lower_third"]
        if not name:
            ov[:] = [o for o in ov if o["kind"] != "lower_third"]
        elif lt is None:
            ov.append({"kind": "lower_third", "id": "speaker", "t_in": 1.0, "duration": 3.0, "title": name, "subtitle": role})
        else:
            lt["title"], lt["subtitle"] = name, role
        if before != [(o.get("title"), o.get("subtitle")) for o in ov if o["kind"] == "lower_third"]:
            changed.append("lower third")

    if edit.drop_pictures:
        gone = set(edit.drop_pictures)
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", g) for g in gone):
            raise ValueError("Unknown picture.")
        before = len(ov)
        ov[:] = [o for o in ov if not (o["kind"] == "media" and o["id"] in gone)]
        if len(ov) != before:
            changed.append("pictures")

    replan: Optional[str] = None
    layout = d["layout"]
    if edit.framing in ("cover", "fit_blur"):
        if layout["framing"] != edit.framing or layout.get("reframe") or layout.get("split"):
            d["layout"] = {**layout, "framing": edit.framing, "reframe": [], "split": []}
            changed.append("framing")
    elif edit.framing == "auto" or edit.multi is not None:
        replan = edit.multi or "switch"
        changed.append("framing")
    elif trimmed and layout["framing"] in ("track", "split"):
        replan = "switch"
    if replan:
        d["layout"] = {**d["layout"], "framing": "cover", "reframe": [], "split": []}      # apply_framing plans it afresh
    if edit.punch_ins is not None:
        have = bool(d["layout"].get("punch_ins"))
        if edit.punch_ins and not have:
            if d["layout"]["framing"] in ("split", "fit_blur") or edit.framing == "fit_blur":
                raise ValueError("Push-ins do not suit stacked people or a blurred whole-frame fit.")
            spans = sentence_spans(edl.transcript, d["source_in"], d["source_out"])
            plan = plan_punch_ins(spans, d["source_out"] - d["source_in"])
            if not plan:
                raise ValueError("This clip has no sentence start where a push-in would fit.")
            d["layout"]["punch_ins"] = plan
            changed.append("push-ins")
        elif not edit.punch_ins and have:
            d["layout"]["punch_ins"] = []
            changed.append("push-ins")

    if not changed and replan is None:
        return edl, None

    if clip.ai is not None and changed:
        notes = [n for n in d["ai"]["notes"] if not n.startswith("edited by a person")]
        d["ai"]["notes"] = notes + ["edited by a person: " + ", ".join(sorted(set(changed)))]

    try:
        new = Clip.model_validate(d)
        clips = [new if c.id == clip_id else c for c in edl.clips]
        used = {o.asset for c in clips for o in c.overlays if o.kind == "media"}
        # a removed picture must not keep counting: an unused AI image would still ask for an AI disclosure
        assets = [a for a in edl.assets if a.origin not in ("capture", "ai_image") or a.id in used]
        return EDL.model_validate({**edl.model_dump(exclude_none=True), "assets": [a.model_dump(exclude_defaults=True) for a in assets],
                                   "clips": [c.model_dump(exclude_none=True) for c in clips]}), replan
    except ValidationError as e:
        raise ValueError(f"That change is not allowed. {_first_line(e)}") from e


def clip_view(edl: EDL, clip_id: str, *, source_duration: float) -> dict:
    """Everything the editor form shows for one clip, with plain values the UI can put in inputs."""
    clip = next((c for c in edl.clips if c.id == clip_id), None)
    if clip is None:
        raise KeyError(clip_id)
    ov = {o.kind: o for o in clip.overlays}
    cap = clip.captions
    lo, hi = clip.source_in - WORD_CONTEXT, clip.source_out + WORD_CONTEXT
    words = [{"i": i, "w": w.w, "s": round(w.s, 2), "e": round(w.e, 2)}
             for i, w in enumerate(edl.transcript) if hi >= w.s and w.e >= lo]
    return {
        "id": clip.id, "title": clip.title, "source_in": clip.source_in, "source_out": clip.source_out,
        "source_duration": source_duration, "min_seconds": MIN_SECONDS, "max_seconds": MAX_SECONDS,
        "hook": getattr(ov.get("hook"), "text", ""),
        "stat": getattr(ov.get("stat_pill"), "text", ""),
        "speaker_name": getattr(ov.get("lower_third"), "title", ""),
        "speaker_title": getattr(ov.get("lower_third"), "subtitle", ""),
        "captions": {"enabled": cap.enabled, "max_words": cap.max_words, "color": cap.color,
                     "highlight_color": cap.highlight_color, "top_px": cap.top_px, "font_family": cap.font_family},
        "fonts": sorted(fonts.KNOWN),
        "framing": clip.layout.framing, "people": len(clip.layout.split) or None,
        "punch_ins": bool(clip.layout.punch_ins),
        "pictures": [{"id": o.id, "caption": o.caption, "t": o.t_in, "placement": o.placement,
                      "ai": next((a.ai for a in edl.assets if a.id == o.asset), "none"),
                      "source": next((a.source_url for a in edl.assets if a.id == o.asset), "")}
                     for o in clip.overlays if o.kind == "media"],
        "canvas": {"width": edl.render.width, "height": edl.render.height},
        "words": words,
        "edited": any(n.startswith("edited by a person") for n in (clip.ai.notes if clip.ai else [])),
    }
