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

---

# SaaS launch and operations (added 2026-11-20)

The first SaaS pass is built (see the dev log, 2026-11-19 and 2026-11-20): email sign-in by code, pay-as-you-go billing, the landing page at `/`, the app at `/dashboard`, legal drafts, the Operator page and its settings, cost limits, alerts, per-user SocioBird, a sample job and welcome mail, and accounts in PostgreSQL. What is left is below. Same sizes as above (S under 3 days, M 1-2 weeks, L over 2 weeks). "Owner" means only the product owner can do it (an account, a key, a decision or a lawyer).

## Launch order
1. **G1-G3** (owner): email and DNS, Razorpay test-mode run, legal review. Nothing else matters until people can sign in, pay and agree to the terms.
2. **H1, H2, H3** (restore drill, error tracking, uptime monitor): so a failure is noticed and survivable.
3. **A private beta** with 5 to 20 real users (G8), watching the Operator page's Money section for real margins.
4. **H4-H9** while the beta runs.
5. **I1-I2** only when one machine is no longer enough (see the signals under I).

## G. Owner actions (cannot be built)
- **G1 Email.** `RESEND_API_KEY`, `MAIL_FROM`, `CLIPCROW_PUBLIC_URL`, `CLIPCROW_ADMINS`, `CLIPCROW_ALERT_EMAIL` in Coolify; SPF, DKIM and DMARC for `shackyapps.in`; a test sign-in from Gmail and Outlook that lands in the inbox. Without it nobody can sign in to the new build.
- **G2 Payments.** The Razorpay test-mode run (`docs/operations.md`), then live keys and the `payment_link.paid` webhook; GSTIN and business details for GST invoices; the refund policy made exact.
- **G3 Legal.** A lawyer reviews `legal.js` (`docs/legal-checklist.md`); entity name, address, grievance officer; the DPDP notice and consent wording.
- **G4 Prices.** Confirm Rs 8 a minute, the 3-minute minimum, the 10 free minutes, the bonus tiers and the margin target (all live-editable on the Operator page).
- **G5 SocioBird OAuth.** Register ClipCrow in SocioBird's Developer settings and run the one manual round trip with a second account (`docs/sociobird-per-user-task.md`, step 0).
- **G6 Sample job.** Export a good, consented recording with `clipcrow export-sample` and set `CLIPCROW_SAMPLE_DIR`.
- **G7 Secrets.** Rotate the production worker token (it is on third-party pods); back up `clipcrow.secret` with the database; keep every key out of chat and the repo.
- **G8 The beta list.** Who the first 5 to 20 users are, and how they will give feedback.
- **G9 The database move.** Create the PostgreSQL service (Coolify can make one, with scheduled backups), set `DATABASE_URL`, run `clipcrow migrate-db` once, redeploy (`docs/deploy.md`, "PostgreSQL").

## H. Build before the public launch
- **H1 Restore drill and offsite backups (S).** PostgreSQL backups taken by the database service (daily, kept off the machine) and one real restore into a scratch database; the same for `clipcrow.secret` and `plans.json`; a written runbook. The SQLite daily copy stays for single-user installs.
- **H2 Error tracking (S).** Sentry (or similar) behind `SENTRY_DSN`; strip request bodies, headers and cookies in `before_send` (they hold sign-in codes and media).
- **H3 Uptime and alert test (S).** A monitor on `/api/health`; fire each alert once on purpose (full disk, stuck queue, failing mail).
- **H4 Email change (S).** A code to the new address and one to the old before the account's address switches. Planned in `docs/email-task.md`, not built.
- **H5 Data export (M).** "Download my data": a zip of the person's jobs, clips, brand files and usage history, made in the background and mailed as a link. The Privacy text promises a copy on request.
- **H6 Money pages (M).** A usage and payments history page, a low-balance email (and one when a job is refused for lack of minutes), invoices listed on the Account page.
- **H7 Refund button (S-M).** On the Operator page: refund a Razorpay payment (the refund API), take back the minutes bought with it, write an audit row.
- **H8 Per-job AI budget (S-M).** A ceiling on AI spend per job (the planner already meters tokens): stop and refund instead of running away; show it on the Money section.
- **H9 Abuse handling (M).** A "report this content" route, a takedown procedure on the Operator page, the list of a person's signed-in devices with "sign out everywhere".
- **H10 Worker releases (M).** Whenever the render code changes, the pods and the home worker must be updated or they get no renders: build the worker image in CI, a version badge on the Operator page, one command to update a pod; a stale pod is shown there.
- **H11 Browser tests (S).** `scripts/e2e_accounts.js` still drives the old password screens; rewrite it for the code sign-in and add the New job, recharge and posting flows.
- **H12 Consent record (S).** Store when each account accepted the Terms and Privacy Policy and which version.

## I. Reliability and scale
Signals that mean "do I1-I2 now": queue waits over 15 minutes at busy hours, the data disk over 70%, a deploy blocked for a long time by a running job, or more than about 30 people active at once.
- **I1 Jobs, brands, pictures, styles and schedules out of local files (L).** The accounts, minutes, payments and SocioBird links are in PostgreSQL now; a job's state is still a JSON file on one volume. For more than one app instance, move job state to the database and keep files in R2. Include per-user quotas on stored bytes.
- **I2 Several app instances and zero-downtime deploys (L).** Needs I1; a job table with a claim (`SELECT ... FOR UPDATE SKIP LOCKED`) instead of an in-process queue, a shared worker hub, and a rolling deploy.
- **I3 A second GPU provider (M).** The on-demand supply answered 503 for hours. Put the pod controller behind a provider interface and add one more provider (or a reserved pod).
- **I4 Framing on the worker (M).** Only if the parallel framing on the server (built) is not enough for long videos: measure the next long job first (`docs/framing-worker-task.md`).
- **I5 R2 housekeeping (S).** Lifecycle rules for abandoned multipart uploads and expired objects, an egress and storage cost line on the Operator page.
- **I6 A load test (M).** 20 people uploading at once against a staging copy; find the real limits of the queue, the disk and the database pool (`CLIPCROW_DB_POOL`).
- **I7 Logs (S).** Structured logs, rotation, and a way to read them without the server shell.

## J. After launch
- **J1 Support tooling (M).** A shared support inbox, a status page, in-app help and a contact form, and "view as this person" for the operator with the person's consent and an audit row.
- **J2 Growth (M).** A privacy-friendly funnel (visit, sign-up, first job, first recharge), promo codes and referrals, a first-run tour on New job.
- **J3 A Marathi and Hindi interface (M-L).** The app and the landing page are English only; most users speak Marathi or Hindi.
- **J4 Public API, keys and webhooks, teams and roles (F1, F2 above).** Needed for agencies; the biggest authorisation change, so not in the same sprint as I1.
- **J5 Mobile web polish (S-M).** An installable PWA, uploads that survive a flaky phone connection.
- **J6 Video quality (see A above).** Caption legibility, audio level between clips, the black seam before the ending video, framing checked on a real-footage set, who-is-who voice separation.

## Done on 2026-11-19 and 2026-11-20 (for the record)
Email (Resend, SMTP, fallback), sign-in by emailed code, job-finished mail (opt-in), money mails; the pay-as-you-go catalogue (Rs 8, a 3-minute minimum, bonus tiers, free minutes) and recharge; cost limits, sign-up cap, throw-away mail refusal; the Operator page, its Money section (margin per job, suggested price) and about 35 live settings; health, alerts and a daily SQLite copy; Terms and Privacy drafts; the landing page (`/`) with two use cases and a cost estimator, the app at `/dashboard`; a sample job and welcome mail; per-user SocioBird (a pasted key, then OAuth); parallel framing; accounts in PostgreSQL with `clipcrow migrate-db`.
