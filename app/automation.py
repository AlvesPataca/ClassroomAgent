from __future__ import annotations

import logging
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
from app.cycle_lock import CycleLock
from app.documents.drive import DriveUploader
from app.documents.engine import DocumentBuilder
from app.domain.models import Assignment, Submission
from app.domain.status import normalize_status
from app.errors import AppError
from app.llm.providers import LLMProvider, get_provider
from app.llm.service import solve_context
from app.monitoring import (
    finish_cycle,
    mark_agent_started,
    mark_agent_stopped,
    schedule_next,
    start_cycle,
)
from app.persistence.database import open_database
from app.persistence.models import (
    AssignmentRecord,
    CourseRecord,
    GeneratedArtifact,
    SolutionRecord,
    SubmissionRecord,
)
from app.sync import synchronize


@dataclass
class CycleResult:
    sync_status: str
    eligible: list[int] = field(default_factory=list)
    solved: list[int] = field(default_factory=list)
    generated: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)
    attached: list[int] = field(default_factory=list)
    errors: dict[int, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AssignmentLabel:
    course: str
    title: str
    due_at: datetime | None


@dataclass(frozen=True)
class DeliveryResult:
    artifacts: int
    uploaded: int
    attached: int


def _log_text(value: object, limit: int = 180) -> str:
    """Keep external titles and safe errors on one compact log line."""
    return " ".join(str(value).split())[:limit]


def _constant_clock(value: datetime) -> Callable[[], datetime]:
    def read() -> datetime:
        return value

    return read


def _assignment_label(engine: Engine, assignment_id: int) -> AssignmentLabel:
    with Session(engine) as session:
        assignment = session.get(AssignmentRecord, assignment_id)
        course = session.get(CourseRecord, assignment.course_id) if assignment else None
        if assignment is None:
            return AssignmentLabel("Disciplina desconhecida", f"Atividade #{assignment_id}", None)
        snapshot = assignment.snapshot
        return AssignmentLabel(
            _log_text((course.snapshot if course else {}).get("name") or "Disciplina desconhecida"),
            _log_text(snapshot.get("title") or f"Atividade #{assignment_id}"),
            Assignment.model_validate(snapshot).due_at,
        )


def reached_midpoint(assignment: Assignment, submission: Submission | None, now: datetime) -> bool:
    """Return true only during the actionable second half of a defined deadline."""
    return _eligibility_rejection_reason(assignment, submission, now) is None


def _eligibility_rejection_reason(
    assignment: Assignment, submission: Submission | None, now: datetime
) -> str | None:
    if now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if assignment.state != "PUBLISHED":
        return "coursework_state"
    if assignment.creation_time is None:
        return "missing_creation_time"
    if assignment.due_at is None:
        return "missing_deadline"
    if assignment.due_at <= assignment.creation_time:
        return "invalid_deadline_window"
    if now >= assignment.due_at:
        return "deadline_passed"
    if normalize_status(assignment, submission, now).value != "PENDING":
        return "submission_state"
    midpoint = assignment.creation_time + (assignment.due_at - assignment.creation_time) / 2
    if now < midpoint:
        return "deadline_window"
    return None


def eligible_assignment_ids(
    engine: Engine, now: datetime, *, logger: logging.Logger | None = None
) -> list[int]:
    active_logger = logger or logging.getLogger("classroom_agent")
    with Session(engine) as session:
        submissions = {
            row.assignment_id: row
            for row in session.scalars(select(SubmissionRecord).order_by(SubmissionRecord.id))
        }
        eligible: list[int] = []
        for row in session.scalars(select(AssignmentRecord).order_by(AssignmentRecord.id)):
            if not row.present:
                active_logger.debug(
                    "[DEBUG] Atividade local=%s rejeitada; motivo=assignment_not_present",
                    row.id,
                )
                continue
            assignment = Assignment.model_validate(row.snapshot)
            submission_row = submissions.get(row.id)
            submission = (
                Submission.model_validate(submission_row.snapshot)
                if submission_row is not None and submission_row.present
                else None
            )
            reason = _eligibility_rejection_reason(assignment, submission, now)
            if reason is not None:
                active_logger.debug(
                    "[DEBUG] Atividade local=%s rejeitada; motivo=%s", row.id, reason
                )
                continue
            eligible.append(row.id)
        return eligible


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
) -> DeliveryResult:
    """Upload generated artifacts to Drive and attach them to the draft submission."""
    with Session(engine) as session:
        assignment = session.get(AssignmentRecord, assignment_id)
        submission = session.scalar(
            select(SubmissionRecord).where(
                SubmissionRecord.assignment_id == assignment_id,
                SubmissionRecord.present.is_(True),
            )
        )
        course = session.get(CourseRecord, assignment.course_id if assignment else -1)
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
        value for artifact in artifacts if (value := uploader.upload(artifact.id).drive_file_id)
    ]
    existing = classroom.submission_material_ids(course_id, coursework_id, submission_id)
    pending = [value for value in drive_ids if value and value not in existing]
    classroom.attach_drive_files(course_id, coursework_id, submission_id, pending)
    return DeliveryResult(len(artifacts), len(drive_ids), len(pending))


def run_cycle(
    engine: Engine,
    settings: Settings,
    classroom: ClassroomClient,
    drive: DriveClient,
    *,
    provider: LLMProvider | None = None,
    now: datetime | None = None,
    logger: logging.Logger | None = None,
) -> CycleResult:
    active_logger = logger or logging.getLogger("classroom_agent")
    current = now or datetime.now(UTC)
    active_logger.info("Sincronizando disciplinas e atividades")
    sync_result = synchronize(engine, lambda: classroom)
    result = CycleResult(sync_status=sync_result.status)
    if sync_result.status != "SUCCESS":
        active_logger.error(
            "Sincronização não concluída; status=%s; motivo=%s",
            sync_result.status,
            sync_result.error or "não informado",
        )
        raise AppError("Ciclo interrompido: sincronização não concluída com sucesso.")
    counters = sync_result.counters
    active_logger.info(
        "[OK] Sincronização concluída; disciplinas=%s; atividades=%s; submissões=%s",
        counters["courses"]["found"],
        counters["assignments"]["found"],
        counters["submissions"]["found"],
    )
    active_logger.info("Procurando atividades elegíveis")
    result.eligible = eligible_assignment_ids(engine, current, logger=active_logger)
    if not result.eligible:
        active_logger.info("Nenhuma atividade elegível encontrada")
    else:
        active_logger.info("%s atividade(s) elegível(is)", len(result.eligible))
    attachments = AttachmentService(engine, settings)
    selected_provider = provider or get_provider(settings)
    for assignment_id in result.eligible:
        label = _assignment_label(engine, assignment_id)
        deadline = (
            label.due_at.astimezone(settings.zone).strftime("%d/%m/%Y %H:%M")
            if label.due_at
            else "não informado"
        )
        active_logger.info(
            "[ATIVIDADE] curso=%s; título=%s; prazo=%s; id_local=%s",
            label.course,
            label.title,
            deadline,
            assignment_id,
        )
        stage = "preparar a atividade"
        try:
            stage = "baixar anexos"
            fetched = attachments.fetch(assignment_id, drive)
            stage = "processar anexos"
            extracted = attachments.extract(assignment_id)
            extracted_count = sum(row["status"] == "EXTRACTED" for row in extracted)
            failed_count = sum(row["status"] == "FAILED" for row in extracted)
            active_logger.info(
                "[OK] Anexos processados; encontrados=%s; extraídos=%s; falhas=%s",
                len(fetched),
                extracted_count,
                failed_count,
            )
            stage = "extrair contexto"
            context = build_assignment_context(engine, assignment_id, settings, read_forms=True)
            active_logger.info(
                "[OK] Contexto extraído; fontes=%s; pronto_para_ia=%s",
                len(context.provenance),
                context.ready_for_ai,
            )
            digest = context_hash(context)
            solution = _completed_solution(engine, assignment_id, digest)
            if solution is None:
                stage = "gerar resposta"
                solution = solve_context(engine, context, selected_provider)
                result.solved.append(assignment_id)
                active_logger.info(
                    "[OK] Resposta gerada; solução=%s; provider=%s; modelo=%s",
                    solution.id,
                    solution.provider,
                    solution.model,
                )
            else:
                active_logger.info("Resposta existente reutilizada; solução=%s", solution.id)
            if _already_generated(engine, solution.id):
                result.skipped.append(assignment_id)
                active_logger.info("Arquivos existentes reutilizados")
            else:
                stage = "gerar arquivos"
                artifacts = DocumentBuilder(engine, settings).build_all(
                    assignment_id, solution.version
                )
                result.generated.append(assignment_id)
                types = ",".join(artifact.artifact_type for artifact in artifacts)
                active_logger.info(
                    "[OK] Arquivos gerados; quantidade=%s; tipos=%s", len(artifacts), types
                )
            stage = "enviar arquivos ao Google Drive"
            delivery = attach_solution(
                engine, assignment_id, solution.id, classroom, drive, settings
            )
            active_logger.info(
                "[OK] Upload no Google Drive confirmado; arquivos=%s", delivery.uploaded
            )
            active_logger.info(
                "[OK] Rascunho atualizado no Classroom; novos_anexos=%s; turn_in=false",
                delivery.attached,
            )
            result.attached.append(assignment_id)
        except (AppError, OSError, ValueError) as exc:
            result.errors[assignment_id] = str(exc)
            reason = (
                _log_text(exc) if isinstance(exc, AppError) else "erro local durante a operação"
            )
            active_logger.error(
                "Falha ao %s; atividade=%s; curso=%s; motivo=%s; nova_tentativa=próximo_ciclo",
                stage,
                label.title,
                label.course,
                reason,
            )
    return result


class CycleAlreadyRunning(AppError):
    pass


def run_single_cycle(
    settings: Settings,
    *,
    mode: str,
    logger: logging.Logger | None = None,
    acquired_lock: CycleLock | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    timer: Callable[[], float] = time.monotonic,
) -> CycleResult | None:
    active_logger = logger or logging.getLogger("classroom_agent")
    lock = acquired_lock or CycleLock(settings.cycle_lock_file)
    # The API reserves this same lock before returning 202 so it can answer 409
    # atomically. acquire() is idempotent for that already-held instance, which
    # keeps lock validation and ownership in the shared executor for every mode.
    if not lock.acquire():
        raise CycleAlreadyRunning("A cycle is already running")
    started = clock()
    started_monotonic = timer()
    active_logger.info("Iniciando ciclo automático; origem=%s", mode)
    drive: DriveClient | None = None
    engine: Engine | None = None
    try:
        engine = open_database(settings.database_file)
        cycle_id = start_cycle(engine, mode, started)
    except Exception:
        if engine is not None:
            engine.dispose()
        lock.release()
        raise
    result: CycleResult | None = None
    try:
        credentials = authenticate(settings, require_write=True)
        active_logger.info("[OK] Autenticação Google carregada")
        classroom = ClassroomClient.from_credentials(credentials, settings)
        drive = DriveClient.from_credentials(credentials, settings)
        active_logger.info("[OK] Clientes Google Classroom e Drive preparados")
        result = run_cycle(engine, settings, classroom, drive, now=started, logger=active_logger)
        duration = timer() - started_monotonic
        finished = started + timedelta(seconds=duration)
        finish_cycle(engine, cycle_id, finished, duration, result)
        active_logger.info(
            "[RESUMO] elegíveis=%s; respondidas=%s; geradas=%s; erros=%s; duração=%.1fs",
            len(result.eligible),
            len(result.solved),
            len(result.generated),
            len(result.errors),
            duration,
        )
        return result
    except AppError as exc:
        duration = timer() - started_monotonic
        finish_cycle(
            engine,
            cycle_id,
            started + timedelta(seconds=duration),
            duration,
            result,
            safe_error=_log_text(exc),
        )
        active_logger.error(
            "Ciclo automático falhou; motivo=%s; duração=%.1fs; nova_tentativa=próximo_horário",
            _log_text(exc),
            duration,
        )
        return None
    except (OSError, ValueError):
        duration = timer() - started_monotonic
        finish_cycle(
            engine,
            cycle_id,
            started + timedelta(seconds=duration),
            duration,
            result,
            safe_error="erro local durante a operação",
        )
        active_logger.error(
            "Ciclo automático falhou por erro local; duração=%.1fs; nova_tentativa=próximo_horário",
            duration,
        )
        return None
    except Exception as exc:
        duration = timer() - started_monotonic
        finish_cycle(
            engine,
            cycle_id,
            started + timedelta(seconds=duration),
            duration,
            result,
            safe_error=f"falha técnica: {type(exc).__name__}",
        )
        active_logger.error(
            "Falha técnica inesperada no ciclo; tipo=%s; duração=%.1fs",
            type(exc).__name__,
            duration,
        )
        raise
    finally:
        if drive is not None:
            drive.close()
        engine.dispose()
        lock.release()


def run_forever(
    settings: Settings,
    *,
    once: bool = False,
    logger: logging.Logger | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    timer: Callable[[], float] = time.monotonic,
    wait: Callable[[float], Any] = time.sleep,
) -> None:
    active_logger = logger or logging.getLogger("classroom_agent")
    interval = timedelta(hours=settings.automation_interval_hours)
    state_engine = open_database(settings.database_file)
    if not once:
        mark_agent_started(state_engine, clock())
    try:
        while True:
            started = clock()
            try:
                run_single_cycle(
                    settings,
                    mode="once" if once else "continuous",
                    logger=active_logger,
                    clock=_constant_clock(started),
                    timer=timer,
                )
            except CycleAlreadyRunning:
                active_logger.warning("Ciclo ignorado: outra execução já está em andamento")
            if once:
                return
            next_run = started + interval
            current = clock()
            schedule_next(state_engine, next_run, current)
            seconds = max(1.0, (next_run - current).total_seconds())
            active_logger.info(
                "Próxima verificação: %s",
                next_run.astimezone(settings.zone).strftime("%d/%m/%Y %H:%M:%S"),
            )
            wait(seconds)
    finally:
        if not once:
            mark_agent_stopped(state_engine, clock())
        state_engine.dispose()
