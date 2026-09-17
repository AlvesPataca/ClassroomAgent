from datetime import datetime

from app.domain.models import Assignment, Submission, SubmissionStatus

ACTIONABLE = {"NEW", "CREATED", "RECLAIMED_BY_STUDENT"}


def is_overdue(assignment: Assignment, submission: Submission | None, now: datetime) -> bool:
    if now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return bool(
        assignment.state == "PUBLISHED"
        and assignment.due_at is not None
        and assignment.due_at < now
        and submission is not None
        and submission.state in ACTIONABLE
    )


def normalize_status(
    assignment: Assignment, submission: Submission | None, now: datetime
) -> SubmissionStatus:
    overdue = is_overdue(assignment, submission, now)
    if submission is None:
        return SubmissionStatus.UNKNOWN
    if submission.state in ACTIONABLE:
        return SubmissionStatus.MISSING if overdue else SubmissionStatus.PENDING
    if submission.state == "TURNED_IN":
        return SubmissionStatus.TURNED_IN
    if submission.state == "RETURNED":
        return (
            SubmissionStatus.GRADED
            if submission.assigned_grade is not None
            else SubmissionStatus.RETURNED
        )
    return SubmissionStatus.UNKNOWN
