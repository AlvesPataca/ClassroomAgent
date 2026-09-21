import io
import logging

from app.config import Settings
from app.logging_config import configure_logging, reset_logging_for_tests


def test_logging_routes_normal_messages_and_errors_without_duplication():
    stdout = io.StringIO()
    stderr = io.StringIO()
    reset_logging_for_tests()
    logger = configure_logging(Settings(), stdout=stdout, stderr=stderr)

    logger.info("ciclo iniciado")
    logger.error("falha segura")

    assert "[INFO] ciclo iniciado" in stdout.getvalue()
    assert "falha segura" not in stdout.getvalue()
    assert stderr.getvalue().count("[ERROR] falha segura") == 1
    reset_logging_for_tests()


def test_logging_respects_level_and_does_not_format_unused_secret_arguments():
    stdout = io.StringIO()
    stderr = io.StringIO()
    reset_logging_for_tests()
    logger = configure_logging(
        Settings(log_level="ERROR"), stdout=stdout, stderr=stderr
    )

    logger.info("segredo=%s", "GEMINI_SECRET")
    logger.error("erro operacional")

    assert "GEMINI_SECRET" not in stdout.getvalue() + stderr.getvalue()
    assert stderr.getvalue() == "[ERROR] erro operacional\n"
    reset_logging_for_tests()


def test_reset_logging_removes_all_handlers():
    reset_logging_for_tests()
    logger = logging.getLogger("classroom_agent")
    logger.addHandler(logging.NullHandler())
    reset_logging_for_tests()
    assert logger.handlers == []
