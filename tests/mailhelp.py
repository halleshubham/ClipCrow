"""Signing in during tests: ask for the emailed code, read it from the app's Outbox, send it back."""
import re

from clipcrow.service.mailer import Outbox  # noqa: F401  (re-exported for the tests)


def code_for(client, email):
    box = client.app.state.outbox
    msg = box.last(email.strip().lower())
    assert msg is not None, "no mail was sent"
    return re.match(r"(\d{6}) ", msg.subject).group(1)


def sign_up(client, email="a@example.com"):
    """Request a code and verify it: the account exists and the client holds the session cookie. Returns the verify response."""
    client.app.state.accounts._q("UPDATE login_sends SET at = at - 120")        # tests skip the 60 s wait between two mails to one address
    before = len(client.app.state.outbox.sent)
    r = client.post("/api/auth/code", json={"email": email})
    assert r.status_code == 200, r.text
    assert len(client.app.state.outbox.sent) == before + 1, "no new mail was sent"
    r = client.post("/api/auth/verify", json={"email": email, "code": code_for(client, email)})
    assert r.status_code == 200, r.text
    return r
