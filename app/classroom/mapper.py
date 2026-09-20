from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.domain.models import Assignment, Course, CourseResource, Submission, Topic


def resolve_due_datetime(
    due_date: dict[str, int] | None,
    due_time: dict[str, int] | None,
    timezone: str = "America/Sao_Paulo",
) -> datetime | None:
    if due_date is None and due_time is None:
        return None
    if due_date is None or due_time is None:
        raise ValueError("Classroom dueDate and dueTime must be present together")
    nanos = due_time.get("nanos", 0)
    if not 0 <= nanos < 1_000_000_000:
        raise ValueError("Invalid nanoseconds")
    return datetime(
        due_date["year"],
        due_date["month"],
        due_date["day"],
        due_time.get("hours", 0),
        due_time.get("minutes", 0),
        due_time.get("seconds", 0),
        nanos // 1000,
        tzinfo=UTC,
    ).astimezone(ZoneInfo(timezone))


def map_course(raw: dict[str, Any]) -> Course:
    return Course(
        raw_payload=deepcopy(raw),
        google_id=raw["id"],
        name=raw["name"],
        section=raw.get("section"),
        description=raw.get("description"),
        room=raw.get("room"),
        state=raw.get("courseState", "COURSE_STATE_UNSPECIFIED"),
        alternate_link=raw.get("alternateLink"),
        creation_time=raw.get("creationTime"),
        update_time=raw.get("updateTime"),
    )


def map_assignment(raw: dict[str, Any], timezone: str = "America/Sao_Paulo") -> Assignment:
    due = resolve_due_datetime(raw.get("dueDate"), raw.get("dueTime"), timezone)
    utc_due = due.astimezone(UTC) if due else None
    return Assignment(
        google_id=raw["id"],
        course_id=raw["courseId"],
        title=raw["title"],
        description=raw.get("description"),
        creation_time=raw.get("creationTime"),
        update_time=raw.get("updateTime"),
        due_date=utc_due.date() if utc_due else None,
        due_time=utc_due.timetz() if utc_due else None,
        due_at=due,
        alternate_link=raw.get("alternateLink"),
        work_type=raw.get("workType", "COURSE_WORK_TYPE_UNSPECIFIED"),
        max_points=raw.get("maxPoints"),
        topic_id=raw.get("topicId"),
        state=raw.get("state", "COURSE_WORK_STATE_UNSPECIFIED"),
        raw_payload=deepcopy(raw),
    )


def map_submission(raw: dict[str, Any]) -> Submission:
    return Submission(
        google_id=raw["id"],
        course_id=raw["courseId"],
        assignment_id=raw["courseWorkId"],
        state=raw.get("state", "SUBMISSION_STATE_UNSPECIFIED"),
        assigned_grade=raw.get("assignedGrade"),
        draft_grade=raw.get("draftGrade"),
        late=raw.get("late"),
        creation_time=raw.get("creationTime"),
        update_time=raw.get("updateTime"),
        raw_payload=deepcopy(raw),
    )


def map_course_resource(raw: dict[str, Any], kind: str) -> CourseResource:
    materials = raw.get("materials", [])
    if not isinstance(materials, list) or any(not isinstance(item, dict) for item in materials):
        raise ValueError("Invalid course resource materials")
    return CourseResource(
        google_id=raw["id"],
        course_id=raw["courseId"],
        kind=kind,
        title=raw.get("title") or (raw.get("text") or "Comunicado")[:200],
        description=raw.get("description") or raw.get("text"),
        topic_id=raw.get("topicId"),
        materials=deepcopy(materials),
        creation_time=raw.get("creationTime"),
        update_time=raw.get("updateTime"),
        alternate_link=raw.get("alternateLink"),
        raw_payload=deepcopy(raw),
    )


def map_topic(raw: dict[str, Any], course_id: str) -> Topic:
    return Topic(
        google_id=raw["topicId"],
        course_id=course_id,
        name=raw["name"],
        update_time=raw.get("updateTime"),
    )
