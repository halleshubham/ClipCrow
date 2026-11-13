#!/usr/bin/env python3
"""Break a video down for frame-by-frame study.

Writes paired frames (two frames a moment apart, so motion between them is visible)
on labelled contact sheets, a shot list from cut detection, audio event markers on
time-aligned spectrogram strips, loudness and a word-level transcript.
This script only measures; Claude does the watching.

Run with:  uv run --python 3.12 --with numpy --with pillow --with faster-whisper --with "av<19" analyze.py VIDEO OUT_DIR
"""
import argparse
import json
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

SR = 22050
N_FFT = 1024
HOP = 256
FRAME_RATE_A = SR / HOP  # audio analysis frames per second
AUDIO_CHUNK = 15  # seconds per audio strip image
PX_PER_SEC = 80

ASPECTS = {"9:16": 9 / 16, "16:9": 16 / 9, "1:1": 1.0, "4:5": 4 / 5, "4:3": 4 / 3, "3:4": 3 / 4}


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def find_tool(name):
    path = shutil.which(name) or str(Path.home() / ".local/bin" / name)
    if not Path(path).exists():
        raise SystemExit(f"{name} not found (expected on PATH or in ~/.local/bin)")
    return path


FFMPEG = find_tool("ffmpeg")
FFPROBE = find_tool("ffprobe")


def font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


# ---------------------------------------------------------------- video

def probe(video):
    d = json.loads(run([FFPROBE, "-v", "error", "-print_format", "json",
                        "-show_format", "-show_streams", str(video)]).stdout)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    num, den = (int(x) for x in v["avg_frame_rate"].split("/"))
    w, h = int(v["width"]), int(v["height"])
    rotation = next((int(sd["rotation"]) for sd in v.get("side_data_list", []) if "rotation" in sd), 0)
    if abs(rotation) in (90, 270):
        w, h = h, w
    aspect = min(ASPECTS, key=lambda k: abs(ASPECTS[k] - w / h))
    return {
        "duration": round(float(d["format"]["duration"]), 3),
        "width": w,
        "height": h,
        "aspect": aspect,
        "fps": round(num / den, 3) if den else None,
        "video_codec": v["codec_name"],
        "audio_codec": a["codec_name"] if a else None,
    }


def detect_cuts(video, threshold):
    r = run([FFMPEG, "-hide_banner", "-nostats", "-i", str(video), "-an",
             "-vf", f"scale=320:-2,select='gt(scene,{threshold})',metadata=print",
             "-f", "null", "-"])
    cuts, t = [], None
    for line in r.stderr.splitlines():
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            t = float(m.group(1))
        m = re.search(r"lavfi\.scene_score=([\d.]+)", line)
        if m and t is not None:
            cuts.append({"time": round(t, 3), "score": round(float(m.group(1)), 3)})
            t = None
    return cuts


def shots_from_cuts(cuts, duration):
    bounds = [0.0] + [c["time"] for c in cuts] + [duration]
    shots = []
    for a, b in zip(bounds, bounds[1:]):
        if b - a > 0.01:
            shots.append({"index": len(shots) + 1, "start": round(a, 3), "end": round(b, 3),
                          "duration": round(b - a, 3)})
    return shots


def plan_moments(start, end, step, gap, cuts):
    """Each moment is a pair of frames: t and t + gap. Cuts always get a pair on each side."""
    last = end - gap
    special = []
    for c in cuts:
        for t, note in ((c["time"] - 0.2, "before cut"), (c["time"] + 0.04, "after cut")):
            if start <= t <= last:
                special.append((round(t, 3), note))
    regular = []
    clearance = min(0.25, step / 2)
    t = start
    while t <= last + 1e-6:
        if all(abs(t - s) >= clearance for s, _ in special):
            regular.append((round(t, 3), ""))
        t += step
    return [{"index": i + 1, "t": t, "t2": round(t + gap, 3), "note": note}
            for i, (t, note) in enumerate(sorted(special + regular))]


def frame_path(frames_dir, t):
    return frames_dir / f"{int(round(t * 1000)):08d}.jpg"


def extract_frames(video, times, frames_dir, width):
    frames_dir.mkdir(parents=True, exist_ok=True)

    def one(t):
        path = frame_path(frames_dir, t)
        if not path.exists():
            run([FFMPEG, "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(video),
                 "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "3", "-y", str(path)])

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(one, sorted(set(times))))


def load_frame(frames_dir, t, size):
    path = frame_path(frames_dir, t)
    if path.exists():
        return Image.open(path).convert("RGB")
    return Image.new("RGB", size, (40, 0, 0))


def make_pair_sheets(moments, frames_dir, out_dir, vertical, width, grid=None):
    out_dir.mkdir(parents=True, exist_ok=True)
    pairs_per_row, rows = grid or ((2, 2) if vertical else (1, 3))
    per_sheet = pairs_per_row * rows
    sample = next((frame_path(frames_dir, m["t"]) for m in moments if frame_path(frames_dir, m["t"]).exists()), None)
    fw, fh = Image.open(sample).size if sample else (width, int(width * (16 / 9 if vertical else 9 / 16)))
    label_h, pad, gap = 34, 10, 4
    cell_w, cell_h = 2 * fw + gap, label_h + fh
    f_label = font(20)
    sheets = []
    for n, s in enumerate(range(0, len(moments), per_sheet), start=1):
        chunk = moments[s:s + per_sheet]
        used_rows = -(-len(chunk) // pairs_per_row)
        sheet = Image.new("RGB", (pairs_per_row * cell_w + (pairs_per_row + 1) * pad,
                                  used_rows * cell_h + (used_rows + 1) * pad), (24, 24, 24))
        draw = ImageDraw.Draw(sheet)
        for i, m in enumerate(chunk):
            x = pad + (i % pairs_per_row) * (cell_w + pad)
            y = pad + (i // pairs_per_row) * (cell_h + pad)
            draw.text((x, y + 6), f"M{m['index']:03d}  A {m['t']:.2f}s  ->  B {m['t2']:.2f}s",
                      fill=(255, 255, 255), font=f_label)
            if m["note"]:
                draw.text((x + cell_w - 130, y + 6), f"[{m['note']}]", fill=(255, 210, 0), font=f_label)
            sheet.paste(load_frame(frames_dir, m["t"], (fw, fh)).resize((fw, fh)), (x, y + label_h))
            sheet.paste(load_frame(frames_dir, m["t2"], (fw, fh)).resize((fw, fh)), (x + fw + gap, y + label_h))
        name = f"sheet_{n:03d}.jpg"
        sheet.save(out_dir / name, quality=85)
        sheets.append({"file": name, "from": chunk[0]["t"], "to": chunk[-1]["t2"],
                       "moments": [m["index"] for m in chunk]})
    return sheets


def make_shot_sheets(video, shots, frames_dir, out_dir, vertical):
    thumb_w = 220 if vertical else 320
    cols = 6 if vertical else 4
    per_sheet = cols * 4
    times = [round((s["start"] + s["end"]) / 2, 3) for s in shots]
    extract_frames(video, times, frames_dir / "shots", thumb_w)
    f_label = font(16)
    label_h, pad = 26, 8
    files = []
    for n, s in enumerate(range(0, len(shots), per_sheet), start=1):
        chunk = list(zip(shots[s:s + per_sheet], times[s:s + per_sheet]))
        thumbs = [load_frame(frames_dir / "shots", t, (thumb_w, thumb_w)) for _, t in chunk]
        tw, th = thumbs[0].size
        used_rows = -(-len(chunk) // cols)
        sheet = Image.new("RGB", (cols * (tw + pad) + pad, used_rows * (th + label_h + pad) + pad), (24, 24, 24))
        draw = ImageDraw.Draw(sheet)
        for i, ((shot, _), img) in enumerate(zip(chunk, thumbs)):
            x = pad + (i % cols) * (tw + pad)
            y = pad + (i // cols) * (th + label_h + pad)
            draw.text((x, y + 4), f"S{shot['index']:02d} {shot['start']:.1f}-{shot['end']:.1f}s ({shot['duration']:.1f}s)",
                      fill=(255, 255, 255), font=f_label)
            sheet.paste(img.resize((tw, th)), (x, y + label_h))
        name = f"shots_{n:02d}.jpg"
        sheet.save(out_dir / name, quality=85)
        files.append(name)
    return files


# ---------------------------------------------------------------- audio

def load_audio(video):
    r = subprocess.run([FFMPEG, "-v", "error", "-i", str(video), "-ac", "1", "-ar", str(SR),
                        "-f", "s16le", "-"], capture_output=True)
    return np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768


def spectral_frames(y):
    """Magnitude spectrogram (frames x bins) and RMS per frame, computed in blocks."""
    if len(y) < N_FFT:
        y = np.pad(y, (0, N_FFT - len(y)))
    n = 1 + (len(y) - N_FFT) // HOP
    window = np.hanning(N_FFT).astype(np.float32)
    mags, rms = [], []
    for b in range(0, n, 4096):
        idx = np.arange(N_FFT)[None, :] + HOP * np.arange(b, min(n, b + 4096))[:, None]
        frames = y[idx]
        rms.append(np.sqrt(np.mean(frames ** 2, axis=1)))
        mags.append(np.abs(np.fft.rfft(frames * window, axis=1)).astype(np.float32))
    return np.concatenate(mags), np.concatenate(rms)


def detect_onsets(mag, rms, words):
    logm = np.log1p(10 * mag)
    flux = np.concatenate([[0], np.maximum(0, np.diff(logm, axis=0)).sum(axis=1)])
    win = int(FRAME_RATE_A * 0.5)
    padded = np.pad(flux, win, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, 2 * win + 1)
    med = np.median(windows, axis=1)
    mad = np.median(np.abs(windows - med[:, None]), axis=1)
    thresh = med + 3 * mad + 1e-6
    ref = np.percentile(flux, 95) + 1e-6
    freqs = np.fft.rfftfreq(N_FFT, 1 / SR)
    near = int(FRAME_RATE_A * 0.08)
    onsets = []
    for i in range(1, len(flux) - 1):
        if flux[i] <= thresh[i] or flux[i] < flux[max(0, i - near):i + near + 1].max():
            continue
        strength = flux[i] / ref
        if strength < 0.6:
            continue
        t = i / FRAME_RATE_A
        spec = mag[i]
        centroid = float((freqs * spec).sum() / (spec.sum() + 1e-9))
        in_speech = any(w["start"] - 0.05 <= t <= w["end"] + 0.05 for w in words)
        onsets.append({
            "time": round(t, 3),
            "strength": round(float(strength), 2),
            "centroid_hz": int(centroid),
            "level_db": round(float(20 * np.log10(rms[i] + 1e-9)), 1),
            "character": "bright (click/pop/whoosh/snap)" if centroid > 3500
            else "low (boom/hit/bass drop)" if centroid < 400 else "mid",
            "during_speech": in_speech,
        })
    onsets.sort(key=lambda o: -o["strength"])
    return sorted(onsets[:400], key=lambda o: o["time"])


INFERNO = np.array([[0, 0, 4], [40, 11, 84], [101, 21, 110], [159, 42, 99],
                    [212, 72, 66], [245, 125, 21], [250, 193, 39], [252, 255, 164]], dtype=np.float32)


def colormap(v):
    v = np.clip(v, 0, 1) * (len(INFERNO) - 1)
    lo = np.floor(v).astype(int)
    hi = np.minimum(lo + 1, len(INFERNO) - 1)
    frac = (v - lo)[..., None]
    return (INFERNO[lo] * (1 - frac) + INFERNO[hi] * frac).astype(np.uint8)


def make_audio_strips(mag, rms, onsets, cuts, words, duration, out_dir):
    """Spectrogram + loudness + markers + words, one image per AUDIO_CHUNK seconds, absolute time axis."""
    out_dir.mkdir(parents=True, exist_ok=True)
    freqs = np.fft.rfftfreq(N_FFT, 1 / SR)
    edges = np.geomspace(50, SR / 2, 121)
    # Low bands are narrower than one FFT bin; those borrow the nearest bin instead of going blank.
    bands = np.stack([mag[:, (freqs >= lo) & (freqs < hi)].mean(axis=1) if ((freqs >= lo) & (freqs < hi)).any()
                      else mag[:, np.argmin(np.abs(freqs - np.sqrt(lo * hi)))]
                      for lo, hi in zip(edges, edges[1:])], axis=1)
    db = 20 * np.log10(bands + 1e-6)
    vmax = np.percentile(db, 99.5)
    norm = (db - (vmax - 70)) / 70
    left, spec_h, rms_h, word_h, tick_h = 56, 240, 70, 64, 22
    f_small, f_word = font(14), font(15)
    files = []
    for c0 in np.arange(0, duration, AUDIO_CHUNK):
        c1 = min(duration, c0 + AUDIO_CHUNK)
        w = int((c1 - c0) * PX_PER_SEC)
        if w < 10:
            continue
        H = tick_h + spec_h + rms_h + word_h
        img = Image.new("RGB", (left + w, H), (18, 18, 18))
        draw = ImageDraw.Draw(img)
        cols = np.clip(((c0 + np.arange(w) / PX_PER_SEC) * FRAME_RATE_A).astype(int), 0, len(norm) - 1)
        spec = colormap(norm[cols].T[::-1])
        img.paste(Image.fromarray(spec).resize((w, spec_h)), (left, tick_h))
        for hz in (100, 1000, 10000):
            y = tick_h + spec_h - int(np.log(hz / 50) / np.log(edges[-1] / 50) * spec_h)
            draw.text((4, y - 8), f"{hz // 1000}k" if hz >= 1000 else str(hz), fill=(200, 200, 200), font=f_small)
        # loudness line
        level = 20 * np.log10(rms[cols] + 1e-9)
        ly = tick_h + spec_h + rms_h - 4 - np.clip((level + 60) / 60, 0, 1) * (rms_h - 8)
        draw.line(list(zip(left + np.arange(w), ly)), fill=(120, 220, 120), width=1)
        draw.text((4, tick_h + spec_h + 4), "level", fill=(120, 220, 120), font=f_small)

        def x_of(t):
            return left + int((t - c0) * PX_PER_SEC)

        # Events inside speech are mostly syllables: short and dim. Events outside speech are likely SFX: full and bright.
        for o in onsets:
            if c0 <= o["time"] < c1:
                x = x_of(o["time"])
                if o["during_speech"]:
                    draw.line([(x, tick_h + spec_h), (x, tick_h + spec_h + 14)], fill=(150, 60, 60), width=1)
                else:
                    shade = int(150 + 105 * min(1, o["strength"] / 2))
                    draw.line([(x, tick_h), (x, tick_h + spec_h + rms_h)], fill=(shade, 30, 30), width=3)
        for c in cuts:
            if c0 <= c["time"] < c1:
                draw.line([(x_of(c["time"]), 0), (x_of(c["time"]), H)], fill=(0, 220, 255), width=2)
        for s in np.arange(np.ceil(c0), c1):
            draw.line([(x_of(s), tick_h - 6), (x_of(s), tick_h)], fill=(220, 220, 220))
            draw.text((x_of(s) + 2, 2), f"{s:.0f}s", fill=(220, 220, 220), font=f_small)
        row_y = tick_h + spec_h + rms_h + 4
        last_end = [-1e9, -1e9]
        for wd in words:
            if not c0 <= wd["start"] < c1:
                continue
            x = x_of(wd["start"])
            row = next((r for r in (0, 1) if x > last_end[r]), int(np.argmin(last_end)))
            x = max(x, int(last_end[row]))
            text = wd["word"].strip()
            draw.text((x, row_y + row * 28), text, fill=(255, 255, 255), font=f_word)
            last_end[row] = x + draw.textlength(text, font=f_word) + 4
        name = f"audio_{int(c0):04d}-{int(c1):04d}s.png"
        img.save(out_dir / name)
        files.append(name)
    return files


def loudness(video):
    r = run([FFMPEG, "-hide_banner", "-nostats", "-i", str(video), "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"])
    i = re.findall(r"I:\s+(-?[\d.]+) LUFS", r.stderr)
    lra = re.findall(r"LRA:\s+([\d.]+) LU", r.stderr)
    peak = re.findall(r"Peak:\s+(-?[\d.]+) dBFS", r.stderr)
    return {"integrated_lufs": float(i[-1]) if i else None, "lra_lu": float(lra[-1]) if lra else None,
            "true_peak_dbfs": float(peak[-1]) if peak else None}


def silences(video):
    r = run([FFMPEG, "-hide_banner", "-nostats", "-i", str(video), "-vn",
             "-af", "silencedetect=n=-35dB:d=0.25", "-f", "null", "-"])
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    return [{"start": round(max(0, s), 3), "end": round(e, 3)} for s, e in zip(starts, ends)]


def _srt_time(t):
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def transcribe(video, out_dir, model):
    """Word-timed transcript with faster-whisper (the engine ClipCrow itself uses); writes transcript.json/.srt/.txt."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise SystemExit("faster-whisper is missing: run with  uv run --python 3.12 --with numpy --with pillow "
                         "--with faster-whisper --with 'av<19' ...  (or pass --no-transcript)")
    wav = out_dir / "transcript.wav"
    run([FFMPEG, "-v", "error", "-y", "-i", str(video), "-ac", "1", "-ar", "16000", str(wav)])
    segments, info = WhisperModel(model, device="auto", compute_type="auto").transcribe(
        str(wav), word_timestamps=True, vad_filter=True)
    segs, words, srt, txt = [], [], [], []
    for i, seg in enumerate(segments, 1):
        ws = [{"word": w.word, "start": round(w.start, 3), "end": round(w.end, 3)} for w in (seg.words or [])]
        segs.append({"start": round(seg.start, 3), "end": round(seg.end, 3), "text": seg.text, "words": ws})
        words += ws
        srt.append(f"{i}\n{_srt_time(seg.start)} --> {_srt_time(seg.end)}\n{seg.text.strip()}\n")
        txt.append(seg.text.strip())
    wav.unlink(missing_ok=True)
    (out_dir / "transcript.json").write_text(json.dumps({"language": info.language, "segments": segs}, ensure_ascii=False))
    (out_dir / "transcript.srt").write_text("\n".join(srt))
    (out_dir / "transcript.txt").write_text("\n".join(txt))
    return info.language, words


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("video", type=Path)
    p.add_argument("out", type=Path)
    p.add_argument("--step", type=float, help="seconds between moments (default: 0.5 up to 90s, 1 up to 10min, else 2)")
    p.add_argument("--gap", type=float, default=0.2, help="seconds between the two frames of a pair")
    p.add_argument("--threshold", type=float, default=0.3, help="scene-change threshold for cut detection")
    p.add_argument("--width", type=int, help="frame width on sheets")
    p.add_argument("--grid", help="pairs per sheet as COLSxROWS, e.g. 3x3 (default 2x2 vertical, 1x3 horizontal)")
    p.add_argument("--model", default="small", help="faster-whisper model (tiny, base, small, medium, large-v3)")
    p.add_argument("--no-transcript", action="store_true")
    p.add_argument("--start", type=float, help="only study from here (dense mode)")
    p.add_argument("--end", type=float, help="only study up to here (dense mode)")
    p.add_argument("--tag", help="name for a dense-mode sheet folder, e.g. dense_12-15")
    p.add_argument("--frames-only", action="store_true", help="skip audio and transcript (dense mode)")
    a = p.parse_args()

    out = a.out
    out.mkdir(parents=True, exist_ok=True)
    meta = probe(a.video)
    vertical = meta["height"] > meta["width"]
    width = a.width or (400 if vertical else 640)
    duration = meta["duration"]
    step = a.step or (0.5 if duration <= 90 else 1.0 if duration <= 600 else 2.0)

    data_path = out / "data.json"
    data = json.loads(data_path.read_text()) if data_path.exists() else {}
    cuts = data.get("cuts") or detect_cuts(a.video, a.threshold)
    shots = shots_from_cuts(cuts, duration)

    start, end = a.start or 0.0, min(a.end or duration, duration - 0.05)
    moments = plan_moments(start, end, step, a.gap, cuts)
    frames_dir = out / "frames"
    print(f"{len(moments)} frame pairs, {len(cuts)} cuts, {len(shots)} shots")
    extract_frames(a.video, [m["t"] for m in moments] + [m["t2"] for m in moments], frames_dir, width)
    sheet_dir = out / ("sheets" if not a.tag else f"sheets_{a.tag}")
    grid = tuple(int(x) for x in a.grid.lower().split("x")) if a.grid else None
    sheets = make_pair_sheets(moments, frames_dir, sheet_dir, vertical, width, grid)

    if a.frames_only:
        print(f"Wrote {len(sheets)} sheets to {sheet_dir}")
        return

    shot_sheets = make_shot_sheets(a.video, shots, frames_dir, out, vertical)
    language, words = (None, []) if a.no_transcript else transcribe(a.video, out, a.model)
    y = load_audio(a.video)
    audio_files, onsets = [], []
    if len(y):
        mag, rms = spectral_frames(y)
        onsets = detect_onsets(mag, rms, words)
        audio_files = make_audio_strips(mag, rms, onsets, cuts, words, duration, out / "audio")
    quiet = silences(a.video) if len(y) else []
    loud = loudness(a.video) if len(y) else {}

    durs = [s["duration"] for s in shots]
    speech_secs = sum(w["end"] - w["start"] for w in words)
    stats = {
        "shots": len(shots),
        "avg_shot_s": round(float(np.mean(durs)), 2),
        "median_shot_s": round(float(np.median(durs)), 2),
        "shortest_shot_s": round(min(durs), 2),
        "longest_shot_s": round(max(durs), 2),
        "cuts_per_min": round(len(cuts) / duration * 60, 1),
        "cuts_in_first_3s": sum(1 for c in cuts if c["time"] <= 3),
        "words": len(words),
        "words_per_min": round(len(words) / duration * 60, 1) if words else 0,
        "speech_coverage_pct": round(speech_secs / duration * 100, 1) if words else 0,
        "silence_total_s": round(sum(s["end"] - s["start"] for s in quiet), 2),
        "audio_events": len(onsets),
        "audio_events_outside_speech": sum(1 for o in onsets if not o["during_speech"]),
    }
    data = {"meta": meta, "settings": {"step": step, "gap": a.gap, "threshold": a.threshold},
            "stats": stats, "loudness": loud, "language": language, "cuts": cuts, "shots": shots,
            "moments": moments, "sheets": sheets, "shot_sheets": shot_sheets, "audio_images": audio_files,
            "silences": quiet, "audio_events": onsets}
    data_path.write_text(json.dumps(data, indent=1))

    lines = [f"# Watch data: {a.video.name}", "",
             f"{meta['width']}x{meta['height']} ({meta['aspect']}), {meta['fps']} fps, {duration:.1f}s. "
             f"Frame pairs every {step}s, frames {a.gap}s apart.", "", "## Stats", ""]
    lines += [f"- {k}: {v}" for k, v in {**stats, **loud}.items()]
    lines += ["", "## Reading order", "",
              "1. Shot overview: " + ", ".join(shot_sheets),
              f"2. Every pair sheet, in order ({len(sheets)}):"]
    lines += [f"   - sheets/{s['file']}: {s['from']:.2f}s to {s['to']:.2f}s" for s in sheets]
    lines += [f"3. Audio strips (cyan = cut, red = audio event, green = level, words at bottom): "
              + ", ".join(f"audio/{f}" for f in audio_files),
              "4. data.json for exact numbers: cuts, shots, audio_events, silences",
              "5. transcript.json / transcript.srt for words" if words else "5. No transcript"]
    (out / "index.md").write_text("\n".join(lines) + "\n")
    print(f"Done: {out / 'index.md'}")


if __name__ == "__main__":
    main()
