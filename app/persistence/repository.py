import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from app.attachments.materials import discover
from app.domain.models import Assignment, Course, Submission
from app.persistence.models import AssignmentRecord, CourseRecord, Record, SubmissionRecord


class Change(StrEnum):
    NEW = "NEW"
    UPDATED = "UPDATED"
    UNCHANGED = "UNCHANGED"


# Deliberate allowlists: no OAuth, URLs with access tokens, or unrelated attachments.
RAW_FIELDS = {
    Course: {
        "id",
        "name",
        "section",
        "description",
        "descriptionHeading",
        "room",
        "courseState",
        "creationTime",
        "updateTime",
        "shortAnswerSubmission",
        "multipleChoiceSubmission",
    },
    Assignment: {
        "id",
        "courseId",
        "title",
        "description",
        "state",
        "creationTime",
        "updateTime",
        "dueDate",
        "dueTime",
        "maxPoints",
        "workType",
        "multipleChoiceQuestion",
    },
    Submission: {
        "id",
        "courseId",
        "courseWorkId",
        "userId",
        "state",
        "assignedGrade",
        "draftGrade",
        "late",
        "creationTime",
        "updateTime",
    },
}
R = TypeVar("R", bound=Record)


def upsert(
    session: Session,
    model: type[R],
    item: Course | Assignment | Submission,
    now: datetime,
    **parent: int,
) -> tuple[R, Change]:
    """Caller owns a BEGIN IMMEDIATE transaction: classification and write are atomic."""
    if now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    snapshot = item.model_dump(mode="json", exclude={"raw_payload"})
    raw = {key: value for key, value in item.raw_payload.items() if key in RAW_FIELDS[type(item)]}
    if isinstance(raw.get("multipleChoiceQuestion"), dict):
        choices = raw["multipleChoiceQuestion"].get("choices", [])
        raw["multipleChoiceQuestion"] = {
            "choices": [v for v in choices if isinstance(v, str)]
            if isinstance(choices, list)
            else []
        }
    for key in ("shortAnswerSubmission", "multipleChoiceSubmission"):
        if isinstance(raw.get(key), dict):
            raw[key] = {"answer": raw[key].get("answer")}
    relevant = {k: v for k, v in snapshot.items() if k not in {"creation_time", "update_time"}}
    if isinstance(item, Assignment) and item.due_at is not None:
        relevant["due_at"] = item.due_at.astimezone(UTC).isoformat()
    relevant["extra"] = {
        k: raw[k]
        for k in ("shortAnswerSubmission", "multipleChoiceSubmission", "multipleChoiceQuestion")
        if k in raw
    }
    digest = hashlib.sha256(
        json.dumps(relevant, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    identity = {"google_id": item.google_id, **parent}
    previous = session.scalar(select(model).filter_by(**identity))
    change = (
        Change.NEW
        if previous is None
        else Change.UPDATED
        if previous.fingerprint != digest or not previous.present
        else Change.UNCHANGED
    )
    values = dict(
        **identity,
        snapshot=snapshot,
        raw_payload=raw,
        fingerprint=digest,
        first_seen_at=now,
        last_seen_at=now,
        last_synced_at=now,
        creation_time=item.creation_time,
        update_time=item.update_time,
        present=True,
    )
    statement = insert(model).values(**values)
    session.execute(
        statement.on_conflict_do_update(
            index_elements=list(identity),
            set_={
                key: value
                for key, value in values.items()
                if key not in {*identity, "first_seen_at"}
            },
        )
    )
    session.expire_all()
    row = session.scalar(select(model).filter_by(**identity))
    assert row is not None
    return row, change


def upsert_course(session: Session, item: Course, now: datetime) -> tuple[CourseRecord, Change]:
    return upsert(session, CourseRecord, item, now)


def upsert_assignment(
    session: Session, item: Assignment, now: datetime, course: CourseRecord
) -> tuple[AssignmentRecord, Change]:
    if item.course_id != course.google_id:
        raise ValueError("Assignment course mismatch")
    row, change = upsert(session, AssignmentRecord, item, now, course_id=course.id)
    if discover(session, row.id, item.raw_payload.get("materials", []), now):
        if change == Change.UNCHANGED:
            change = Change.UPDATED
    return row, change


def upsert_submission(
    session: Session, item: Submission, now: datetime, assignment: AssignmentRecord
) -> tuple[SubmissionRecord, Change]:
    if (
        item.assignment_id != assignment.google_id
        or item.course_id != assignment.snapshot["course_id"]
    ):
        raise ValueError("Submission assignment mismatch")
    return upsert(session, SubmissionRecord, item, now, assignment_id=assignment.id)
