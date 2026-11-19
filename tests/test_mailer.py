"""Email: Resend and SMTP behind one interface, with a fallback; nothing from outside reaches a header or the markup unescaped."""
import io
import json
import logging

import pytest

from clipcrow.service import mailer as m
from clipcrow.service.mailer import FallbackMailer, MailError, MailQueue, Message, Outbox, ResendMailer, SmtpMailer


def msg(**kw):
    return Message(**{"to": "a@example.com", "subject": "Hello", "text": "Body", **kw})


def test_a_message_refuses_header_injection_and_bad_addresses():
    for bad in ("a@example.com\nBcc: x@y.zz", "a@example.com\r", "no-at-sign", "a b@example.com", "<a@example.com>", "a@example.com, b@example.com", ""):
        with pytest.raises(ValueError):
            msg(to=bad)
    with pytest.raises(ValueError):
        msg(subject="one\nBcc: evil@example.com")
    with pytest.raises(ValueError):
        Message(to="a@example.com", subject="s", text="t", surprise=1)                  # closed shape


def test_resend_gets_exactly_the_request_the_api_expects():
    seen = []
    r = ResendMailer("re_key", "ClipCrow <support@shackyapps.in>", "support@shackyapps.in", request=lambda body, idem: seen.append((body, idem)) or {})
    r.send(msg(html="<p>Hi</p>", idempotency_key="k1"))
    body, idem = seen[0]
    assert body == {"from": "ClipCrow <support@shackyapps.in>", "to": ["a@example.com"], "subject": "Hello", "text": "Body", "html": "<p>Hi</p>",
                    "reply_to": "support@shackyapps.in"} and idem == "k1"


def test_resend_retries_a_server_error_once_but_not_a_refusal():
    calls = []

    def flaky(body, idem):
        calls.append(idem)
        if len(calls) == 1:
            raise MailError("Resend answered 503")
    ResendMailer("k", "s@example.com", request=flaky).send(msg())
    assert len(calls) == 2 and calls[0] == calls[1]                                     # the same idempotency key, so no double mail

    def refuse(body, idem):
        calls.append("x")
        raise MailError("Resend answered 422")
    calls.clear()
    with pytest.raises(MailError):
        ResendMailer("k", "s@example.com", request=refuse).send(msg())
    assert len(calls) == 1


class FakeSMTP:
    sent, logins, tls = [], [], []

    def __init__(self, host, port, timeout=0):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        FakeSMTP.tls.append(context is not None and context.verify_mode.name == "CERT_REQUIRED")

    def login(self, user, password):
        FakeSMTP.logins.append(user)

    def send_message(self, message):
        FakeSMTP.sent.append(message)


def test_smtp_builds_a_utf8_message_with_tls_and_a_verified_certificate():
    FakeSMTP.sent, FakeSMTP.logins, FakeSMTP.tls = [], [], []
    s = SmtpMailer("smtp.example.com", 587, "user", "pw", "support@shackyapps.in", "support@shackyapps.in", factory=FakeSMTP)
    s.send(msg(subject="तुमचा कोड", text="तुमचा कोड: 123456", html="<p>तुमचा कोड</p>"))
    sent = FakeSMTP.sent[0]
    assert FakeSMTP.tls == [True] and FakeSMTP.logins == ["user"]
    assert sent["To"] == "a@example.com" and sent["Reply-To"] == "support@shackyapps.in"
    assert "तुमचा कोड" in sent.get_body(("plain",)).get_content() and sent.get_body(("html",)) is not None


def test_smtp_without_tls_is_only_for_the_local_machine():
    with pytest.raises(ValueError):
        SmtpMailer("smtp.example.com", 25, "", "", "s@example.com", security="none")
    SmtpMailer("127.0.0.1", 25, "", "", "s@example.com", security="none")
    with pytest.raises(ValueError):
        SmtpMailer("h", 1, "", "", "s@example.com", security="plain")


def test_a_failing_first_provider_falls_back_to_the_second_and_says_so(caplog):
    class Down(m.Mailer):
        name = "resend"

        def send(self, msg):
            raise MailError("Resend could not be reached")
    box = Outbox()
    with caplog.at_level(logging.INFO, logger="clipcrow.mail"):
        FallbackMailer(Down(), box).send(msg())
    assert len(box.sent) == 1 and "delivered by outbox" in caplog.text
    with pytest.raises(MailError):
        FallbackMailer(Down(), Down()).send(msg())


def test_a_failed_mail_is_logged_without_the_address_or_the_body(caplog):
    class Down(m.Mailer):
        name = "x"

        def send(self, msg):
            raise MailError("the SMTP server did not accept the mail")
    with caplog.at_level(logging.ERROR, logger="clipcrow.mail"):
        assert MailQueue(Down(), sync=True).send(msg(subject="secret subject", text="secret body")) is False
    assert "a@example.com" not in caplog.text and "secret" not in caplog.text and "sign-in" not in caplog.text.replace("mail (mail)", "")


def test_the_queue_sends_in_the_background():
    box = Outbox()
    q = MailQueue(box)
    q.send(msg())
    import time
    for _ in range(50):
        if box.sent:
            break
        time.sleep(0.05)
    assert len(box.sent) == 1


def test_from_env_picks_the_provider_and_refuses_a_half_set_configuration(monkeypatch):
    for k in ("CLIPCROW_EMAIL", "RESEND_API_KEY", "SMTP_HOST", "MAIL_FROM", "CLIPCROW_EMAIL_FALLBACK", "MAIL_REPLY_TO"):
        monkeypatch.delenv(k, raising=False)
    assert m.from_env() is None                                                          # off by default
    monkeypatch.setenv("RESEND_API_KEY", "re_x")
    with pytest.raises(ValueError, match="MAIL_FROM"):
        m.from_env()
    monkeypatch.setenv("MAIL_FROM", "ClipCrow <support@shackyapps.in>")
    assert m.from_env().name == "resend"
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("CLIPCROW_EMAIL_FALLBACK", "smtp")
    assert m.from_env().name == "resend+smtp"
    monkeypatch.setenv("CLIPCROW_EMAIL", "smtp")
    monkeypatch.delenv("CLIPCROW_EMAIL_FALLBACK")
    assert m.from_env().name == "smtp"
    monkeypatch.setenv("MAIL_FROM", "bad\nBcc: x@y.zz")
    with pytest.raises(ValueError):
        m.from_env()


def test_the_templates_escape_everything_and_stay_one_line_in_the_subject():
    mail = m.plain_mail("a@example.com", "Hi <script>alert(1)</script>", ["a & b <i>"], "x")
    assert "<script>" not in mail.html and "&lt;script&gt;" in mail.html and "a &amp; b &lt;i&gt;" in mail.html
    code = m.sign_in_code_mail("a@example.com", "123456", 10)
    assert code.subject == "123456 is your ClipCrow sign-in code" and "10 minutes" in code.text
    done = m.job_done_mail("a@example.com", False, "https://x.example/#/jobs/abc")
    assert "could not finish" in done.subject and "https://x.example/#/jobs/abc" in done.text
