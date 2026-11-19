"""Transactional email: Resend (default) or any SMTP server, behind one small interface.

Rules (CLAUDE.md): nothing from a person or a model reaches a header or the markup unescaped; the keys come from the environment and are never
logged or put in a URL; a failure is logged without the address or the body. Mails go through a small queue and a worker thread so a request
never waits for the provider.

``CLIPCROW_EMAIL``: ``resend`` | ``smtp`` | ``off`` (the default is ``resend`` when ``RESEND_API_KEY`` is set, ``smtp`` when ``SMTP_HOST`` is
set, else off). ``CLIPCROW_EMAIL_FALLBACK``: the other provider, used when the first fails after its retry.
"""
from __future__ import annotations

import html
import json
import logging
import os
import queue
import re
import smtplib
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from typing import Callable, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

log = logging.getLogger("clipcrow.mail")

ADDRESS = re.compile(r"^[^@\s<>\"',;]{1,64}@[^@\s<>\"',;]{1,190}\.[^@\s<>\"',;]{2,24}\Z")
_BAD = re.compile(r"[\r\n\x00]")
USER_AGENT = "ClipCrow-mail/1.0"
RESEND_URL = "https://api.resend.com/emails"


class MailError(RuntimeError):
    """A mail could not be sent. The message is safe to log (no address, no body)."""


class Message(BaseModel):
    """One mail. Closed shape: header injection (CR, LF, NUL) is refused, not stripped."""
    model_config = ConfigDict(extra="forbid")
    to: str
    subject: str = Field(min_length=1, max_length=150)
    text: str = Field(min_length=1, max_length=20000)
    html: Optional[str] = Field(default=None, max_length=60000)
    kind: str = Field(default="mail", max_length=40)
    idempotency_key: str = Field(default="", max_length=80)

    @field_validator("to")
    @classmethod
    def _to(cls, v: str) -> str:
        v = v or ""
        if _BAD.search(v) or not ADDRESS.match(v.strip()):
            raise ValueError("not an email address")
        return v.strip()

    @field_validator("subject")
    @classmethod
    def _subject(cls, v: str) -> str:
        if _BAD.search(v):
            raise ValueError("a subject is one line")
        return v


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


class Mailer:
    name = "mailer"

    def send(self, msg: Message) -> None:                    # pragma: no cover - interface
        raise NotImplementedError


class ResendMailer(Mailer):
    name = "resend"

    def __init__(self, api_key: str, sender: str, reply_to: str = "", *, request: Callable[..., dict] | None = None, retries: int = 1):
        if not api_key:
            raise ValueError("Resend needs an API key")
        self._key, self.sender, self.reply_to, self.retries = api_key, sender, reply_to, retries
        self._request = request or self._http

    def _http(self, body: dict, idem: str) -> dict:
        req = urllib.request.Request(RESEND_URL, method="POST", data=json.dumps(body).encode(), headers={
            "Authorization": f"Bearer {self._key}", "Content-Type": "application/json", "User-Agent": USER_AGENT, "Idempotency-Key": idem})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:                 # noqa: S310 (fixed https URL)
                return json.loads(r.read(200_000) or b"{}")
        except urllib.error.HTTPError as e:
            raise MailError(f"Resend answered {e.code}") from None
        except (urllib.error.URLError, TimeoutError, ValueError):
            raise MailError("Resend could not be reached") from None

    def send(self, msg: Message) -> None:
        body = {"from": self.sender, "to": [msg.to], "subject": msg.subject, "text": msg.text}
        if msg.html:
            body["html"] = msg.html
        if self.reply_to:
            body["reply_to"] = self.reply_to
        idem = msg.idempotency_key or uuid.uuid4().hex
        last: Exception | None = None
        for _ in range(self.retries + 1):
            try:
                self._request(body, idem)
                return
            except MailError as e:
                last = e
                if "answered 4" in str(e) and "answered 429" not in str(e):      # a refusal of the request itself is not retried
                    break
        raise MailError(str(last))


class SmtpMailer(Mailer):
    name = "smtp"

    def __init__(self, host: str, port: int, user: str, password: str, sender: str, reply_to: str = "", security: str = "starttls",
                 *, factory: Callable[..., smtplib.SMTP] | None = None):
        if security not in ("starttls", "ssl", "none"):
            raise ValueError("SMTP_SECURITY is starttls, ssl or none")
        if security == "none" and host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("SMTP without TLS is only allowed to the local machine")
        self.host, self.port, self.user, self._password, self.sender, self.reply_to, self.security = host, port, user, password, sender, reply_to, security
        self._factory = factory

    def _build(self, msg: Message) -> EmailMessage:
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = self.sender, msg.to, msg.subject
        if self.reply_to:
            m["Reply-To"] = self.reply_to
        m["Message-ID"] = f"<{uuid.uuid4().hex}@{self.sender.rsplit('@', 1)[-1].strip('>')}>"
        m.set_content(msg.text)
        if msg.html:
            m.add_alternative(msg.html, subtype="html")
        return m

    def send(self, msg: Message) -> None:
        m = self._build(msg)
        try:
            ctx = ssl.create_default_context()
            if self._factory:
                smtp = self._factory(self.host, self.port, timeout=15)
            elif self.security == "ssl":
                smtp = smtplib.SMTP_SSL(self.host, self.port, timeout=15, context=ctx)
            else:
                smtp = smtplib.SMTP(self.host, self.port, timeout=15)
            with smtp:
                if self.security == "starttls":
                    smtp.starttls(context=ctx)
                if self.user:
                    smtp.login(self.user, self._password)
                smtp.send_message(m)
        except (smtplib.SMTPException, OSError, ssl.SSLError):
            raise MailError("the SMTP server did not accept the mail") from None


class FallbackMailer(Mailer):
    """The first provider, then the second when the first fails; the log says which one delivered."""

    def __init__(self, first: Mailer, second: Mailer):
        self.first, self.second, self.name = first, second, f"{first.name}+{second.name}"

    def send(self, msg: Message) -> None:
        try:
            self.first.send(msg)
        except MailError:
            log.warning("mail: %s failed, trying %s", self.first.name, self.second.name)
            self.second.send(msg)
            log.info("mail: delivered by %s", self.second.name)


class Outbox(Mailer):
    """Tests and development: keeps what would have been sent."""
    name = "outbox"

    def __init__(self):
        self.sent: list[Message] = []

    def send(self, msg: Message) -> None:
        self.sent.append(msg)

    def last(self, to: str = "") -> Optional[Message]:
        return next((m for m in reversed(self.sent) if not to or m.to == to), None)


class MailQueue:
    """Sends in the background so a request never waits for the provider; a failure is logged (no address, no body)."""

    def __init__(self, mailer: Mailer, *, sync: bool = False):
        self.mailer, self.sync = mailer, sync
        self._failed: list[float] = []                                 # times of recent failures (for the operator's alert)
        self._q: "queue.Queue[Message]" = queue.Queue(maxsize=500)
        if not sync:
            threading.Thread(target=self._run, name="clipcrow-mail", daemon=True).start()

    def send(self, msg: Message) -> bool:
        if self.sync:
            return self._deliver(msg)
        try:
            self._q.put_nowait(msg)
            return True
        except queue.Full:
            log.error("mail queue is full: a %s mail was dropped", msg.kind)
            return False

    def _deliver(self, msg: Message) -> bool:
        try:
            self.mailer.send(msg)
            return True
        except MailError as e:
            log.error("mail (%s) not sent: %s", msg.kind, e)
            self._failed.append(time.time())
            return False
        except Exception:                                              # noqa: BLE001 - a mail must never take a worker down
            log.exception("mail (%s) not sent", msg.kind)
            self._failed.append(time.time())
            return False

    def failures_last_hour(self) -> int:
        cut = time.time() - 3600
        self._failed = [t for t in self._failed if t >= cut]
        return len(self._failed)

    def _run(self) -> None:
        while True:
            self._deliver(self._q.get())


def sender_address() -> str:
    raw = os.environ.get("MAIL_FROM", "").strip()
    if not raw:
        return ""
    name, addr = parseaddr(raw)
    if _BAD.search(raw) or not ADDRESS.match(addr):
        raise ValueError("MAIL_FROM must be an address like support@example.com or Name <support@example.com>")
    return formataddr((name, addr)) if name else addr


def _one(kind: str, sender: str, reply_to: str) -> Optional[Mailer]:
    env = os.environ
    if kind == "resend":
        return ResendMailer(env.get("RESEND_API_KEY", ""), sender, reply_to) if env.get("RESEND_API_KEY") else None
    if kind == "smtp":
        if not env.get("SMTP_HOST"):
            return None
        return SmtpMailer(env["SMTP_HOST"], int(env.get("SMTP_PORT", "587")), env.get("SMTP_USER", ""), env.get("SMTP_PASSWORD", ""), sender, reply_to,
                          env.get("SMTP_SECURITY", "starttls"))
    return None


def from_env() -> Optional[Mailer]:
    """The configured mailer, or None when email is off. Raises ValueError for a half-set configuration (better a clear stop than silent mail loss)."""
    env = os.environ
    kind = (env.get("CLIPCROW_EMAIL") or ("resend" if env.get("RESEND_API_KEY") else "smtp" if env.get("SMTP_HOST") else "off")).strip().lower()
    if kind == "off":
        return None
    if kind not in ("resend", "smtp"):
        raise ValueError("CLIPCROW_EMAIL is resend, smtp or off")
    sender = sender_address()
    if not sender:
        raise ValueError("email is on, so MAIL_FROM is needed (for example support@shackyapps.in)")
    reply_to = env.get("MAIL_REPLY_TO", "").strip() or sender
    first = _one(kind, sender, reply_to)
    if first is None:
        raise ValueError("CLIPCROW_EMAIL=%s needs %s" % (kind, "RESEND_API_KEY" if kind == "resend" else "SMTP_HOST"))
    other = (env.get("CLIPCROW_EMAIL_FALLBACK") or "").strip().lower()
    if other and other not in ("resend", "smtp", "off"):
        raise ValueError("CLIPCROW_EMAIL_FALLBACK is resend, smtp or off")
    second = _one(other, sender, reply_to) if other and other != kind and other != "off" else None
    return FallbackMailer(first, second) if second else first


# ---- the mails themselves: fixed templates, every value escaped -------------------------------------------------------------------------------

def _page(heading: str, lines: list[str], big: str = "") -> tuple[str, str]:
    text = "\n\n".join([heading, *([big] if big else []), *lines])
    body = "".join(f'<p style="margin:0 0 14px">{esc(x)}</p>' for x in lines)
    code = f'<p style="font-size:32px;letter-spacing:6px;font-weight:700;margin:6px 0 18px">{esc(big)}</p>' if big else ""
    page = (f'<div style="font-family:system-ui,Segoe UI,Arial,sans-serif;font-size:15px;line-height:1.5;color:#14151B;max-width:480px">'
            f'<h2 style="margin:0 0 14px;font-size:18px">{esc(heading)}</h2>{code}{body}</div>')
    return text, page


def sign_in_code_mail(to: str, code: str, minutes: int) -> Message:
    text, page = _page("Your ClipCrow sign-in code",
                       [f"Enter this code to sign in. It works once and expires in {minutes} minutes.",
                        "If you did not ask for it, ignore this mail: nobody can sign in without the code.",
                        "Questions? Write to support@shackyapps.in."], big=code)
    return Message(to=to, subject=f"{code} is your ClipCrow sign-in code", text=text, html=page, kind="sign-in-code")


def job_done_mail(to: str, ok: bool, link: str) -> Message:
    head = "Your clips are ready" if ok else "Your job could not finish"
    lines = ["Open it to watch, edit and download the clips." if ok else "Open it to see what went wrong; the minutes were given back."] + ([link] if link else [])
    text, page = _page(head, lines)
    return Message(to=to, subject=head, text=text, html=page, kind="job-done")


def plain_mail(to: str, heading: str, lines: list[str], kind: str) -> Message:
    text, page = _page(heading, lines)
    return Message(to=to, subject=heading[:150], text=text, html=page, kind=kind)


def billing_mail(to: str, kind: str, detail: str) -> Message:
    """Receipts and failures. Only fixed words and a plan or pack name from our own catalogue go in; Razorpay sends the tax receipt itself."""
    if kind == "paid":
        return plain_mail(to, "Payment received, thank you", [f"We received your payment for {detail}." if detail else "We received your payment.",
                                                              "Razorpay emails you the receipt and the invoice. Your minutes are on your Account page."], "billing-paid")
    if kind == "payment-failed":
        return plain_mail(to, "We could not take your payment", ["Your last payment did not go through. Your plan stays active while the payment service tries again.",
                                                                 "Please check your payment method with your bank or card issuer. If it keeps failing the plan ends and you move to Free. Write to support@shackyapps.in if you need help."], "billing-failed")
    if kind == "ended":
        return plain_mail(to, "Your ClipCrow plan has ended", ["Your subscription has ended, so your account is now on the Free plan. Your jobs and clips stay until their normal expiry.",
                                                               "You can subscribe again on your Account page."], "billing-ended")
    return plain_mail(to, "Your subscription is cancelled", ["Your plan stays active until the end of the period you paid for, then your account moves to Free."], "billing-cancelled")


def welcome_mail(to: str, minutes: float, site: str, sample_link: str) -> Message:
    """Sent once, when an address signs in for the first time. Fixed words; the plan's minutes and our own links are the only values."""
    lines = [f"Your account is ready on the Free plan: {minutes:g} minutes of video a month.",
             "To make clips: drop a long video on the New job page, choose its language, how many clips and how long, and press Generate clips. Pictures, your logo, an ending video and a watch-the-full-video banner are on the same page.",
             *([f"Open the sample job to see what ClipCrow makes from a real recording: {sample_link}"] if sample_link else []),
             *([f"Start here: {site}"] if site else []),
             "Questions or ideas? Reply to this mail or write to support@shackyapps.in. We read everything."]
    text, page = _page("Welcome to ClipCrow", lines)
    return Message(to=to, subject="Welcome to ClipCrow", text=text, html=page, kind="welcome")
