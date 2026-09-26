"""Persistent, secret-free operational state for the monitoring API."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine, desc, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.persistence.models import (
    AgentStateRecord,
    AssignmentRecord,
    AutomationActivityRecord,
    AutomationCycleRecord,
    CourseRecord,
    GeneratedArtifact,
)

if TYPE_CHECKING:
    from app.automation import CycleResult


def utc_now() -> datetime:
    return datetime.now(UTC)


def _state(session: Session, now: datetime) -> AgentStateRecord:
    state = session.get(AgentStateRecord, 1)
    if state is None:
        state = AgentStateRecord(id=1, mode="unknown", updated_at=now)
        session.add(state)
        session.flush()
    return state


def mark_agent_started(engine: Engine, now: datetime) -> None:
    with Session(engine) as session, session.begin():
        state = _state(session, now)
        state.mode = "continuous"
        state.agent_started_at = now
        state.heartbeat_at = now
        state.updated_at = now


def mark_agent_stopped(engine: Engine, now: datetime) -> None:
    with Session(engine) as session, session.begin():
        state = _state(session, now)
        state.heartbeat_at = now
        state.next_cycle = None
        state.cycle_running = False
        state.cycle_started_at = None
        state.mode = "stopped"
        state.updated_at = now


def schedule_next(engine: Engine, next_cycle: datetime, now: datetime) -> None:
    with Session(engine) as session, session.begin():
        state = _state(session, now)
        state.mode = "continuous"
        state.heartbeat_at = now
        state.next_cycle = next_cycle
        state.updated_at = now


def start_cycle(engine: Engine, mode: str, started_at: datetime) -> int:
    with Session(engine) as session, session.begin():
        session.execute(
            update(AutomationCycleRecord)
            .where(AutomationCycleRecord.status == "RUNNING")
            .values(
                status="FAILED",
                finished_at=started_at,
                errors=1,
                error="ciclo anterior interrompido",
            )
        )
        cycle = AutomationCycleRecord(
            mode=mode,
            status="RUNNING",
            started_at=started_at,
        )
        session.add(cycle)
        state = _state(session, started_at)
        state.cycle_running = True
        state.cycle_started_at = started_at
        state.heartbeat_at = started_at
        if mode == "continuous":
            state.mode = mode
        elif state.mode != "continuous":
            state.mode = mode
        state.updated_at = started_at
        session.flush()
        return cycle.id


def _activity_status(result: CycleResult, assignment_id: int) -> str:
    if assignment_id in result.errors:
        return "error"
    if assignment_id in result.generated:
        return "generated"
    if assignment_id in result.solved:
        return "answered"
    return "reused"


def finish_cycle(
    engine: Engine,
    cycle_id: int,
    finished_at: datetime,
    duration_seconds: float,
    result: CycleResult | None,
    *,
    safe_error: str | None = None,
) -> None:
    with Session(engine) as session, session.begin():
        cycle = session.get(AutomationCycleRecord, cycle_id)
        if cycle is None:
            return
        eligible = len(result.eligible) if result else 0
        answered = len(result.solved) if result else 0
        generated = len(result.generated) if result else 0
        errors = len(result.errors) if result else 1
        cycle.status = (
            "FAILED" if safe_error else "PARTIAL" if result and result.errors else "SUCCESS"
        )
        cycle.finished_at = finished_at
        cycle.eligible = eligible
        cycle.answered = answered
        cycle.generated = generated
        cycle.errors = errors
        cycle.duration_seconds = duration_seconds
        cycle.error = safe_error

        state = _state(session, finished_at)
        state.cycle_running = False
        state.cycle_started_at = None
        state.last_cycle = finished_at
        state.heartbeat_at = finished_at
        state.eligible = eligible
        state.answered = answered
        state.generated = generated
        state.errors = errors
        state.duration_seconds = duration_seconds
        state.updated_at = finished_at

        if result:
            for assignment_id in result.eligible:
                assignment = session.get(AssignmentRecord, assignment_id)
                course = (
                    session.get(CourseRecord, assignment.course_id) if assignment else None
                )
                snapshot = assignment.snapshot if assignment else {}
                pdf_generated = session.scalar(
                    select(GeneratedArtifact.id).where(
                        GeneratedArtifact.assignment_id == assignment_id,
                        GeneratedArtifact.artifact_type == "PDF",
                        GeneratedArtifact.status.in_(("READY", "UPLOAD_PENDING", "UPLOADED")),
                    )
                )
                session.add(
                    AutomationActivityRecord(
                        cycle_id=cycle_id,
                        assignment_id=assignment_id,
                        course=str(
                            (course.snapshot if course else {}).get("name")
                            or "Disciplina desconhecida"
                        )[:180],
                        title=str(snapshot.get("title") or f"Atividade #{assignment_id}")[:180],
                        due_at=(
                            datetime.fromisoformat(snapshot["due_at"])
                            if isinstance(snapshot.get("due_at"), str)
                            else snapshot.get("due_at")
                        ),
                        status=_activity_status(result, assignment_id),
                        pdf_generated=pdf_generated is not None,
                        draft_attached=assignment_id in result.attached,
                        recorded_at=finished_at,
                    )
                )


def status_snapshot(
    engine: Engine, settings: Settings, *, now: datetime | None = None
) -> dict[str, Any]:
    current = now or utc_now()
    with Session(engine) as session:
        state = session.get(AgentStateRecord, 1)
        if state is None:
            return {
                "status": "online",
                "agent_running": False,
                "cycle_running": False,
                "mode": "unknown",
                "interval_hours": settings.automation_interval_hours,
                "last_cycle": None,
                "next_cycle": None,
                "eligible": 0,
                "answered": 0,
                "generated": 0,
                "errors": 0,
                "duration_seconds": None,
            }
        grace = timedelta(minutes=15)
        expected = state.next_cycle or state.heartbeat_at
        agent_running = bool(
            state.mode == "continuous" and expected and current <= expected + grace
        )
        return {
            "status": "online",
            "agent_running": agent_running,
            "cycle_running": state.cycle_running,
            "mode": state.mode,
            "interval_hours": settings.automation_interval_hours,
            "last_cycle": state.last_cycle,
            "next_cycle": state.next_cycle,
            "eligible": state.eligible,
            "answered": state.answered,
            "generated": state.generated,
            "errors": state.errors,
            "duration_seconds": state.duration_seconds,
        }


def cycle_history(engine: Engine, limit: int, offset: int = 0) -> list[dict[str, Any]]:
    with Session(engine) as session:
        rows = session.scalars(
            select(AutomationCycleRecord)
            .where(AutomationCycleRecord.status != "RUNNING")
            .order_by(desc(AutomationCycleRecord.started_at), desc(AutomationCycleRecord.id))
            .offset(offset)
            .limit(limit)
        )
        return [
            {
                "started_at": row.started_at,
                "finished_at": row.finished_at,
                "mode": row.mode,
                "status": row.status.lower(),
                "eligible": row.eligible,
                "answered": row.answered,
                "generated": row.generated,
                "errors": row.errors,
                "duration_seconds": row.duration_seconds,
            }
            for row in rows
        ]


def recent_activities(engine: Engine, limit: int) -> list[dict[str, Any]]:
    with Session(engine) as session:
        rows = session.scalars(
            select(AutomationActivityRecord)
            .order_by(desc(AutomationActivityRecord.recorded_at))
            .limit(limit * 4)
        )
        seen: set[int] = set()
        output: list[dict[str, Any]] = []
        for row in rows:
            if row.assignment_id in seen:
                continue
            seen.add(row.assignment_id)
            output.append(
                {
                    "course": row.course,
                    "title": row.title,
                    "due_at": row.due_at,
                    "status": row.status,
                    "pdf_generated": row.pdf_generated,
                    "draft_attached": row.draft_attached,
                }
            )
            if len(output) == limit:
                break
        return output
