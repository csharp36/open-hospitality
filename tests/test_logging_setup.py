"""The serving process must emit its own `usali.*` logs.

Nothing in the app configured logging, so `usali.*` records propagated to
an unconfigured root logger and Python dropped everything below WARNING —
which silently swallowed the console notifier's OTP line in the deployed
demo (uvicorn's own INFO logs still appeared: uvicorn installs its own
handlers). `configure_logging` attaches a handler to the `usali` logger at
a configurable level so the app's records actually reach the platform log.
"""

import io
import logging

import pytest

from usali.logging_setup import USALI_HANDLER_NAME, configure_logging


@pytest.fixture(autouse=True)
def _restore_usali_logger():
    """Snapshot and restore the shared `usali` logger, and clear any global
    logging.disable() a prior test may have left set (a known footgun)."""
    logger = logging.getLogger("usali")
    saved_handlers = logger.handlers[:]
    saved_level = logger.level
    saved_propagate = logger.propagate
    saved_disabled = logger.disabled
    # Undo state a prior test may have leaked: a global logging.disable() and
    # a dictConfig(disable_existing_loggers=True) that flips .disabled on every
    # existing usali* logger (a known footgun in this suite). Prod never does
    # this — uvicorn configures with disable_existing_loggers=False.
    logging.disable(logging.NOTSET)
    for existing in logging.root.manager.loggerDict.values():
        if isinstance(existing, logging.Logger) and existing.name.startswith("usali"):
            existing.disabled = False
    try:
        yield
    finally:
        logger.handlers[:] = saved_handlers
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate
        logger.disabled = saved_disabled


def test_info_records_pass_at_info_level():
    """At INFO the notifier's OTP line (logged via usali.notifications at
    INFO) is enabled — the exact record that was being dropped."""
    configure_logging("INFO")
    assert logging.getLogger("usali.notifications").isEnabledFor(logging.INFO)


def test_a_lower_level_still_suppresses_info():
    """WARNING keeps INFO out — the level is honoured, not hard-coded."""
    configure_logging("WARNING")
    assert not logging.getLogger("usali.notifications").isEnabledFor(logging.INFO)


def test_the_record_actually_reaches_a_handler():
    """Not just 'enabled' — an INFO record emitted on a usali child logger
    is written by a handler on the usali logger (in prod: stdout → Cloud
    Run). Capture it via a probe handler to prove end-to-end delivery."""
    configure_logging("INFO")
    buf = io.StringIO()
    probe = logging.StreamHandler(buf)
    logging.getLogger("usali").addHandler(probe)
    try:
        logging.getLogger("usali.notifications").info("SMS to=%s body=%s", "+1555", "123456")
    finally:
        logging.getLogger("usali").removeHandler(probe)
    assert "SMS to=+1555 body=123456" in buf.getvalue()


def test_configuration_is_idempotent():
    """create_app/serve may run it more than once; it must not stack
    duplicate handlers on the usali logger."""
    configure_logging("INFO")
    configure_logging("INFO")
    ours = [
        h for h in logging.getLogger("usali").handlers
        if h.get_name() == USALI_HANDLER_NAME
    ]
    assert len(ours) == 1
