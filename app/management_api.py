"""Local-only management reads and explicit DTO mapping."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as HttpPath
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, defer, load_only

from app.api_schemas import (
    ArtifactSummary,
    AssignmentDetail,
    AssignmentSummary,
    AttachmentSummary,
    OperationalSettings,
    ReviewFile,
    ReviewQuestion,
    SolutionDetail,
    SolutionSummary,
)
from app.automation import reached_midpoint
from app.config import Settings
from app.domain.models import Assignment, Submission
from app.domain.status import is_overdue, normalize_status
from app.llm.prompt import solution_template
from app.persistence.database import open_database
from app.persistence.models import (
    AssignmentRecord,
    AttachmentRecord,
    CourseRecord,
    GeneratedArtifact,
    SolutionRecord,
    SubmissionRecord,
)

Id = Annotated[int, HttpPath(gt=0)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]
MIMES = {
    "PDF": "application/pdf",
    "DOCX": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "TXT": "text/plain",
    "TEXT": "text/plain",
    "MARKDOWN": "text/markdown",
    "MD": "text/markdown",
    "HTML": "text/html",
    "CSS": "text/css",
    "JAVASCRIPT": "text/javascript",
    "JSON": "application/json",
    "XML": "application/xml",
    "CSV": "text/csv",
    "PYTHON": "text/plain",
    "JAVA": "text/plain",
    "C": "text/plain",
    "HEADER": "text/plain",
    "CPP": "text/plain",
    "SQL": "text/plain",
}


def missing(resource: str) -> HTTPException:
    return HTTPException(404, detail=f"{resource}_not_found")


def artifact_path(row: GeneratedArtifact, settings: Settings) -> Path | None:
    """Resolve only registered regular files inside the generated directory."""
    try:
        root = settings.generated_root.resolve(strict=True)
        path = Path(row.local_path).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            return None
        # Never expose credentials even if the configured output directory is too broad.
        if path in {
            settings.token_file.resolve(),
            settings.credentials_file.resolve(),
            settings.database_file.resolve(),
        } or path.name.startswith("."):
            return None
        return path
    except (OSError, ValueError, RuntimeError):
        return None


def artifact_dto(row: GeneratedArtifact, version: int, settings: Settings) -> ArtifactSummary:
    path = artifact_path(row, settings)
    return ArtifactSummary(
        id=row.id,
        solution_id=row.solution_id,
        solution_version=version,
        version=row.version,
        format=row.artifact_type,
        name=f"artifact-{row.id}" + (path.suffix if path else ""),
        size_bytes=path.stat().st_size if path else None,
        status=row.status,
        created_at=row.created_at,
        uploaded_at=row.uploaded_at,
        drive_file_id=row.drive_file_id,
    )


def review_template(types: Any) -> str | None:
    if not isinstance(types, list) or not types or set(types) <= {"UNKNOWN"}:
        return None
    return solution_template(types)


def solution_dto(row: SolutionRecord, count: int) -> SolutionSummary:
    response = row.response or {}
    types = response.get("assignment_types")
    template = review_template(types)
    return SolutionSummary(
        id=row.id,
        assignment_id=row.assignment_id,
        version=row.version,
        status=row.status,
        template=template,
        provider=row.provider,
        model=row.model,
        created_at=row.created_at,
        updated_at=row.updated_at,
        has_deliverable=bool(response.get("deliverable")),
        artifact_count=count,
    )


def management_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    def session_dependency() -> Iterator[Session]:
        engine = open_database(settings.database_file)
        try:
            with Session(engine) as session:
                yield session
        finally:
            engine.dispose()

    DB = Annotated[Session, Depends(session_dependency)]

    def solutions_for(session: Session, assignment_id: int) -> list[SolutionSummary]:
        count = (
            select(func.count(GeneratedArtifact.id))
            .where(GeneratedArtifact.solution_id == SolutionRecord.id)
            .correlate(SolutionRecord)
            .scalar_subquery()
        )
        rows = session.execute(
            select(
                SolutionRecord.id,
                SolutionRecord.assignment_id,
                SolutionRecord.version,
                SolutionRecord.status,
                SolutionRecord.provider,
                SolutionRecord.model,
                SolutionRecord.created_at,
                SolutionRecord.updated_at,
                SolutionRecord.response["assignment_types"],
                func.length(SolutionRecord.response["deliverable"].as_string()),
                count,
            )
            .where(SolutionRecord.assignment_id == assignment_id)
            .order_by(SolutionRecord.version.desc())
        )
        return [
            SolutionSummary(
                id=id_,
                assignment_id=aid,
                version=version,
                status=status,
                provider=provider,
                model=model,
                created_at=c_at,
                updated_at=u_at,
                template=review_template(types),
                has_deliverable=bool(length),
                artifact_count=total,
            )
            for id_, aid, version, status, provider, model, c_at, u_at, types, length, total in rows
        ]

    def artifacts_for(
        session: Session, *, assignment_id: int | None = None, solution_id: int | None = None
    ) -> list[ArtifactSummary]:
        query = select(GeneratedArtifact, SolutionRecord.version).join(SolutionRecord)
        if assignment_id is not None:
            query = query.where(GeneratedArtifact.assignment_id == assignment_id)
        if solution_id is not None:
            query = query.where(GeneratedArtifact.solution_id == solution_id)
        return [
            artifact_dto(row, version, settings)
            for row, version in session.execute(query.order_by(GeneratedArtifact.version.desc()))
        ]

    def assignment_rows(
        session: Session,
        limit: int,
        offset: int,
        course_id: int | None = None,
        submission_state: str | None = None,
        assignment_id: int | None = None,
    ) -> list[AssignmentSummary]:
        # Match the core: latest submission by ID, then check its presence.
        sub_id = (
            select(func.max(SubmissionRecord.id))
            .where(
                SubmissionRecord.assignment_id == AssignmentRecord.id,
            )
            .correlate(AssignmentRecord)
            .scalar_subquery()
        )
        latest = (
            select(func.max(SolutionRecord.version))
            .where(SolutionRecord.assignment_id == AssignmentRecord.id)
            .correlate(AssignmentRecord)
            .scalar_subquery()
        )
        solution_count = (
            select(func.count(SolutionRecord.id))
            .where(SolutionRecord.assignment_id == AssignmentRecord.id)
            .correlate(AssignmentRecord)
            .scalar_subquery()
        )
        artifact_count = (
            select(func.count(GeneratedArtifact.id))
            .where(GeneratedArtifact.assignment_id == AssignmentRecord.id)
            .correlate(AssignmentRecord)
            .scalar_subquery()
        )
        query = (
            select(
                AssignmentRecord,
                CourseRecord.snapshot,
                SubmissionRecord.snapshot,
                SubmissionRecord.present,
                solution_count,
                SolutionRecord.version,
                SolutionRecord.status,
                artifact_count,
            )
            .join(CourseRecord)
            .outerjoin(SubmissionRecord, SubmissionRecord.id == sub_id)
            .outerjoin(
                SolutionRecord,
                (SolutionRecord.assignment_id == AssignmentRecord.id)
                & (SolutionRecord.version == latest),
            )
            .options(defer(AssignmentRecord.raw_payload))
        )
        if course_id is not None:
            query = query.where(AssignmentRecord.course_id == course_id)
        if assignment_id is not None:
            query = query.where(AssignmentRecord.id == assignment_id)
        if submission_state is not None:
            query = query.where(
                SubmissionRecord.snapshot["state"].as_string() == submission_state,
                SubmissionRecord.present.is_(True),
            )
        rows = session.execute(
            query.order_by(AssignmentRecord.present.desc(), AssignmentRecord.id.desc())
            .limit(limit)
            .offset(offset)
        )
        now = datetime.now(UTC)
        result = []
        for a, course, sub, sub_present, count, version, state, artifacts in rows:
            domain = Assignment.model_validate(a.snapshot)
            submission = Submission.model_validate(sub) if sub and sub_present else None
            result.append(
                AssignmentSummary(
                    id=a.id,
                    course_id=a.course_id,
                    course_name=course.get("name", ""),
                    title=a.snapshot.get("title", ""),
                    description_preview=(a.snapshot.get("description") or "")[:240] or None,
                    due_at=a.snapshot.get("due_at"),
                    submission_state=submission.state if submission else None,
                    status=normalize_status(domain, submission, now).value,
                    late=is_overdue(domain, submission, now),
                    eligible=a.present and reached_midpoint(domain, submission, now),
                    present=a.present,
                    solution_count=count,
                    latest_solution_version=version,
                    latest_solution_status=state,
                    artifact_count=artifacts,
                    first_seen_at=a.first_seen_at,
                    last_synced_at=a.last_synced_at,
                )
            )
        return result

    @router.get("/assignments", response_model=list[AssignmentSummary], tags=["Assignments"])
    def assignments(
        session: DB,
        limit: Limit = 20,
        offset: Offset = 0,
        course_id: Annotated[int | None, Query(gt=0)] = None,
        status: Annotated[
            str | None,
            Query(
                max_length=64,
                description="Submission state, e.g. NEW or TURNED_IN (not normalized status).",
            ),
        ] = None,
    ) -> list[AssignmentSummary]:
        return assignment_rows(session, limit, offset, course_id, status)

    @router.get(
        "/assignments/{assignment_id}", response_model=AssignmentDetail, tags=["Assignments"]
    )
    def assignment(assignment_id: Id, session: DB) -> AssignmentDetail:
        rows = assignment_rows(session, 1, 0, assignment_id=assignment_id)
        if not rows:
            raise missing("assignment")
        row = session.get(AssignmentRecord, assignment_id)
        assert row
        attachments = session.scalars(
            select(AttachmentRecord)
            .options(
                load_only(
                    AttachmentRecord.id,
                    AttachmentRecord.kind,
                    AttachmentRecord.title,
                    AttachmentRecord.mime_type,
                    AttachmentRecord.status,
                    AttachmentRecord.present,
                )
            )
            .where(AttachmentRecord.assignment_id == assignment_id)
            .order_by(AttachmentRecord.id)
        )
        return AssignmentDetail(
            **rows[0].model_dump(),
            description=row.snapshot.get("description"),
            attachments=[
                AttachmentSummary(
                    id=a.id,
                    kind=a.kind,
                    title=a.title,
                    mime_type=a.mime_type,
                    status=a.status,
                    present=a.present,
                )
                for a in attachments
            ],
            solutions=solutions_for(session, assignment_id),
            artifacts=artifacts_for(session, assignment_id=assignment_id),
        )

    @router.get(
        "/assignments/{assignment_id}/solutions",
        response_model=list[SolutionSummary],
        tags=["Solutions"],
    )
    def solutions(assignment_id: Id, session: DB) -> list[SolutionSummary]:
        if session.get(AssignmentRecord, assignment_id) is None:
            raise missing("assignment")
        return solutions_for(session, assignment_id)

    @router.get("/solutions/{solution_id}", response_model=SolutionDetail, tags=["Solutions"])
    def solution(solution_id: Id, session: DB) -> SolutionDetail:
        row = session.get(SolutionRecord, solution_id)
        if row is None:
            raise missing("solution")
        count = (
            session.scalar(
                select(func.count())
                .select_from(GeneratedArtifact)
                .where(GeneratedArtifact.solution_id == solution_id)
            )
            or 0
        )
        summary = solution_dto(row, count)
        result = SolutionDetail(**summary.model_dump(), review_kind="unavailable")
        response = row.response or {}
        if row.status not in {"READY", "NEEDS_REVIEW", "APPROVED"}:
            return result
        if summary.template == "academic-report":
            if summary.has_deliverable:
                result.review_kind = "deliverable"
                result.deliverable = response["deliverable"]
        elif summary.template == "question-answer":
            result.review_kind = "question_answer"
            result.answer = response.get("answer")
            result.question_answers = [
                ReviewQuestion(question_id=q["question_id"], answer=q["answer"])
                for q in response.get("question_answers", [])
            ]
        elif summary.template == "code-assignment":
            result.review_kind = "code"
            result.files = [
                ReviewFile(kind=f["kind"], title=f["title"], specification=f["specification"])
                for f in response.get("artifacts", [])
            ]
        return result

    @router.get(
        "/solutions/{solution_id}/artifacts",
        response_model=list[ArtifactSummary],
        tags=["Artifacts"],
    )
    def artifacts(solution_id: Id, session: DB) -> list[ArtifactSummary]:
        if session.get(SolutionRecord, solution_id) is None:
            raise missing("solution")
        return artifacts_for(session, solution_id=solution_id)

    @router.get("/artifacts/{artifact_id}", response_model=ArtifactSummary, tags=["Artifacts"])
    def artifact(artifact_id: Id, session: DB) -> ArtifactSummary:
        row = session.get(GeneratedArtifact, artifact_id)
        if row is None:
            raise missing("artifact")
        version = session.scalar(
            select(SolutionRecord.version).where(SolutionRecord.id == row.solution_id)
        )
        return artifact_dto(row, version or 0, settings)

    @router.get(
        "/artifacts/{artifact_id}/content",
        response_class=FileResponse,
        tags=["Artifacts"],
        responses={
            200: {"content": {mime: {} for mime in MIMES.values()}},
            206: {"description": "Partial content"},
            416: {"description": "Range not satisfiable"},
        },
    )
    def content(artifact_id: Id, session: DB) -> FileResponse:
        row = session.get(GeneratedArtifact, artifact_id)
        if row is None:
            raise missing("artifact")
        path = artifact_path(row, settings)
        if path is None or row.status not in {"READY", "UPLOAD_PENDING", "UPLOADED"}:
            raise missing("artifact_content")
        return FileResponse(
            path,
            media_type=MIMES.get(row.artifact_type, "application/octet-stream"),
            filename=f"artifact-{row.id}{path.suffix}",
            content_disposition_type="inline" if row.artifact_type == "PDF" else "attachment",
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
        )

    @router.get("/settings", response_model=OperationalSettings, tags=["Settings"])
    def operational_settings() -> OperationalSettings:
        return OperationalSettings(
            interval_hours=settings.automation_interval_hours,
            eligibility_window_fraction=0.5,
            timezone=settings.timezone,
            default_document_format="pdf",
            provider=settings.llm_provider,
            model={"gemini": settings.gemini_model, "astra": settings.astra_model}.get(
                settings.llm_provider
            ),
            google_credentials_configured=settings.credentials_file.is_file(),
            google_token_present=settings.token_file.is_file(),
            drive_folder_configured=bool(settings.drive_responses_folder_id),
        )

    return router
