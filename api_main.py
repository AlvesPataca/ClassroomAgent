"""Entrypoint for the private ClassroomAgent monitoring API."""

import logging

import uvicorn

from app.api import create_app
from app.config import load_settings
from app.logging_config import configure_logging


def main() -> None:
    settings = load_settings()
    configure_logging(settings)
    logging.getLogger("classroom_agent.api").info(
        "[API] API iniciada; host=%s; porta=%s", settings.api_host, settings.api_port
    )
    uvicorn.run(
        create_app(settings),
        host=settings.api_host,
        port=settings.api_port,
        workers=1,
        access_log=False,
        log_config=None,
    )


if __name__ == "__main__":
    main()
