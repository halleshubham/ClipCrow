# Operating ClipCrow as a service

Written 2026-11-19 with the first SaaS pass (email sign-in, limits, operator page, alerts, backups, legal pages). A checklist to read before opening sign-ups.

## What is in place
- **Sign-in:** emailed code only (`docs/email-task.md`). A mail outage means nobody can sign in: set a backup provider (`CLIPCROW_EMAIL_FALLBACK`) and watch the mail alert.
- **Cost limits per person:** active jobs, jobs a day, minutes a job, new accounts a day; throw-away mail domains refused; a person's disabled account gets no code and no session. Not yet: a per-job AI-token budget (the planner's token use is metered and shown, but nothing stops a job at a ceiling), and a card-on-file requirement for the free plan.
- **Operator page** (`#/admin`, emails in `CLIPCROW_ADMINS`): jobs now, disk, sign-ups, mail, GPU worker and pod, a table of people with plan and minutes (change plan, add or remove minutes, disable), recent failures. Actions are logged with short ids only.
- **Alerts** (`CLIPCROW_ALERT_EMAIL`): disk under 5 GB or 10%, jobs waiting for 15 minutes with nothing running, 5 failures in an hour, GPU tasks nobody can take for 5 minutes, 3 mail failures in an hour. One mail per problem per hour, one when it clears. `GET /api/health` for an uptime monitor (point UptimeRobot or Better Stack at it).
- **Money mails:** payment received, payment failed (the plan stays while Razorpay retries), subscription cancelled, subscription ended. Razorpay sends the tax receipt.
- **Backups:** the database is copied once a day to `<data>/backups/` (seven kept). That protects against a damaged database, **not against losing the disk**: also snapshot the `/data` volume (Coolify or the host) and keep a copy off the machine. R2 holds finished clips when it is configured, not the database.

## Restore test (do it once before launch, then every quarter)
1. Stop the service. 2. Copy `<data>/backups/clipcrow-YYYYMMDD.db` over `<data>/clipcrow.db` and delete `clipcrow.db-wal` and `clipcrow.db-shm`. 3. Start; sign in; confirm the people, plans and minutes are there. 4. Write the date and the result in the dev log. A restore loses accounts made, and minutes used, since the copy.

## Billing: the test-mode run (needs your Razorpay test keys; not done)
1. Put the **test** key id, secret and webhook secret in the environment of a staging copy; create a test plan and map it in `plans.json`. 2. Subscribe with a test card; check the plan flips and the "payment received" mail arrives. 3. Use Razorpay's failing test card: the "could not take your payment" mail arrives and the plan stays; let the retries run out (or send `subscription.halted` from the dashboard): the plan drops to Free and the "ended" mail arrives. 4. Cancel: plan runs to the end date. 5. Buy a minutes pack: credits appear once. 6. Replay a webhook from the dashboard: nothing changes twice. 7. Only then switch to live keys.
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
