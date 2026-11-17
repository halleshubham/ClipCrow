# Plan: telling people apart on the GPU worker (not built)

**Status:** planned, not built. **Decision (2026-11-17): not worth building yet.** Measured on a 4-core CPU with a made-up 20-minute 1280x720 video:
one-person check 3 s; seat discovery (600 frames, YuNet) 23 s; watching mouths ~25 s; voice embeddings ~130 s (650 windows at ~0.2 s). **About 3 minutes in all, 75% of it the voice model.**
A GPU worker would save roughly 2 minutes, and only on videos with several people, at the price of about a day of work, a new place where a worker's output is trusted, and a server fallback
that must stay anyway (the worker is a PC that is sometimes off). Stage times are now recorded per job (`Stage.seconds`, shown on the job page): build this if real jobs show the **frame stage over ~4 minutes**
or the server is seen to stall other jobs while it runs. A cheaper step first: embed fewer windows (a stride) if the voice model is the whole cost.

## Why it might be worth it
`seats.build_speaker_map` runs on the server CPU. Its cost for a 20-minute video, rough: face finding (decode at <= 600 frames + YuNet), voice embeddings
(~0.2 s per 1.5 s window on CPU: a few hundred windows), watching mouths (a few hundred frames). On a GPU the voice model is roughly 10x faster, and the server
(shared with other people's jobs, Chrome renders and the recogniser fallback) is left alone.

Move it only if the measured stage takes **more than about 4 minutes**, or the server is seen to stall other jobs while it runs. A one-person video already skips
almost all of it (the quick check looks at 16 frames).

## What the worker already has
The `:latest` image (torch, transformers, OpenCV with YuNet, FFmpeg) and a per-job cache of the source video: the first render of a job fetches it, the other clips reuse it.
So the video does not cross the network twice if the speakers task is taken by the worker that then renders.

## Design
1. **A new task kind `speakers`** next to `transcribe` and `render` (`WorkerHub.submit_speakers(job_id, files={"source.mp4": path}, meta={"duration": ...})`, same lease, heartbeat and `max_seconds`).
   The manifest names only the working video (the cleaned `clean.mp4` when there is one, else the upload); a worker fetches it by name, as for a render.
2. **The worker runs `seats.build_speaker_map`** on the cached video with the GPU embedder (`WavLMEmbedder` on CUDA: add `device` to it) and a temp output directory.
   It posts only JSON: seats, turns, the voice-to-seat assignment, lifts. **No picture and no file.**
3. **The server treats the result as untrusted** (see CLAUDE.md): `SpeakerMap.from_json` plus bounds (at most 12 seats and 20 voices, every fraction in 0..1, turns sorted, inside the duration, at most 4000 turns,
   JSON at most 500 KB), then draws `speakers.jpg` itself from one frame it reads from its own copy. The signature of the video is the server's, never the worker's.
4. **Fallbacks, in order:** no worker online -> the server does it (today's code); the worker does not deliver (lease lost, error, timeout) -> the server does it; a worker that is too old to know the kind never
   receives the task (a `kinds` list in its poll, like the render pin).
5. **Order in a job:** unchanged. `make()` calls `speakers_fn`; `RealPipeline` gives it a `RemoteSpeakers(hub)` that tries a worker first (as `RemoteTranscriber` does for speech).
   The "Find who is speaking" button uses the same function.
6. **Protocol:** a `kinds` field in `/api/worker/poll` (server first, then workers: an older server rejects new poll fields), a result model `SpeakersResultIn`, and the docs in `docs/deploy.md`.

## Tests
- Hub: the task is offered only to a worker that lists the kind; lease/heartbeat/expiry behave as for a render; a stale or foreign worker's result is refused.
- Validation: each bound above has a test with a hostile result (negative fractions, a million turns, a voice id of 10^9).
- `RemoteSpeakers`: no worker -> local call; worker error -> local call; a good result -> the map is saved and `speakers.jpg` is drawn by the server.
- One end-to-end test with a fake worker that returns a canned map.

## Effort and risk
About a day. Main risks: another place a worker's output is trusted (hence the strict validation and the server-drawn picture) and a job that waits for a worker that is asleep
(hence the pickup timeout and the fallback). Do not do it before the measurement says it pays.
