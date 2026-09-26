"""Explicit, versioned presentation models; never serialize persistence records."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorBody(DTO):
    code: str
    message: str


class ErrorResponse(DTO):
    error: ErrorBody
    detail: str


class SolutionSummary(DTO):
    id: int
    assignment_id: int
    version: int
    status: str
    template: str | None
    provider: str
    model: str
    created_at: AwareDatetime
    updated_at: AwareDatetime
    has_deliverable: bool
    artifact_count: int


class ReviewQuestion(DTO):
    question_id: str
    answer: str


class ReviewFile(DTO):
    kind: str
    title: str
    specification: str


class SolutionDetail(SolutionSummary):
    review_kind: Literal["deliverable", "question_answer", "code", "unavailable"]
    deliverable: str | None = None
    answer: str | None = None
    question_answers: list[ReviewQuestion] = Field(default_factory=list)
    files: list[ReviewFile] = Field(default_factory=list)


class ArtifactSummary(DTO):
    id: int
    solution_id: int
    solution_version: int
    version: int
    format: str
    name: str
    size_bytes: int | None
    status: str
    created_at: AwareDatetime
    uploaded_at: AwareDatetime | None
    drive_file_id: str | None


class AttachmentSummary(DTO):
    id: int
    kind: str
    title: str
    mime_type: str | None
    status: str
    present: bool


class AssignmentSummary(DTO):
    id: int
    course_id: int
    course_name: str
    title: str
    description_preview: str | None
    due_at: AwareDatetime | None
    submission_state: str | None
    status: str
    late: bool
    eligible: bool
    present: bool
    solution_count: int
    latest_solution_version: int | None
    latest_solution_status: str | None
    artifact_count: int
    first_seen_at: AwareDatetime
    last_synced_at: AwareDatetime


class AssignmentDetail(AssignmentSummary):
    description: str | None
    attachments: list[AttachmentSummary]
    solutions: list[SolutionSummary]
    artifacts: list[ArtifactSummary]


class OperationalSettings(DTO):
    interval_hours: int
    eligibility_window_fraction: float
    timezone: str
    default_document_format: str
    provider: str
    model: str | None
    turn_in_enabled: Literal[False] = False
    google_credentials_configured: bool
    google_token_present: bool
    google_authentication_status: Literal["not_verified"] = "not_verified"
    drive_folder_configured: bool
