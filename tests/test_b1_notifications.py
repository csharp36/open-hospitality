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
