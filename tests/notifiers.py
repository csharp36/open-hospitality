"""A capturing Notifier fake for tests — records every message, sends nothing."""

from dataclasses import dataclass, field


@dataclass
class CapturingNotifier:
    emails: list[dict[str, str]] = field(default_factory=list)
    smses: list[dict[str, str]] = field(default_factory=list)

    def send_email(self, *, to: str, subject: str, body: str) -> None:
        self.emails.append({"to": to, "subject": subject, "body": body})

    def send_sms(self, *, to: str, body: str) -> None:
        self.smses.append({"to": to, "body": body})
