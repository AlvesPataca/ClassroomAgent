from datetime import date, time
from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Course(DomainModel):
    raw_payload: dict[str, Any] = Field(default_factory=dict, repr=False)
    google_id: str
    name: str
    section: str | None = None
    description: str | None = None
    room: str | None = None
    state: str = "COURSE_STATE_UNSPECIFIED"
    alternate_link: str | None = None
    creation_time: AwareDatetime | None = None
    update_time: AwareDatetime | None = None


class Assignment(DomainModel):
    google_id: str
    course_id: str
    title: str
    description: str | None = None
    creation_time: AwareDatetime | None = None
    update_time: AwareDatetime | None = None
    due_date: date | None = None
    due_time: time | None = None
    due_at: AwareDatetime | None = None
    alternate_link: str | None = None
    work_type: str = "COURSE_WORK_TYPE_UNSPECIFIED"
    max_points: float | None = None
    topic_id: str | None = None
    state: str = "COURSE_WORK_STATE_UNSPECIFIED"
    raw_payload: dict[str, Any] = Field(default_factory=dict, repr=False)


class SubmissionStatus(StrEnum):
    PENDING = "PENDING"
    TURNED_IN = "TURNED_IN"
    RETURNED = "RETURNED"
    GRADED = "GRADED"
    MISSING = "MISSING"
    UNKNOWN = "UNKNOWN"


class Submission(DomainModel):
    google_id: str
    course_id: str
    assignment_id: str
    state: str = "SUBMISSION_STATE_UNSPECIFIED"
    assigned_grade: float | None = None
    draft_grade: float | None = None
    late: bool | None = None
    creation_time: AwareDatetime | None = None
    update_time: AwareDatetime | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict, repr=False)


class CourseResource(DomainModel):
    google_id: str
    course_id: str
    kind: str
    title: str
    description: str | None = None
    topic_id: str | None = None
    materials: list[dict[str, Any]] = Field(default_factory=list)
    creation_time: AwareDatetime | None = None
    update_time: AwareDatetime | None = None
    alternate_link: str | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict, repr=False)


class Topic(DomainModel):
    google_id: str
    course_id: str
    name: str
    update_time: AwareDatetime | None = None
