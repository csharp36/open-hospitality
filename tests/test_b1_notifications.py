"""The Notifier seam: ConsoleNotifier is the config default; create_app accepts
an injected notifier and exposes it on app.state; an unknown USALI_NOTIFIER
value fails fast."""

import logging
from pathlib import Path

import pytest

from usali.config import Settings
from usali.notifications import ConsoleNotifier, notifier_from_settings
from usali.server import create_app
from tests.notifiers import CapturingNotifier


def test_console_notifier_logs_both_channels(caplog):
    # Neutralize two GLOBAL logging-state vectors that another test earlier in
    # the suite can leave set (e.g. a logging.config.dictConfig/fileConfig call
    # with the default disable_existing_loggers=True): the logger's own
    # `.disabled` flag and the process-wide manager.disable threshold. Both
    # short-circuit `isEnabledFor` before level/handler checks, so caplog would
    # capture nothing regardless of at_level. Restore them afterward.
    logger = logging.getLogger("usali.notifications")
    saved_disabled = logger.disabled
    saved_manager_disable = logging.root.manager.disable
    logger.disabled = False
    logging.disable(logging.NOTSET)  # resets manager.disable AND clears the level cache
    try:
        n = ConsoleNotifier()
        with caplog.at_level(logging.INFO, logger="usali.notifications"):
            n.send_email(to="a@example.test", subject="Hi", body="Body")
            n.send_sms(to="+15550000000", body="123456")
    finally:
        logger.disabled = saved_disabled
        logging.disable(saved_manager_disable)
    text = " ".join(r.message for r in caplog.records)
    assert "a@example.test" in text and "+15550000000" in text


def test_notifier_from_settings_selects_console_by_default():
    assert isinstance(notifier_from_settings(Settings(notifier="console")), ConsoleNotifier)


def test_notifier_from_settings_rejects_unknown():
    with pytest.raises(RuntimeError, match="unknown notifier"):
        notifier_from_settings(Settings(notifier="carrier-pigeon"))


def test_create_app_uses_injected_notifier(tmp_path: Path):
    fake = CapturingNotifier()
    app = create_app(notifier=fake, dist_dir=tmp_path / "nope")
    assert app.state.notifier is fake


# --- SmtpNotifier (B2 delivery) ---------------------------------------------
# The seam's first REAL adapter. Everything above proves the seam; these prove
# that selecting "smtp" produces something that actually hands a message to an
# SMTP server, and that a half-configured selection fails at STARTUP rather than
# at the first signup.


class _FakeSmtp:
    """Stands in for smtplib.SMTP: records the conversation, sends nothing."""

    instances: list["_FakeSmtp"] = []

    def __init__(self, host: str, port: int, timeout: float = 0) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.started_tls = False
        self.login_args: tuple[str, str] | None = None
        self.messages: list[object] = []
        self.quit_called = False
        _FakeSmtp.instances.append(self)

    def __enter__(self) -> "_FakeSmtp":
        return self

    def __exit__(self, *exc: object) -> None:
        self.quit_called = True

    def starttls(self) -> None:
        self.started_tls = True

    def login(self, user: str, password: str) -> None:
        self.login_args = (user, password)

    def send_message(self, msg: object) -> None:
        self.messages.append(msg)


@pytest.fixture
def fake_smtp(monkeypatch):
    import smtplib

    _FakeSmtp.instances = []
    monkeypatch.setattr(smtplib, "SMTP", _FakeSmtp)
    return _FakeSmtp


def _smtp_settings(**over: object) -> Settings:
    base: dict[str, object] = {
        "notifier": "smtp",
        "smtp_host": "smtp.example.test",
        "smtp_port": 587,
        "smtp_username": "apikey",
        "smtp_password": "s3cret",
        "smtp_from": "Open Hospitality <hello@example.test>",
    }
    base.update(over)
    return Settings(**base)  # type: ignore[arg-type]


def test_smtp_notifier_sends_an_email_over_starttls(fake_smtp):
    n = notifier_from_settings(_smtp_settings())
    n.send_email(to="owner@example.test", subject="Your invite", body="Open: https://x/y")
    (conn,) = fake_smtp.instances
    assert (conn.host, conn.port) == ("smtp.example.test", 587)
    assert conn.started_tls and conn.login_args == ("apikey", "s3cret")
    assert conn.quit_called
    (msg,) = conn.messages
    assert msg["To"] == "owner@example.test"
    assert msg["Subject"] == "Your invite"
    assert msg["From"] == "Open Hospitality <hello@example.test>"
    assert "Open: https://x/y" in msg.get_content()


def test_smtp_notifier_refuses_sms_loudly_rather_than_dropping_it(fake_smtp):
    # There is no SMS vendor. Silently swallowing a code the caller believes was
    # delivered is the failure mode that strands a signup with no way forward, so
    # this raises — every SMS caller must have been moved to email first.
    n = notifier_from_settings(_smtp_settings())
    with pytest.raises(RuntimeError, match="no SMS"):
        n.send_sms(to="+15550000000", body="123456")
    assert fake_smtp.instances == []


@pytest.mark.parametrize("missing", ["smtp_host", "smtp_from"])
def test_smtp_selection_without_required_settings_fails_fast(missing):
    # A blank host or From is a deploy mistake. Catch it when the notifier is
    # built (create_app) — not on the first owner who asks for an invite.
    with pytest.raises(RuntimeError, match="notifier='smtp' requires"):
        notifier_from_settings(_smtp_settings(**{missing: ""}))


def test_smtp_without_credentials_skips_login(fake_smtp):
    # An unauthenticated relay (an internal MTA, or Cloud Run's egress to a
    # network-authorised SMTP host) is legitimate: connect and send, no LOGIN.
    n = notifier_from_settings(_smtp_settings(smtp_username="", smtp_password=""))
    n.send_email(to="a@example.test", subject="s", body="b")
    (conn,) = fake_smtp.instances
    assert conn.login_args is None and conn.messages
