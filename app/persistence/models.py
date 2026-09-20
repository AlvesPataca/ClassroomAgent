from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class AwareTimestamp(TypeDecorator[datetime]):
    """SQLite has no timezone type; store UTC ISO text and restore aware values."""

    impl = String
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> str | None:
        if value is None:
            return None
        if value.utcoffset() is None:
            raise ValueError("Timestamp must be timezone-aware")
        return value.astimezone(UTC).isoformat()

    def process_result_value(self, value: str | None, dialect: Any) -> datetime | None:
        return datetime.fromisoformat(value) if value else None


class Base(DeclarativeBase):
    pass


class Record:
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    google_id: Mapped[str] = mapped_column(String, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    last_synced_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    creation_time: Mapped[datetime | None] = mapped_column(AwareTimestamp())
    update_time: Mapped[datetime | None] = mapped_column(AwareTimestamp())
    present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class CourseRecord(Record, Base):
    __tablename__ = "courses"
    __table_args__ = (UniqueConstraint("google_id"),)


class AssignmentRecord(Record, Base):
    __tablename__ = "assignments"
    __table_args__ = (UniqueConstraint("course_id", "google_id"),)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)


class SubmissionRecord(Record, Base):
    __tablename__ = "submissions"
    __table_args__ = (UniqueConstraint("assignment_id", "google_id"),)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"), index=True)


class CourseResourceRecord(Base):
    __tablename__ = "course_resources"
    __table_args__ = (UniqueConstraint("course_id", "kind", "google_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    google_id: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String)
    topic_id: Mapped[str | None] = mapped_column(String, index=True)
    topic_name: Mapped[str | None] = mapped_column(String)
    materials: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    alternate_link: Mapped[str | None] = mapped_column(String)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    creation_time: Mapped[datetime | None] = mapped_column(AwareTimestamp())
    update_time: Mapped[datetime | None] = mapped_column(AwareTimestamp())
    first_seen_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class AssignmentResourceLink(Base):
    __tablename__ = "assignment_resource_links"
    __table_args__ = (UniqueConstraint("assignment_id", "resource_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"), index=True)
    resource_id: Mapped[int] = mapped_column(ForeignKey("course_resources.id"), index=True)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)


class SyncRun(Base):
    __tablename__ = "sync_runs"
    __table_args__ = (CheckConstraint("status IN ('RUNNING', 'SUCCESS', 'PARTIAL', 'FAILED')"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(AwareTimestamp())
    status: Mapped[str] = mapped_column(String, nullable=False, index=True)
    counters: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(String)


class AttachmentRecord(Base):
    __tablename__ = "attachments"
    __table_args__ = (
        UniqueConstraint("assignment_id", "identity_key"),
        CheckConstraint("kind IN ('DRIVE_FILE', 'LINK', 'YOUTUBE', 'FORM', 'UNKNOWN')"),
        CheckConstraint(
            "status IN ('DISCOVERED', 'DOWNLOADED', 'EXTRACTED', 'UNSUPPORTED', 'FAILED')"
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"), index=True)
    identity_key: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    drive_id: Mapped[str | None] = mapped_column(String)
    url: Mapped[str | None] = mapped_column(String)
    mime_type: Mapped[str | None] = mapped_column(String)
    materialized_mime: Mapped[str | None] = mapped_column(String)
    local_path: Mapped[str | None] = mapped_column(String)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    materialized_fingerprint: Mapped[str | None] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    drive_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String, default="DISCOVERED")
    error_code: Mapped[str | None] = mapped_column(String)
    error: Mapped[str | None] = mapped_column(String)
    extracted: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    history: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    present: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    removed_at: Mapped[datetime | None] = mapped_column(AwareTimestamp())
    fetched_at: Mapped[datetime | None] = mapped_column(AwareTimestamp())
    extracted_at: Mapped[datetime | None] = mapped_column(AwareTimestamp())


class SolutionRecord(Base):
    __tablename__ = "solutions"
    __table_args__ = (
        UniqueConstraint("assignment_id", "version"),
        CheckConstraint("version > 0"),
        CheckConstraint("status IN ('GENERATING', 'READY', 'NEEDS_REVIEW', 'FAILED', 'APPROVED')"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    context_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    answer: Mapped[str | None] = mapped_column(String)
    error: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)


class GeneratedArtifact(Base):
    __tablename__ = "generated_artifacts"
    __table_args__ = (
        UniqueConstraint("assignment_id", "version"),
        CheckConstraint("status IN ('GENERATING','READY','UPLOAD_PENDING','UPLOADED','FAILED')"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    solution_id: Mapped[int] = mapped_column(ForeignKey("solutions.id"), index=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"), index=True)
    artifact_type: Mapped[str] = mapped_column(String, default="PDF")
    template: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    local_path: Mapped[str] = mapped_column(String, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    drive_file_id: Mapped[str | None] = mapped_column(String)
    drive_folder_id: Mapped[str | None] = mapped_column(String)
    drive_web_view_link: Mapped[str | None] = mapped_column(String)
    error: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(AwareTimestamp(), nullable=False)
    uploaded_at: Mapped[datetime | None] = mapped_column(AwareTimestamp())
