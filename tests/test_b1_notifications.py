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
    n = ConsoleNotifier()
    with caplog.at_level(logging.INFO):
        n.send_email(to="a@example.test", subject="Hi", body="Body")
        n.send_sms(to="+15550000000", body="123456")
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
