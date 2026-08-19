"""Notification seam (Track B/B1, D-B6). A minimal Notifier interface + a dev
ConsoleNotifier that logs instead of sending. Real SMTP/SMS adapters are the B2
vendor matrix; this ships only the interface + the no-vendor default, selected
by configuration exactly like the payroll/CRM/photo-store seams."""

import logging
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


def notifier_from_settings(settings: Settings) -> Notifier:
    """Config-selected notifier. Only 'console' ships in B1; the SMTP/SMS
    adapters are B2. An unknown name fails fast (the payroll-provider posture)."""
    if settings.notifier == "console":
        return ConsoleNotifier()
    raise RuntimeError(
        f"unknown notifier {settings.notifier!r} (expected console; SMTP/SMS "
        "adapters land in B2)"
    )
