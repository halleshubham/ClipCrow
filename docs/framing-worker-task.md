# Scope: a "framing" task for the GPU worker

Status: scoping only, nothing built (step 2 of "Do this first", the parallel framing on the server, was built 2026-11-19). Written 2026-11-19 after job 8b13efc5b2c1 (76-minute video, 10 clips) spent a long time in the frame stage with no
GPU activity.

## What the frame stage does today
`make.py` stage "frame": (1) if the people option is not "one person", a who-is-who map (`seats.build_speaker_map`, already a worker task, `speakers`);
(2) `framing.apply_framing`, which calls `reframe.plan_framing` once per clip, one after the other, **on the server**. `plan_framing` samples the clip at
5 frames a second (`sample_faces`: `cv2.VideoCapture`, a seek for every sample, OpenCV's Haar face detector, a small thumbnail per sample), then picks the
subject, finds camera cuts, smooths the path. The output per clip is a small `Framing` (framing kind, keyframes `(t, cx)`, split points, fit spans,
face zone). The numbers go into the EDL; the render task later receives them.

So: no GPU is involved (Haar is CPU); the cost is video decoding and seeking on the server's CPU. A worker would help by having **more or faster CPU
cores**, and by not competing with the server's web requests and other jobs, not by having a GPU.

## Do this first (no protocol change)
1. **Time the stage.** Per-stage timings are an open item in the dev log. Log `frame: c3 took 41 s` per clip, so there is a number to beat.
2. **Run the clips in parallel on the server** (`apply_framing` with a thread pool; `cv2` releases the GIL while decoding and detecting). A few lines, no new
   attack surface. Bound it to the server's core count.
3. **Cheaper sampling:** one `cap.set(POS_MSEC)` seek per sample is slow on long-GOP files. Decode the clip once and keep every Nth frame (or ask ffmpeg
   for `fps=5` over the range) instead of seeking.
Only if the stage is still slow for long videos, build the task below.

## The task (if still needed)
**Kind `framing`**, same shape as `speakers`:
- *Server → worker:* manifest = the working video as `media/<name>` (the same file and name a render of the job uses, so the worker caches it for the
  renders and nothing is sent twice); `meta` = `{job_id, multi, graphics, clips: [{id, start, end}]}` (at most 40 clips; numbers only).
- *Worker → server:* `framing: [{id, framing, keyframes:[[t,cx]...], coverage, people, split:[[cx,cy]...], fit_spans:[[t0,t1]...], face_zone}]`.
- *Only when there is no voice map* (`multi == "off"`, or no speakers): with a map `plan_voices` is cheap and stays on the server, which is exactly the
  slow case of job 8b13efc5b2c1.

### Server (`service/workers.py`, `make.py`, `framing.py`)
- `Task.kind = "framing"`; `WorkerHub.submit_framing`; `PollIn.can` gains `"framing"`; `HubView.submit_framing`; `unserved`/pool rules unchanged.
- `FramingIn`/`ResultIn.framing`: closed models (`extra="forbid"`), every number range-checked (cx and cy in 0..1, `t` inside the clip, at most
  `ceil(duration * KEY_RATE) + 2` keyframes, at most 4 split points, at most 100 fit spans, framing in a fixed set), the clip ids must be exactly the ones asked.
  The answer is untrusted like every worker answer.
- `RemoteFraming` (drop-in for `plan_framing`, like `RemoteSpeakers`): worker if one with `framing` is online, else local; a worker that does not deliver,
  or an invalid answer, falls back to the server and says so in the job log.
- `apply_framing(..., framing_fn=)` so the server decides who plans; "frame" stage log lines say "asking the GPU worker to frame the clips".
- Policy: a framing task must **not wake a sleeping pod** (it is CPU work; paying ₹45 to ₹91 an hour to wake a GPU for it is waste): the pod controller's
  `can` list excludes `framing`, so it goes to an already-online worker or stays on the server.
- Cost table: `framing` gets its own label in `gpu_cost._KIND`; time on a priced machine is costed like any task.

### Worker (`worker.py`)
- `handle_framing` (copy of `handle_speakers`: lease, cached download, heartbeat, `plan_framing` per clip with the worker's own detector, JSON back);
  `framing_ready()` (needs `cv2` and the Haar cascade, no torch); `can` gains `"framing"`; clips planned in a thread pool, bounded by the CPU count.
- No new dependency (OpenCV is already in the image).

### Compatibility and order of work
Server first, then the worker image (the answer models forbid unknown fields: the lesson of the `live` field). A worker older than the server simply
does not offer `framing`. The same `reframe.py` on both sides gives the same numbers; a version mismatch still yields valid (slightly different) framing.

### Tests
Fake-transport tests like `tests/test_worker.py`: a good answer is applied; a malformed or out-of-range answer, a wrong clip id set, a dead worker and a
cancelled job all fall back to the server; the pod controller does not wake for `framing`; `apply_framing` gives the same EDL with `framing_fn` as without.
One real check on the home PC with a long video.

## Size and risk
About 1 to 1.5 days: models, hub, `RemoteFraming`, worker handler, docs and tests. Risks: shipping a long video to a remote worker costs minutes of
upload, so it only pays off when the file is already cached there for the renders or the worker is on the local network; a worker with fewer cores than
the server would be slower, not faster. Decide after step 1's numbers.
