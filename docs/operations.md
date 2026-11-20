# Operating ClipCrow as a service

Written 2026-11-19 with the first SaaS pass (email sign-in, limits, operator page, alerts, backups, legal pages). A checklist to read before opening sign-ups.

## What is in place
- **Sign-in:** emailed code only (`docs/email-task.md`). A mail outage means nobody can sign in: set a backup provider (`CLIPCROW_EMAIL_FALLBACK`) and watch the mail alert.
- **Cost limits per person:** active jobs, jobs a day, minutes a job, new accounts a day; throw-away mail domains refused; a person's disabled account gets no code and no session. Not yet: a per-job AI-token budget (the planner's token use is metered and shown, but nothing stops a job at a ceiling), and a card-on-file requirement for the free plan.
- **Operator page** (`#/admin`, emails in `CLIPCROW_ADMINS`): jobs now, disk, sign-ups, mail, GPU worker and pod, a table of people with plan and minutes (change plan, add or remove minutes, disable), recent failures. Actions are logged with short ids only.
- **Alerts** (`CLIPCROW_ALERT_EMAIL`): disk under 5 GB or 10%, jobs waiting for 15 minutes with nothing running, 5 failures in an hour, GPU tasks nobody can take for 5 minutes, 3 mail failures in an hour. One mail per problem per hour, one when it clears. `GET /api/health` for an uptime monitor (point UptimeRobot or Better Stack at it).
- **Money mails:** payment received, payment failed (the plan stays while Razorpay retries), subscription cancelled, subscription ended. Razorpay sends the tax receipt.
- **Backups:** the database is copied once a day to `<data>/backups/` (seven kept). That protects against a damaged database, **not against losing the disk**: also snapshot the `/data` volume (Coolify or the host) and keep a copy off the machine. R2 holds finished clips when it is configured, not the database.

## What a job costs to make (measured, 19 November 2026)
From the production server's 37 finished jobs (`scripts/job_costs.py` re-runs this): the AI is almost all of it, at the model's price and Rs 88 to the dollar.
| Recording | Reels | AI cost | Per minute of video |
|---|---|---|---|
| 20 min interview | 6 to 10 | Rs 66 to 79 | Rs 3.3 to 3.9 |
| 30 min talk | 6 | Rs 95 | Rs 3.2 |
| 53 min podcast (4 runs) | 10 | Rs 135 to 143 | Rs 2.6 |
| 61 min | 10 | Rs 173 | Rs 2.9 |
| 76 min (with the pod's GPU Rs 53) | 10 | Rs 194 + Rs 56 GPU | Rs 3.3 |
| 87 min | 10 | Rs 210 | Rs 2.4 |
| a 1 min raw take (23 runs, 0.3 to 1.7 min) | 1 | Rs 3 to 9 | about Rs 6 (a fixed Rs 4 to 6 per take, so short takes cost more a minute) |
Long recordings cost about **Rs 2.4 to 3.9 a minute**; at Rs 8 a minute that is a 50 to 70% profit share (a 105% to 230% markup) before Razorpay's fee (about 2.4%) and the server. A 1-minute raw take costs about Rs 5.5 and is charged the 3-minute minimum (Rs 24), which is a 340% markup. Without a minimum it would be Rs 8, a 45% markup. A free 10-minute trial costs about Rs 17 to 35 in AI.
Caveats: only 2 of the 37 jobs have metered GPU time (metering is recent; earlier jobs ran on the server's CPU or the home GPU, which costs electricity, not rupees on a bill); the server, storage and bandwidth are not counted; the raw takes tested were all under 2 minutes, so a 30-minute raw recording (the AI reads the whole transcript to judge retakes) will cost more than the table suggests: measure one before promising a price for long raw takes; my own test runs are in the data.

## Restore test, PostgreSQL (when `DATABASE_URL` is set)
The daily file copy below is SQLite's and does nothing for PostgreSQL. Use the database service's backups (Coolify: the database's Backups tab, to R2 or S3), keep one off the machine, and once before launch restore the newest into a scratch database (`createdb scratch; pg_restore -d scratch dump` or the service's restore), start a staging copy of ClipCrow with `DATABASE_URL` pointing at it, and check people, plans and minutes are there. Write the date and the result in the dev log.

## Restore test, SQLite (do it once before launch, then every quarter)
1. Stop the service. 2. Copy `<data>/backups/clipcrow-YYYYMMDD.db` over `<data>/clipcrow.db` and delete `clipcrow.db-wal` and `clipcrow.db-shm`. 3. Start; sign in; confirm the people, plans and minutes are there. 4. Write the date and the result in the dev log. A restore loses accounts made, and minutes used, since the copy.

## Billing: the test-mode run (needs your Razorpay test keys; not done)
The product is one pay-as-you-go plan (`docs/deploy.md`, Pricing). 1. Put the **test** key id, secret and webhook secret in the environment of a staging copy (the webhook needs `payment_link.paid`). 2. Sign up: the balance shows the free minutes. 3. Recharge Rs 300 with a test card: the minutes appear once and the "payment received" mail arrives; recharge Rs 2000: 5% bonus minutes. 4. Pay with Razorpay's failing test card: nothing is added. 5. Replay the webhook from the dashboard: nothing is added twice. 6. Upload a video longer than the balance: the 402 message says to recharge. 7. Only then switch to live keys.
**Still to decide or build:** GST invoices (Razorpay can issue invoices for subscriptions; if you register for GST you need your GSTIN on them and a place on the Account page to download them), a refund procedure (a button on the operator page that calls Razorpay's refund API, and gives the minutes back), and the pricing in `plans.json` (the built-in numbers are placeholders).

## Reliability
- One container, one disk. A deploy drains: it waits up to `DRAIN_TIMEOUT_SECONDS` (45) for the running job, otherwise the job is requeued once. With real traffic, schedule deploys for quiet hours (the operator page shows jobs running).
- GPU supply is the weak point: the on-demand provider answers 503 for hours at times. The order is on-demand pods, then the home GPU, then the server's CPU; the alerts above fire when tasks are stuck.
- Error tracking: not set up. If you want it, Sentry's Python SDK can be added behind `SENTRY_DSN` (strip request bodies, headers and cookies in `before_send`: they hold sign-in codes and media).

## Before opening sign-ups
- [ ] `RESEND_API_KEY`, `MAIL_FROM`, `CLIPCROW_PUBLIC_URL` set; DNS (SPF, DKIM, DMARC) verified in Resend; a test sign-in from a Gmail and an Outlook address lands in the inbox.
- [ ] `CLIPCROW_ADMINS` and `CLIPCROW_ALERT_EMAIL` set; an alert mail received once (fill a test disk or stop the runner).
- [ ] Uptime monitor on `/api/health`.
- [ ] Restore test done. Volume snapshot schedule on.
- [ ] Terms and Privacy reviewed by a lawyer, entity and grievance officer filled in (`docs/legal-checklist.md`).
- [ ] Billing test-mode run done; live keys in; prices in `plans.json`.
- [ ] Limits chosen: the defaults suit a small beta, not a launch.
- [ ] The production worker token is not on third-party pods any more, or has been rotated.
