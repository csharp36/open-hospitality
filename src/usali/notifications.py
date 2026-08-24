"""Notification seam (Track B/B1, D-B6). A minimal Notifier interface, a dev
ConsoleNotifier that logs instead of sending, and SmtpNotifier -- the first real
adapter (B2 email). Selected by configuration exactly like the payroll/CRM/
photo-store seams.

Email is the whole delivery story for now. There is no SMS vendor, so
SmtpNotifier.send_sms RAISES rather than silently dropping a code the caller
believes was delivered; every code path that must reach a stranger sends email.
"""

import logging
import smtplib
from email.message import EmailMessage
from typing import Protocol

from usali.config import Settings

logger = logging.getLogger("usali.notifications")


class Notifier(Protocol):
    def send_email(self, *, to: str, subject: str, body: str) -> None: ...
    def send_sms(self, *, to: str, body: str) -> None: ...


class ConsoleNotifier:
    """Dev/test default: logs the message (link/code visible in the console) and
    sends nothing. No vendor, fully testable."""

    def send_email(self, *, to: str, subject: str, body: str) -> None:
        logger.info("EMAIL to=%s subject=%s body=%s", to, subject, body)

    def send_sms(self, *, to: str, body: str) -> None:
        logger.info("SMS to=%s body=%s", to, body)


class SmtpNotifier:
    """Vendor-neutral SMTP email (B2). Any relay that speaks submission works --
    SendGrid, Mailgun, Postmark, SES, a self-hosted MTA -- so choosing a vendor
    is a deploy-time credential change, not a code change.

    One connection PER MESSAGE. Volume here is a handful of invites and codes a
    day, and a long-lived connection across an idle Cloud Run instance is the
    thing that breaks (the relay hangs up and the next send fails); reconnecting
    is both simpler and more reliable at this scale.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        sender: str,
        starttls: bool = True,
        timeout: float = 15.0,
    ) -> None:
        self._host, self._port = host, port
        self._username, self._password = username, password
        self._sender = sender
        self._starttls = starttls
        self._timeout = timeout

    def send_email(self, *, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"] = self._sender
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        # smtplib.SMTP is looked up on the module (not imported by name) so a
        # test can substitute it without reaching into this module's globals.
        with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as conn:
            if self._starttls:
                conn.starttls()
            # An empty username means a network-authorised relay: connect and
            # send with no LOGIN, rather than offering blank credentials.
            if self._username:
                conn.login(self._username, self._password)
            conn.send_message(msg)
        logger.info("EMAIL sent to=%s subject=%s", to, subject)

    def send_sms(self, *, to: str, body: str) -> None:
        raise RuntimeError(
            "no SMS vendor is configured; SmtpNotifier delivers email only. "
            "Move this caller to send_email, or add an SMS adapter."
        )


def notifier_from_settings(settings: Settings) -> Notifier:
    """Config-selected notifier: 'console' (dev, logs only) or 'smtp' (B2 email).
    An unknown name -- or a half-configured 'smtp' -- fails fast at build time
    (the payroll-provider posture), so a deploy mistake surfaces at startup
    instead of on the first owner who asks for an invite."""
    if settings.notifier == "console":
        return ConsoleNotifier()
    if settings.notifier == "smtp":
        missing = [
            name for name, value in
            (("USALI_SMTP_HOST", settings.smtp_host), ("USALI_SMTP_FROM", settings.smtp_from))
            if not value
        ]
        if missing:
            raise RuntimeError(
                f"notifier='smtp' requires {' and '.join(missing)}; "
                "signup links and codes would go nowhere"
            )
        return SmtpNotifier(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            sender=settings.smtp_from,
            starttls=settings.smtp_starttls,
            timeout=settings.smtp_timeout_seconds,
        )
    raise RuntimeError(
        f"unknown notifier {settings.notifier!r} (expected console|smtp)"
    )
