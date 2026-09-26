"""Private monitoring and control API for tailnet access."""

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError, version
from threading import Thread
from typing import Annotated, Any

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api_schemas import ErrorResponse
from app.automation import run_single_cycle
from app.config import Settings, load_settings
from app.cycle_lock import CycleLock, cycle_is_running
from app.management_api import management_router
from app.monitoring import cycle_history, recent_activities, status_snapshot
from app.persistence.database import open_database

api_logger = logging.getLogger("classroom_agent.api")
try:
    APP_VERSION = version("classroom-agent")
except PackageNotFoundError:
    APP_VERSION = "development"


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HealthResponse(ApiModel):
    status: str
    version: str


class StatusResponse(ApiModel):
    status: str
    agent_running: bool
    cycle_running: bool
    mode: str
    interval_hours: int
    last_cycle: str | None = Field(json_schema_extra={"format": "date-time"})
    next_cycle: str | None = Field(json_schema_extra={"format": "date-time"})
    eligible: int
    answered: int
    generated: int
    errors: int
    duration_seconds: float | None
    version: str
    turn_in_enabled: bool = False
    last_cycle_result: str | None


class HistoryItem(ApiModel):
    started_at: str = Field(json_schema_extra={"format": "date-time"})
    finished_at: str | None = Field(json_schema_extra={"format": "date-time"})
    mode: str
    status: str
    eligible: int
    answered: int
    generated: int
    errors: int
    duration_seconds: float | None


class ActivityItem(ApiModel):
    course: str
    title: str
    due_at: str | None = Field(json_schema_extra={"format": "date-time"})
    status: str
    pdf_generated: bool
    draft_attached: bool


class RunAccepted(ApiModel):
    status: str
    detail: str


def _json_safe(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item.isoformat() if hasattr(item, "isoformat") else item for key, item in value.items()
    }


def _manual_cycle(settings: Settings, lock: CycleLock) -> None:
    try:
        api_logger.info("[API] Execução manual iniciada")
        result = run_single_cycle(
            settings,
            mode="manual",
            logger=logging.getLogger("classroom_agent"),
            acquired_lock=lock,
        )
        if result is None:
            api_logger.error("[API] Ciclo manual finalizado com falha")
        else:
            api_logger.info("[API] Execução manual concluída")
    except Exception as exc:
        api_logger.error("[API] Falha ao executar ciclo manual; tipo=%s", type(exc).__name__)
    finally:
        lock.release()


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings or load_settings()
    application = FastAPI(
        title="ClassroomAgent Private API",
        version=APP_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    router = APIRouter(prefix="/api/v1")
    application.router.responses = {code: {"model": ErrorResponse} for code in (404, 409, 422, 500)}

    @application.middleware("http")
    async def request_log(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        api_logger.info(
            "[API] %s %s %s",
            request.method,
            getattr(request.scope.get("route"), "path", "unmatched"),
            response.status_code,
        )
        return response

    @application.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = str(exc.detail)
        code = (
            "cycle_running"
            if exc.status_code == 409
            else (detail if detail.endswith("_not_found") else "http_error")
        )
        return JSONResponse(
            status_code=exc.status_code,
            headers=exc.headers,
            content={"detail": detail, "error": {"code": code, "message": detail}},
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "detail": "Invalid request parameters",
                "error": {"code": "invalid_parameters", "message": "Invalid request parameters"},
            },
        )

    @application.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        api_logger.error(
            "[API] Falha interna; método=%s; rota=%s; tipo=%s",
            request.method,
            getattr(request.scope.get("route"), "path", "unmatched"),
            type(exc).__name__,
        )
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Internal server error",
                "error": {"code": "internal_error", "message": "Internal server error"},
            },
        )

    @router.get("/health", response_model=HealthResponse)
    def health() -> dict[str, str]:
        return {"status": "ok", "version": APP_VERSION}

    @router.get("/status", response_model=StatusResponse)
    def agent_status() -> dict[str, Any]:
        engine = open_database(configured.database_file)
        try:
            payload = status_snapshot(engine, configured)
            payload["cycle_running"] = cycle_is_running(configured.cycle_lock_file)
            if payload["cycle_running"] and payload["mode"] == "continuous":
                payload["agent_running"] = True
            payload["version"] = APP_VERSION
            payload["turn_in_enabled"] = False
            last = cycle_history(engine, 1)
            payload["last_cycle_result"] = last[0]["status"] if last else None
            return _json_safe(payload)
        finally:
            engine.dispose()

    @router.get("/history", response_model=list[HistoryItem])
    def history(
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, Any]]:
        engine = open_database(configured.database_file)
        try:
            return [_json_safe(item) for item in cycle_history(engine, limit, offset)]
        finally:
            engine.dispose()

    @router.get("/activities", response_model=list[ActivityItem])
    def activities(
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> list[dict[str, Any]]:
        engine = open_database(configured.database_file)
        try:
            return [_json_safe(item) for item in recent_activities(engine, limit)]
        finally:
            engine.dispose()

    @router.post("/run", response_model=RunAccepted, status_code=status.HTTP_202_ACCEPTED)
    def request_run() -> dict[str, str]:
        lock = CycleLock(configured.cycle_lock_file)
        if not lock.acquire():
            api_logger.warning("[API] Execução recusada: ciclo já em andamento")
            raise HTTPException(status_code=409, detail="A cycle is already running")
        api_logger.info("[API] Execução manual solicitada")
        try:
            Thread(
                target=_manual_cycle,
                args=(configured, lock),
                name="classroom-agent-manual-cycle",
                daemon=True,
            ).start()
        except Exception:
            lock.release()
            raise

        return {"status": "accepted", "detail": "Manual cycle started"}

    application.include_router(router)
    application.include_router(management_router(configured))
    return application
