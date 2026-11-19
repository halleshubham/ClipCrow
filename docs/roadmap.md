# ClipCrow roadmap (a planning pass, 2026-11-19)

Made by a read-only planning agent from the code and the development log; the product owner decides the order. Bulk scheduling was in flight and is not listed.
Sizes: S under 3 days, M 1-2 weeks, L over 2 weeks. Every item must keep the rules in CLAUDE.md (untrusted input, no network in the render browser, secrets never logged,
Devanagari copy in tests).

## Recommended order of the first ten
1. **A1 Real-footage test set (M).** 6-8 short real clips (street interview, podcast, two people, a statue or caption graphic, Marathi vs Hindi) with hand-labelled "who should be shown";
   a script scores the framing. Every threshold so far (cut 25 / 4x, still face 1.5, plausible 0.8) was checked on made-up videos only. Keep the clips out of git.
2. **B1 Timings and cost on the job page (S).** Stage times exist; add GPU/CPU seconds and a cost line, to decide B2-B4 with data.
3. **A8 Audio and the outro seam (M).** Verify `loudness.py` on a real render (the log still shows -22 vs -14.6 LUFS), true peak, the 2.8 s black seam before the ending video,
   crop-fill instead of black bars for a landscape ending. Optional DeepFilterNet denoise on the worker for noisy street audio (check the licence).
4. **A7 Caption legibility (S-M).** Measure contrast of the caption band against the frame; add a stroke or scrim in code; put captions off `Layout.face_zone`.
5. **A3 Voice separation (M).** The who-is-who pass merged a whole street interview into 2 voices. Tighter threshold, cut at pauses, optional silero-vad (MIT), adaptive voice count.
6. **A2 Shot-aware speaker mode (L).** Each camera shot is its own scene; match faces across shots by a small face-embedding model (not by position); match the voice of the stretch to a face
   in that shot. Needs 1 and 5. Add new fields to `speakers.json` as optional and deploy the server before the worker.
7. **B3 Plan first, analyse later (M).** Run seats, voices and faces only over the stretches the planner picked. The biggest cost lever (a 53-minute video took about 30 minutes).
8. **D1 Approval sheet before the long render (M).** One page, a frame at every cut with the crop drawn, the caption text and the cut list; approve or fix, then render. Optional per brand.
9. **C1 + C2 A curated HyperFrames block pack and lower thirds (M).** About 10 approved registry blocks (3 lower thirds, 3 CSS transitions, 2 overlays, 2 caption templates), vendored and
   pinned, chosen by id in the EDL, checked with `hyperframes check` in CI; a lower third from the who-is-who names. Every block gets a Devanagari render test. No network, no remote fonts.
10. **E1 + E5 Per-platform titles, descriptions, hashtags; translated subtitles (S-M / M).** Closed per-platform output in the clip's language; an SRT/VTT download and a translated caption track.

Next: D2 post-render QA report, A6 cleanup suggestions (hidden retakes, audio repeat detector, re-transcribe the cut as an audit), F1 public API and webhooks, F3 billing hardening.

## Other items, by group
**A. Cut and picture.** A4 bound and speed up who-is-who (time budget, worker default, banner when it times out); A5 reporter-vs-interviewee heuristic (opt-in per job type);
A6 cleanup suggestions as above; A9 default "one person" for talks the planner classifies as single-speaker.
**B. Speed and cost.** B2 render cache keyed by clip EDL hash plus brand assets plus HyperFrames version; B4 worker queue (priorities, heartbeat in the UI, resumable uploads); B5 LLM token budget per job and a planner cache.
**C. Creative.** C3 one "look pack" per brand (caption style, transition, lower third, ending, music, banner); C4 listen/look validation of sound cues and transitions before they become defaults; C5 caption templates (word highlight, box, karaoke) with Devanagari shaping tests.
**D. Workflow.** D2 post-render QA badge per clip (loudness, black frames, contrast, face/logo overlap); D3 a corrections file (treated as untrusted data, capped); D4 bulk operations on clips; D5 completion email (Resend); D6 job templates ("street interview", "podcast", "news explainer").
**E. Distribution.** E2 thumbnail candidates; E3 A/B hooks (only useful with E4); E4 analytics from SocioBird (check its API first); dubbing is a later bet.
**F. Platform.** F1 API keys and signed webhooks; F2 teams and roles (the biggest authorisation change, not in the same sprint as the framing rework); F3 billing test-mode run, dunning, GST invoices; F4 observability (metrics, silent-worker alert); F5 storage lifecycle; F6 security notes for each new feature; F7 a nightly render job on the real-footage set.

## Quick wins (under a day)
Stage-time and cost line on the job page; log the ASR engine chosen; SRT/VTT download of the caption words; completion email; show the suggested-cut fragments in the cleanup review;
crop-fill for a landscape ending; cap and sanitise any planner hints before D3.

## Do not build
A general timeline editor; auto-posting without the person's approval; auto-dubbing or voice cloning; face recognition of public figures to name people; remote blocks or fonts in the render browser;
LLM-written HTML/CSS; anything copied from OpenShorts `cloud/`. Do not widen a threshold on made-up data.

## Questions only the product owner can answer
1. Which footage may be kept as a test set, and who labels "who should be on screen"?
2. Who pays first: solo creators, agencies or newsrooms (teams, API or the approval sheet first)?
3. Is a mandatory approval step acceptable for newsrooms, with auto-render the default for creators?
4. Is translation or dubbing wanted, for which languages, and are voice clones acceptable?
5. Price and minutes per reel (how much GPU and LLM cost must B3 remove), and is a home-PC worker the main path or should a cloud GPU be planned?
