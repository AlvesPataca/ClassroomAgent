from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.attachments.drive import DriveClient
from app.attachments.service import AttachmentService
from app.auth.google import authenticate
from app.classroom.client import ClassroomClient
from app.config import Settings
from app.context.builder import build_assignment_context, context_hash
from app.documents.drive import DriveUploader
from app.documents.engine import DocumentBuilder
from app.domain.models import Assignment, Submission
from app.domain.status import normalize_status
from app.errors import AppError
from app.llm.providers import LLMProvider, get_provider
from app.llm.service import solve_context
from app.persistence.database import open_database
from app.persistence.models import (
    AssignmentRecord,
    GeneratedArtifact,
    SolutionRecord,
    SubmissionRecord,
)
from app.sync import synchronize

Log = Callable[[str], None]


@dataclass
class CycleResult:
    sync_status: str
    eligible: list[int] = field(default_factory=list)
    solved: list[int] = field(default_factory=list)
    generated: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)
    errors: dict[int, str] = field(default_factory=dict)


def reached_midpoint(
    assignment: Assignment, submission: Submission | None, now: datetime
) -> bool:
    """Return true only during the actionable second half of a defined deadline."""
    if now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if (
        assignment.state != "PUBLISHED"
        or assignment.creation_time is None
        or assignment.due_at is None
        or assignment.due_at <= assignment.creation_time
        or now >= assignment.due_at
    ):
        return False
    midpoint = assignment.creation_time + (assignment.due_at - assignment.creation_time) / 2
    return now >= midpoint and normalize_status(assignment, submission, now).value == "PENDING"


def eligible_assignment_ids(engine: Engine, now: datetime) -> list[int]:
    with Session(engine) as session:
        submissions = {
            row.assignment_id: Submission.model_validate(row.snapshot)
            for row in session.scalars(
                select(SubmissionRecord)
                .where(SubmissionRecord.present.is_(True))
                .order_by(SubmissionRecord.id)
            )
        }
        return [
            row.id
            for row in session.scalars(
                select(AssignmentRecord)
                .where(AssignmentRecord.present.is_(True))
                .order_by(AssignmentRecord.id)
            )
            if reached_midpoint(
                Assignment.model_validate(row.snapshot), submissions.get(row.id), now
            )
        ]


def _completed_solution(engine: Engine, assignment_id: int, digest: str) -> SolutionRecord | None:
    with Session(engine) as session:
        return session.scalars(
            select(SolutionRecord)
            .where(
                SolutionRecord.assignment_id == assignment_id,
                SolutionRecord.context_hash == digest,
                SolutionRecord.status.in_(("READY", "NEEDS_REVIEW", "APPROVED")),
            )
            .order_by(SolutionRecord.version.desc())
        ).first()


def _already_generated(engine: Engine, solution_id: int) -> bool:
    with Session(engine) as session:
        return (
            session.scalars(
                select(GeneratedArtifact).where(
                    GeneratedArtifact.solution_id == solution_id,
                    GeneratedArtifact.status.in_(("READY", "UPLOAD_PENDING", "UPLOADED")),
                )
            ).first()
            is not None
        )


def _solution_artifacts(engine: Engine, solution_id: int) -> list[GeneratedArtifact]:
    with Session(engine) as session:
        return list(
            session.scalars(
                select(GeneratedArtifact)
                .where(GeneratedArtifact.solution_id == solution_id)
                .order_by(GeneratedArtifact.version)
            )
        )


def attach_solution(
    engine: Engine,
    assignment_id: int,
    solution_id: int,
    classroom: ClassroomClient,
    drive: DriveClient,
    settings: Settings,
) -> int:
    """Upload generated artifacts to Drive and attach them to the draft submission."""
    with Session(engine) as session:
        assignment = session.get(AssignmentRecord, assignment_id)
        submission = session.scalar(
            select(SubmissionRecord).where(
                SubmissionRecord.assignment_id == assignment_id,
                SubmissionRecord.present.is_(True),
            )
        )
        course = session.get(
            __import__("app.persistence.models", fromlist=["CourseRecord"]).CourseRecord,
            assignment.course_id if assignment else -1,
        )
        if assignment is None or submission is None or course is None:
            raise AppError("Submissão de rascunho não encontrada para anexar os arquivos.")
        course_id, coursework_id, submission_id = (
            course.google_id,
            assignment.google_id,
            submission.google_id,
        )
    artifacts = _solution_artifacts(engine, solution_id)
    uploader = DriveUploader(engine, settings, drive.service)
    drive_ids = [
        value
        for artifact in artifacts
        if (value := uploader.upload(artifact.id).drive_file_id)
    ]
    existing = classroom.submission_material_ids(course_id, coursework_id, submission_id)
    pending = [value for value in drive_ids if value and value not in existing]
    classroom.attach_drive_files(course_id, coursework_id, submission_id, pending)
    return len(pending)


def run_cycle(
    engine: Engine,
    settings: Settings,
    classroom: ClassroomClient,
    drive: DriveClient,
    *,
    provider: LLMProvider | None = None,
    now: datetime | None = None,
    log: Log = lambda _: None,
) -> CycleResult:
    current = now or datetime.now(UTC)
    sync_result = synchronize(engine, lambda: classroom)
    result = CycleResult(sync_status=sync_result.status)
    if sync_result.status != "SUCCESS":
        raise AppError("Ciclo interrompido: sincronização não concluída com sucesso.")
    result.eligible = eligible_assignment_ids(engine, current)
    attachments = AttachmentService(engine, settings)
    selected_provider = provider or get_provider(settings)
    for assignment_id in result.eligible:
        try:
            log(f"Atividade #{assignment_id}: preparando anexos e contexto.")
            attachments.fetch(assignment_id, drive)
            attachments.extract(assignment_id)
            context = build_assignment_context(engine, assignment_id, settings, read_forms=True)
            digest = context_hash(context)
            solution = _completed_solution(engine, assignment_id, digest)
            if solution is None:
                solution = solve_context(engine, context, selected_provider)
                result.solved.append(assignment_id)
                log(f"Atividade #{assignment_id}: solução #{solution.id} gerada.")
            if _already_generated(engine, solution.id):
                result.skipped.append(assignment_id)
                log(f"Atividade #{assignment_id}: arquivos atuais já existem.")
            else:
                artifacts = DocumentBuilder(engine, settings).build_all(
                    assignment_id, solution.version
                )
                result.generated.append(assignment_id)
                log(f"Atividade #{assignment_id}: {len(artifacts)} arquivo(s) gerado(s).")
            attached = attach_solution(
                engine, assignment_id, solution.id, classroom, drive, settings
            )
            log(f"Atividade #{assignment_id}: {attached} anexo(s) adicionado(s) ao rascunho.")
        except (AppError, OSError, ValueError) as exc:
            result.errors[assignment_id] = str(exc)
            log(f"Atividade #{assignment_id}: falhou; será repetida no próximo ciclo: {exc}")
    return result


def run_forever(
    settings: Settings,
    *,
    once: bool = False,
    log: Log = lambda _: None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    wait: Callable[[float], Any] = time.sleep,
) -> None:
    interval = timedelta(hours=settings.automation_interval_hours)
    while True:
        started = clock()
        drive: DriveClient | None = None
        engine: Engine | None = None
        try:
            credentials = authenticate(settings, require_write=True)
            classroom = ClassroomClient.from_credentials(credentials, settings)
            drive = DriveClient.from_credentials(credentials, settings)
            engine = open_database(settings.database_file)
            result = run_cycle(engine, settings, classroom, drive, now=started, log=log)
            log(
                f"Ciclo concluído: {len(result.eligible)} elegível(is), "
                f"{len(result.solved)} respondida(s), {len(result.generated)} gerada(s), "
                f"{len(result.errors)} erro(s)."
            )
        except AppError as exc:
            log(f"Ciclo falhou: {exc}. Nova tentativa no próximo horário.")
        except (OSError, ValueError):
            log("Ciclo falhou por erro local seguro. Nova tentativa no próximo horário.")
        finally:
            if drive is not None:
                drive.close()
            if engine is not None:
                engine.dispose()
        if once:
            return
        next_run = started + interval
        seconds = max(1.0, (next_run - clock()).total_seconds())
        log(f"Próxima verificação: {next_run.astimezone(settings.zone):%d/%m/%Y %H:%M:%S}.")
        wait(seconds)
