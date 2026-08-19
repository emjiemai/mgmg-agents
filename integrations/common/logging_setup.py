"""Loguru configuration shared by every agent and client."""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from integrations.common.config import PROJECT_ROOT, settings

_CONFIGURED = False

_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS!UTC}Z</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{extra[agent]}</cyan> | "
    "<cyan>{name}:{function}:{line}</cyan> - <level>{message}</level>"
)


def setup_logging(agent: str = "-") -> "logger.__class__":
    """Configure loguru for stderr plus a rotating file sink.

    Idempotent: repeated calls only rebind the ``agent`` field so each module
    can tag its own lines without duplicating sinks.

    Args:
        agent: Short agent name shown on every line ('ceo-daily-brief', ...).

    Returns:
        A logger bound to ``agent``.
    """
    global _CONFIGURED
    if not _CONFIGURED:
        log_dir = Path(PROJECT_ROOT) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        logger.remove()
        logger.configure(extra={"agent": agent})
        logger.add(
            sys.stderr,
            level=settings.log_level,
            format=_FORMAT,
            backtrace=False,
            diagnose=False,  # never dump local variables — they hold credentials
        )
        logger.add(
            log_dir / "mgmg-{time:YYYY-MM-DD}.log",
            level=settings.log_level,
            format=_FORMAT,
            rotation="00:00",
            retention="30 days",
            compression="gz",
            enqueue=True,
            backtrace=False,
            diagnose=False,
        )
        _CONFIGURED = True

    return logger.bind(agent=agent)
