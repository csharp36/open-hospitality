"""Application logging setup for the serving process (Track B/B1 ops).

Nothing configured logging, so `usali.*` records propagated to an
unconfigured root logger and Python's last-resort handler dropped everything
below WARNING — which silently swallowed the console notifier's OTP line in
the deployed demo (uvicorn's own INFO still appeared because uvicorn installs
its own handlers). `configure_logging` attaches ONE handler to the `usali`
logger at a configurable level so the app's own records reach stdout, where
Cloud Run captures them. Scoped to the `usali` logger — not root — so uvicorn
keeps its handlers and there is no double-logging; the level is driven by
USALI_LOG_LEVEL (config default INFO).
"""

import logging
import sys

# Named so repeated calls (create_app / serve) re-use the one handler
# instead of stacking duplicates.
USALI_HANDLER_NAME = "usali-console"


def configure_logging(level: str = "INFO") -> None:
    """Route `usali.*` logs to stdout at `level`. Idempotent. An unknown
    level name falls back to INFO rather than raising at startup."""
    resolved = logging.getLevelName(level.upper())
    numeric: int = resolved if isinstance(resolved, int) else logging.INFO

    logger = logging.getLogger("usali")
    logger.setLevel(numeric)
    # Re-enable defensively: a dictConfig(disable_existing_loggers=True)
    # elsewhere (uvicorn's own config, a stray init) can flip .disabled on
    # every pre-existing logger, which would silently swallow our records.
    logger.disabled = False
    for existing in logger.handlers:
        if existing.get_name() == USALI_HANDLER_NAME:
            existing.setLevel(numeric)
            return

    handler = logging.StreamHandler(sys.stdout)
    handler.set_name(USALI_HANDLER_NAME)
    handler.setLevel(numeric)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
