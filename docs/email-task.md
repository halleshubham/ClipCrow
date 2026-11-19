# Scope: email from ClipCrow (Resend or SMTP), and sign-in by email code only

Status: scoping only, nothing built. Written 2026-11-19, decisions added the same day. Today nothing sends email (`accounts.py` says so): no email check, no receipts, no "your clips are ready", and sign-in is by password.

## Decisions (from the product owner, 2026-11-19)
- **Sender:** `support@shackyapps.in` (`MAIL_FROM`). Reply-to the same address.
- **Resend is the default provider** (`CLIPCROW_EMAIL=resend`); SMTP is the backup or self-host path, selectable alone or as the fallback.
- **Job-finished mail only when the person opted in** (off by default, a switch in settings).
- **Sign-in is by emailed one-time code only. There is no password login.** Because sign-in itself depends on mail, email is now on the critical path: the fallback provider matters, and a mail outage must show a clear message instead of a dead sign-in page.

## Sign-in by email code (replaces the password screens, the reset flow and the separate verify mail)
One screen: enter your email, get a 6-digit code, enter it. **Sign-up and sign-in are the same flow**: an unknown address that proves it can read the mail gets an account, so "verify email" is the first sign-in and there is no password reset to build.
- **Code:** 6 digits from `secrets`, valid 10 minutes, stored only as a hash (`HMAC-SHA256` with a server key, so a copied table is useless), **single use**, at most **5 wrong tries** per code, then the code is dead and a new one is needed. A new code replaces the old one. Compared in constant time.
- **No account enumeration:** "If that address can sign in, a code is on its way" for every address; same response and similar timing. Signing up can be switched off (`CLIPCROW_ALLOW_SIGNUP`, exists today): then an unknown address gets the same answer and no mail.
- **Rate limits:** per address (at most 5 codes an hour, 15 a day), per IP, and a global brake so a flood cannot spend the Resend quota or get the domain flagged. A 60-second wait before "send again".
- **Session:** after a good code, the existing session cookie flow (`sessions` table, hashed token, expiry) is unchanged. Optional "remember this device" is the session length already in use; no new long-lived secret. Signing out everywhere deletes the user's sessions.
- **Email change:** a code to the **new** address, and one to the old one, before it switches. The address is the account's identity, so this is the one sensitive action.
- **Existing accounts (they have a password hash):** they keep their account by email: the first sign-in after the change is by code to the same address; `pw_hash` is no longer read and a later migration drops the column (it is `NOT NULL` today, so new accounts get an empty marker until then). The operator-token login used when accounts are off is untouched.
- **Login page copy** says what to do next in every case (code expired, too many tries, mail not arriving: check spam, ask for another code in a minute, or write to support@shackyapps.in). A visible "mail is not set up on this server" state when `CLIPCROW_EMAIL=off` in accounts mode (sign-in cannot work then; the server should refuse to start accounts mode without a mailer unless an operator override says so).
- **Table:** `login_codes(email_hash PRIMARY KEY, code_hash, expires_at, tries, created_at, sent_count_hour)`; the address itself is only in `users`.

## What it is for (in the order to build)
1. **The sign-in code** (replaces passwords, resets and verification; the first SaaS blocker). 2. **Billing:** payment received, payment failed, plan ending (with the Razorpay webhook, which stays the only thing that changes money).
3. **Job finished / failed** (opt-in per account; one mail per job, never per clip). 4. **Account:** an email-change code, account deleted, SocioBird disconnected, and (later) a sign-in alert from a new device.
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
- **Codes, not links:** the sign-in code above; nothing clickable authenticates anyone (a mail scanner that opens links cannot sign anyone in). The mail says the code, how long it lasts, and "if you did not ask for this, ignore it".
- **Secrets** (`RESEND_API_KEY`, `SMTP_PASSWORD`) are read from the environment, never printed, logged, committed, or put in a URL.
- Copy in Marathi or Hindi is Devanagari; the account's language (new field, default English) picks the template. Tests include a Devanagari subject.

## Delivery health (needed before real customers)
DNS for the sending domain: SPF, DKIM, DMARC (Resend gives the records; for SMTP the provider does). Bounces and complaints: Resend's signed webhook (verify the signature like the Razorpay one: raw body, constant-time compare) writes an address to an `email_suppressed` table; sending to a suppressed address is skipped and logged. An SMTP provider's bounces arrive by mail to the return path and are handled by hand at first.
Non-transactional mail would need an unsubscribe link and a list-unsubscribe header; none is planned.

## The work
1. `mailer.py` with the closed `Message`, both providers, the queue and the fake transports for tests (half a day to a day).
2. `login_codes`, the request-code and verify-code routes, the one sign-in screen replacing the password screens, the email-change flow (`docs/ui.md`: reuse `.card`, `label.field`, `.btn`; both themes; 360 px; the code box is a text input with `inputmode=numeric` and `autocomplete=one-time-code`) (one to two days).
3. Billing mails from the verified webhook handler, job-finished mail from the runner's end-of-job hook, an opt-in switch in settings (one day).
4. Docs and rules: the env table in `docs/deploy.md` and the README, a posting-style rule in `.claude/development-rules.md`, a dev-log entry (half a day).
5. Tests, no network: the Resend request is exactly what the API expects (headers, JSON, idempotency); the SMTP send reaches a local stub server (a thread with `socketserver` or `aiosmtpd`) with correct UTF-8 encoding and TLS settings; header-injection attempts are refused; a code works once, expires after 10 minutes, dies after 5 wrong tries, and is stored only hashed; the same response (and no mail) for known and unknown addresses and with sign-up off; the rate limits hold; an old password-account signs in by code; no password route is left; the fallback sends through the second provider and says so; `off` sends nothing; no secret appears in a log line or a response.
About 4 to 5 days in all (the sign-in rewrite is the larger part). Order against the other SaaS blockers: email and sign-in first, then legal pages, then the billing test run.

## Open questions
- Code length and lifetime (6 digits, 10 minutes proposed) and the daily cap per address (15 proposed).
- Should a signed-in session last 30 days (today's value is the existing session expiry) or be shorter now that every sign-in costs a mail?
- Should staff or test accounts be able to sign in without mail (a printed code in the server log, only in a development switch, never in production)?
