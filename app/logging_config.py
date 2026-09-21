"""Central logging configuration for CLI and systemd/journald."""

import logging
import sys
from typing import TextIO

from app.config import Settings

LOGGER_NAME = "classroom_agent"


class _BelowError(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < logging.ERROR


def configure_logging(
    settings: Settings,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> logging.Logger:
    """Send normal events to stdout and errors to stderr without duplication."""
    logger = logging.getLogger(LOGGER_NAME)
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logger.setLevel(level)
    logger.propagate = False
    if logger.handlers:
        return logger
    formatter = logging.Formatter("[%(levelname)s] %(message)s")

    output_handler = logging.StreamHandler(stdout or sys.stdout)
    output_handler.setLevel(logging.DEBUG)
    output_handler.addFilter(_BelowError())
    output_handler.setFormatter(formatter)

    error_handler = logging.StreamHandler(stderr or sys.stderr)
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)

    logger.addHandler(output_handler)
    logger.addHandler(error_handler)
    return logger


def reset_logging_for_tests() -> None:
    logger = logging.getLogger(LOGGER_NAME)
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)
