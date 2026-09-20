"""Safe console and rotating-file logging for long-running deployments."""

import logging
from logging.handlers import RotatingFileHandler

from app.config import Settings

LOGGER_NAME = "classroom_agent"


def configure_logging(settings: Settings) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logger.setLevel(level)
    logger.propagate = False
    if logger.handlers:
        return logger
    settings.log_file.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s", "%Y-%m-%dT%H:%M:%S%z"
    )
    file_handler = RotatingFileHandler(
        settings.log_file,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


def reset_logging_for_tests() -> None:
    logger = logging.getLogger(LOGGER_NAME)
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)
