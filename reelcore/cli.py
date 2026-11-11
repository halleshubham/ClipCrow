from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .edl import EDLError, json_schema, load_edl


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="reelcore")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("schema", help="print the EDL JSON schema")
    v = sub.add_parser("validate", help="validate an EDL file")
    v.add_argument("edl")
    c = sub.add_parser("compile", help="write the HyperFrames project for a clip (no render)")
    c.add_argument("edl"); c.add_argument("--clip", required=True); c.add_argument("--out", required=True)
    c.add_argument("--media-root", default="."); c.add_argument("--gsap")
    c.add_argument("--no-media", action="store_true", help="HTML only: skip ffmpeg and the matte")
    r = sub.add_parser("render", help="compile, check and render a clip to MP4")
    r.add_argument("edl"); r.add_argument("--clip", required=True); r.add_argument("--out", required=True)
    r.add_argument("--media-root", default="."); r.add_argument("--workdir", default="build")
    r.add_argument("--gsap"); r.add_argument("--workers", type=int)
    r.add_argument("--matte-device", default="auto"); r.add_argument("--no-check", action="store_true")
    a = ap.parse_args(argv)

    try:
        if a.cmd == "schema":
            print(json.dumps(json_schema(), indent=2)); return 0
        edl = load_edl(a.edl)
        if a.cmd == "validate":
            print(f"ok: {len(edl.clips)} clip(s), {len(edl.transcript)} words"); return 0
        if a.cmd == "compile":
            from .compile import compile_clip
            clip = next((x for x in edl.clips if x.id == a.clip), None)
            if clip is None:
                raise EDLError(f"no clip {a.clip!r}")
            res = compile_clip(edl, clip, a.out, media_root=a.media_root, gsap_js=a.gsap,
                               prepare=not a.no_media)
            print(f"wrote {res.project_dir} ({res.duration:.2f}s, scenes: {', '.join(res.scenes) or 'none'})")
            return 0
        if a.cmd == "render":
            from .pipeline import render_clip
            out, _ = render_clip(edl, a.clip, a.out, workdir=a.workdir, media_root=a.media_root,
                                 gsap_js=a.gsap, workers=a.workers, matte_device=a.matte_device,
                                 run_check=not a.no_check)
            print(f"rendered {out}"); return 0
    except (EDLError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr); return 2
    except Exception as e:  # RenderError and friends
        print(f"error: {e}", file=sys.stderr); return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
