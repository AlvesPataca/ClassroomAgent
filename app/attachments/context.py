"""Typed, inert context DTO. No prompts, providers, tools, network or arbitrary file reads."""

from dataclasses import asdict, dataclass
from typing import Any, Literal

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.attachments.safety import AttachmentError, AttachmentStore
from app.config import Settings
from app.errors import AppError
from app.persistence.models import (
    AssignmentRecord,
    AttachmentRecord,
    CourseRecord,
    SubmissionRecord,
)


@dataclass(frozen=True)
class UntrustedText:
    text: str
    trust: Literal["UNTRUSTED_DATA"] = "UNTRUSTED_DATA"


@dataclass(frozen=True)
class ContextAttachment:
    local_id: int
    kind: str
    title: UntrustedText
    status: str
    sections: list[UntrustedText]


@dataclass(frozen=True)
class ContextBundle:
    assignment_local_id: int
    course: UntrustedText
    title: UntrustedText
    description: UntrustedText
    due_at: str | None
    submissions: list[dict[str, Any]]
    attachments: list[ContextAttachment]
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_context(engine: Engine, assignment_id: int, settings: Settings) -> ContextBundle:
    store = AttachmentStore(settings.attachments_root, settings.attachment_max_bytes)
    with Session(engine) as session:
        assignment = session.get(AssignmentRecord, assignment_id)
        if assignment is None or not assignment.present:
            raise AppError("Atividade local indisponível.")
        course = session.get(CourseRecord, assignment.course_id)
        if course is None or not course.present:
            raise AppError("Disciplina local indisponível.")
        attachments = []
        total = 0
        for row in session.scalars(
            select(AttachmentRecord)
            .where(
                AttachmentRecord.assignment_id == assignment_id, AttachmentRecord.present.is_(True)
            )
            .order_by(AttachmentRecord.id)
        ):
            status, sections = row.status, []
            if row.status == "EXTRACTED" and row.extracted:
                try:
                    if (
                        not row.local_path
                        or not row.content_hash
                        or row.extracted.get("content_hash") != row.content_hash
                    ):
                        raise AttachmentError("STALE", "Extração desatualizada.")
                    store.read(row.local_path, course.id, assignment_id, row.content_hash)
                    for part in row.extracted.get("sections", []):
                        text = part.get("text")
                        if not isinstance(text, str):
                            raise AttachmentError("INVALID", "Extração inválida.")
                        total += len(text)
                        if total > settings.extraction_max_chars:
                            raise AppError("Contexto excede EXTRACTION_MAX_CHARS.")
                        sections.append(UntrustedText(text))
                except (AttachmentError, OSError):
                    status, sections = "FAILED", []
            attachments.append(
                ContextAttachment(row.id, row.kind, UntrustedText(row.title), status, sections)
            )
        # Explicit allowlist excludes raw payloads, URLs, paths, OAuth and provider metadata.
        submissions = [
            {k: row.snapshot.get(k) for k in ("state", "assigned_grade", "draft_grade", "late")}
            for row in session.scalars(
                select(SubmissionRecord)
                .where(
                    SubmissionRecord.assignment_id == assignment_id,
                    SubmissionRecord.present.is_(True),
                )
                .order_by(SubmissionRecord.id)
            )
        ]
        return ContextBundle(
            assignment.id,
            UntrustedText(course.snapshot["name"]),
            UntrustedText(assignment.snapshot["title"]),
            UntrustedText(assignment.snapshot.get("description") or ""),
            assignment.snapshot.get("due_at"),
            submissions,
            attachments,
        )
