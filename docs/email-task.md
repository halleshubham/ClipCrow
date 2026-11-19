# Scope: email from ClipCrow, through Resend or any SMTP server

Status: scoping only, nothing built. Written 2026-11-19. Today nothing sends email (`accounts.py` says so): no password reset, no email check, no receipts, no "your clips are ready".

## What it is for (in the order to build)
1. **Password reset** and **email verification** (the two SaaS blockers). 2. **Billing:** payment received, payment failed, plan ending (with the Razorpay webhook, which stays the only thing that changes money).
3. **Job finished / failed** (opt-in per account; one mail per job, never per clip). 4. **Account:** sign-in from a new device (later), account deleted, SocioBird disconnected.
Marketing mail is out of scope. Every mail here is transactional.

## Two providers behind one small interface
`clipcrow/service/mailer.py`: `Mailer.send(Message) -> None` where `Message` is a closed model (`to` one address, `subject` up to 150 characters, `text`, optional `html`, `kind`, `idempotency_key`). Two implementations:
- **ResendMailer:** `POST https://api.resend.com/emails` over HTTPS with the key from `RESEND_API_KEY`, `Idempotency-Key` header, 15-second timeout, one retry on 5xx or timeout. A User-Agent of our own.
- **SmtpMailer:** `smtplib` with `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_SECURITY` (`starttls` default, `ssl`, or `none` only for 127.0.0.1), 15-second timeout, one connection per send, certificate verified (never disabled). Built with `email.message.EmailMessage` and a UTF-8 policy so a Devanagari subject and body are encoded correctly.
- **Choice:** `CLIPCROW_EMAIL=resend|smtp|off` (default `off`: the product behaves as today, and the reset and verify screens say email is not set up). Optional `CLIPCROW_EMAIL_FALLBACK=smtp` (or `resend`): when the first provider fails after its retry, the second sends; the log line says which one delivered. `MAIL_FROM` (an address on a domain with SPF, DKIM and DMARC) and `MAIL_REPLY_TO`.
- Sending never blocks a request: mails go through a small queue and worker thread (the `publish_pool` pattern); a failure is logged without the address or body and, for a reset, the person is told "if the address is registered a mail is on its way", the same answer either way.

## Rules (they come from CLAUDE.md)
- **Untrusted text never reaches a header or markup unescaped.** Reject CR, LF and NUL in `to` and `subject`; the HTML body is built from fixed templates with every value escaped; the plain-text part is always present. No raw job text, file names or channel names in a subject.
- **Links** are built from the configured public URL (`CLIPCROW_PUBLIC_URL`), never from the request's Host header. A reset or verify link carries a random token in the path or query of **our** domain only, and the page that opens it must not leak it (`Referrer-Policy: no-referrer` on that route).
- **Tokens:** 32 random bytes, stored only as a SHA-256 hash, single use, 30 minutes for a reset and 24 hours for a verify, bound to the user id, constant-time compare, the old ones deleted on use. A used or expired link says what to do next. New table `email_tokens(token_hash PRIMARY KEY, user_id, kind, expires_at, used_at)`.
- **No account enumeration:** same answer and similar timing whether the address exists; the existing per-address and per-IP rate limits apply to "send me a reset" and "send the verify mail again" (also a per-address cap per day).
- **Secrets** (`RESEND_API_KEY`, `SMTP_PASSWORD`) are read from the environment, never printed, logged, committed, or put in a URL.
- A changed password signs out every other session. Changing the email address needs the verify mail to the **new** address first.
- Copy in Marathi or Hindi is Devanagari; the account's language (new field, default English) picks the template. Tests include a Devanagari subject.

## Delivery health (needed before real customers)
DNS for the sending domain: SPF, DKIM, DMARC (Resend gives the records; for SMTP the provider does). Bounces and complaints: Resend's signed webhook (verify the signature like the Razorpay one: raw body, constant-time compare) writes an address to an `email_suppressed` table; sending to a suppressed address is skipped and logged. An SMTP provider's bounces arrive by mail to the return path and are handled by hand at first.
Non-transactional mail would need an unsubscribe link and a list-unsubscribe header; none is planned.

## The work
1. `mailer.py` with the closed `Message`, both providers, the queue and the fake transports for tests (half a day to a day).
2. `email_tokens` and the reset and verify routes plus their screens (`docs/ui.md`: reuse `.card`, `label.field`, `.btn`; both themes; 360 px) (one day).
3. Billing mails from the verified webhook handler, job-finished mail from the runner's end-of-job hook, an opt-in switch in settings (one day).
4. Docs and rules: the env table in `docs/deploy.md` and the README, a posting-style rule in `.claude/development-rules.md`, a dev-log entry (half a day).
5. Tests, no network: the Resend request is exactly what the API expects (headers, JSON, idempotency); the SMTP send reaches a local stub server (a thread with `socketserver` or `aiosmtpd`) with correct UTF-8 encoding and TLS settings; header-injection attempts are refused; a reset token works once, expires, and is stored only hashed; the same response for known and unknown addresses; the fallback sends through the second provider and says so; `off` sends nothing; no secret appears in a log line or a response.
About 3 to 4 days in all. Order against the other SaaS blockers: email first (reset and verify unblock sign-up), then legal pages, then the billing test run.

## Open questions
Which sending domain and address (`MAIL_FROM`)? Is Resend the default and SMTP the self-host or backup path, or the other way round? Are job-finished mails on by default or opt-in? Should the first release verify email at sign-up, or only offer password reset?
