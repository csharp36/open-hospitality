"""Building and posting a signed intake message — shared by the webhook tests
and the property-endpoint tests.

It lives here because two modules now send mail to an address: OH-23 Task 3's
`tests/test_intake_email.py`, which is about the webhook itself, and Task 4's
`tests/test_intake_address_api.py`, which sends one message to a rotated
address to show the operator what a stale PMS looks like. One signing helper,
so the two cannot drift into two ideas of what the worker sends (D-OH23.1).
"""

import hashlib
import hmac
import time
from email.message import EmailMessage

from usali.config import get_settings

_SECRET = get_settings().email_intake_secret

SENDER = "nightaudit@pms.test"


def _attach(msg, name, data):
    subtype = "pdf" if name.lower().endswith(".pdf") else "octet-stream"
    msg.add_attachment(data, maintype="application", subtype=subtype, filename=name)


def _message(*, subject="Night audit 2026-07-07", message_id="<one@pms.test>",
             attachments=()):
    """A whole RFC822 message, as bytes — what the worker forwards verbatim."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["From"] = SENDER
    msg.set_content("Tonight's reports are attached.")
    for name, data in attachments:
        _attach(msg, name, data)
    return msg.as_bytes()


def _post(client, raw, *, to, frm=SENDER, auth=None, secret=_SECRET, timestamp=None):
    """Sign and post exactly as the worker does (D-OH23.1/.2).

    `auth=None` means an ALIGNED pass for the envelope sender's own domain,
    which is what `sender_allowed` requires since the 2026-09-21 review
    decision (D-OH23.4) — a bare `dkim=pass` carries no `header.d` and vouches
    for nobody, so it would make every case a `sender_rejected`. The domain is
    read off `frm` after stripping angle brackets, the same trimming
    `intake._domain_of` does.
    """
    if auth is None:
        domain = frm.strip().strip("<>").strip().rpartition("@")[2]
        auth = f"dkim=pass header.d={domain}"
    stamp = str(int(time.time())) if timestamp is None else timestamp
    digest = hmac.new(
        secret.encode("utf-8"), stamp.encode("utf-8") + b"\n" + raw, hashlib.sha256
    ).hexdigest()
    return client.post(
        "/api/intake/email",
        content=raw,
        headers={
            "Content-Type": "message/rfc822",
            "X-Intake-Timestamp": stamp,
            "X-Intake-Signature": f"sha256={digest}",
            "X-Intake-To": to,
            "X-Intake-From": frm,
            "X-Intake-Auth": auth,
        },
    )
