from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TaskType = Literal[
    "MULTIPLE_CHOICE",
    "SHORT_ANSWER",
    "LONG_FORM",
    "PROGRAMMING",
    "RESEARCH",
    "CALCULATION",
    "DOCUMENT",
    "PRESENTATION",
    "SPREADSHEET",
    "UNKNOWN",
]
SourceType = Literal["DESCRIPTION_TASK", "FORM_TASK", "MATERIAL_TASK"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class SourceText(StrictModel):
    text: str
    source: str
    trust: Literal["UNTRUSTED_DATA"] = "UNTRUSTED_DATA"


class Question(StrictModel):
    id: str
    title: SourceText
    task_type: TaskType = "UNKNOWN"
    required: bool = False
    options: list[SourceText] = Field(default_factory=list)


class FormContext(StrictModel):
    url: str | None
    form_id: str | None = None
    title: SourceText
    description: SourceText | None = None
    status: Literal[
        "QUESTIONS_AVAILABLE", "FORM_DETECTED_BUT_QUESTIONS_UNAVAILABLE", "PARTIAL_QUESTIONS"
    ] = "FORM_DETECTED_BUT_QUESTIONS_UNAVAILABLE"
    questions: list[Question] = Field(default_factory=list)
    reason: str = "Perguntas indisponíveis; forneça o enunciado manualmente no futuro."


class LinkContext(StrictModel):
    url: str
    source: str


class AttachmentContext(StrictModel):
    local_id: int
    kind: str
    title: SourceText
    status: str
    content: SourceText | None = None


class RelatedMaterialContext(StrictModel):
    local_id: int
    kind: str
    title: SourceText
    description: SourceText
    topic: SourceText | None = None
    score: int = Field(ge=0, le=100)
    reasons: list[str] = Field(default_factory=list)


class SubmissionContext(StrictModel):
    state: str
    assigned_grade: float | None = None
    draft_grade: float | None = None
    late: bool | None = None


class AssignmentContext(StrictModel):
    schema_version: int = 5
    assignment_local_id: int
    course_local_id: int
    course: SourceText
    assignment: SourceText
    description: SourceText
    due_at: str | None
    state: str
    max_points: float | None
    submissions: list[SubmissionContext]
    source_types: list[SourceType]
    assignment_types: list[TaskType]
    classification_evidence: list[str]
    links: list[LinkContext]
    forms: list[FormContext]
    questions: list[Question]
    attachments: list[AttachmentContext]
    related_materials: list[RelatedMaterialContext] = Field(default_factory=list)
    ready_for_ai: bool
    missing_context: list[str]
    readiness_evidence: list[str]
    provenance: dict[str, str]
    source_hashes: dict[str, str]
    warnings: list[str]
