from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from app.classroom.mapper import map_assignment, map_course, map_submission, resolve_due_datetime
from app.domain.models import Assignment, Submission
from app.domain.status import is_overdue, normalize_status

NOW = datetime(2026, 9, 18, tzinfo=UTC)


def assignment(**kwargs):
    return Assignment(google_id="a", course_id="c", title="Work", state="PUBLISHED", **kwargs)


def submission(state, **kwargs):
    return Submission(google_id="s", course_id="c", assignment_id="a", state=state, **kwargs)


@pytest.mark.parametrize(
    ("state", "grade", "expected"),
    [
        ("NEW", None, "PENDING"),
        ("CREATED", None, "PENDING"),
        ("RECLAIMED_BY_STUDENT", 10, "PENDING"),
        ("TURNED_IN", 10, "TURNED_IN"),
        ("RETURNED", None, "RETURNED"),
        ("RETURNED", 0, "GRADED"),
        ("FUTURE_STATE", 10, "UNKNOWN"),
    ],
)
def test_status(state, grade, expected):
    assert normalize_status(assignment(), submission(state, assigned_grade=grade), NOW) == expected


def test_missing_and_late_are_independent():
    task = assignment(due_at=datetime(2026, 9, 17, tzinfo=UTC))
    sub = submission("CREATED", late=False)
    assert is_overdue(task, sub, NOW)
    assert normalize_status(task, sub, NOW) == "MISSING"
    assert sub.late is False
    assert not is_overdue(task, submission("TURNED_IN", late=True), NOW)
    assert normalize_status(task, None, NOW) == "UNKNOWN"
    assert not is_overdue(task, None, NOW)
    assert not is_overdue(task.model_copy(update={"state": "DELETED"}), sub, NOW)


def test_deadline_boundary_and_naive_rejection():
    task = assignment(due_at=NOW)
    assert not is_overdue(task, submission("NEW"), NOW)
    with pytest.raises(ValueError):
        is_overdue(task, None, datetime(2026, 9, 18))
    with pytest.raises(ValidationError):
        assignment(due_at=datetime(2026, 9, 18))


def test_utc_deadline_crosses_local_day():
    due = resolve_due_datetime({"year": 2026, "month": 9, "day": 18}, {})
    assert due is not None
    assert due.isoformat() == "2026-09-17T21:00:00-03:00"
    assert resolve_due_datetime(None, None) is None


@pytest.mark.parametrize("day,hour,expected", [(8, 6, "-05:00"), (8, 7, "-04:00")])
def test_dst(day, hour, expected):
    due = resolve_due_datetime(
        {"year": 2026, "month": 3, "day": day}, {"hours": hour}, "America/New_York"
    )
    assert due is not None
    assert due.isoformat().endswith(expected)


@pytest.mark.parametrize(
    "date,time",
    [
        ({"year": 2026, "month": 1, "day": 1}, None),
        (None, {}),
        ({"year": 2026, "month": 2, "day": 30}, {}),
        ({"year": 2026, "month": 1, "day": 1}, {"nanos": -1}),
    ],
)
def test_invalid_due(date, time):
    with pytest.raises(ValueError):
        resolve_due_datetime(date, time)


def test_mappers_preserve_payload_without_aliasing():
    raw: dict[str, Any] = {
        "id": "a",
        "courseId": "c",
        "title": "T",
        "maxPoints": 0,
        "creationTime": "2026-09-17T01:02:03Z",
        "future": {"nested": 1},
    }
    task = map_assignment(raw)
    raw["future"]["nested"] = 2
    assert task.raw_payload["future"]["nested"] == 1
    assert task.max_points == 0
    assert task.creation_time is not None
    offset = task.creation_time.utcoffset()
    assert offset is not None and offset.total_seconds() == 0
    assert map_course({"id": "c", "name": "Math"}).name == "Math"
    sub = map_submission({"id": "s", "courseId": "c", "courseWorkId": "a", "assignedGrade": 0})
    assert sub.assigned_grade == 0
    assert sub.late is None
