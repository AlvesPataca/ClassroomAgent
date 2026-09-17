from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.domain.models import Assignment, Course, Submission
from app.errors import AppError
from app.persistence.models import AssignmentRecord, CourseRecord, SubmissionRecord, SyncRun


class LocalReader:
    def __init__(self, engine: Engine, settings: Settings) -> None:
        self.settings = settings
        # Load all three tables in one read transaction: no mixed sync snapshots on CLI reads.
        with Session(engine) as session:
            session.connection().exec_driver_sql("BEGIN")
            latest = session.scalar(select(SyncRun).order_by(SyncRun.id.desc()).limit(1))
            if latest is None:
                raise AppError(
                    "Banco ainda não sincronizado. Execute python main.py sync ou use --api."
                )
            self.sync_status = latest.status
            self.synced_at = latest.finished_at
            self._courses = [
                Course.model_validate(row.snapshot)
                for row in session.scalars(
                    select(CourseRecord)
                    .where(CourseRecord.present.is_(True))
                    .order_by(CourseRecord.id)
                )
            ]
            self._assignments = [
                Assignment.model_validate(row.snapshot)
                for row in session.scalars(
                    select(AssignmentRecord)
                    .where(AssignmentRecord.present.is_(True))
                    .order_by(AssignmentRecord.id)
                )
            ]
            self._submissions = [
                Submission.model_validate(row.snapshot)
                for row in session.scalars(
                    select(SubmissionRecord)
                    .where(SubmissionRecord.present.is_(True))
                    .order_by(SubmissionRecord.id)
                )
            ]

    def list_courses(self, *, include_archived: bool = False) -> list[Course]:
        states = {"ACTIVE", "ARCHIVED"} if include_archived else {"ACTIVE"}
        return [row for row in self._courses if row.state in states]

    def list_assignments(self, course_id: str) -> list[Assignment]:
        rows = [
            row.model_copy(deep=True)
            for row in self._assignments
            if row.course_id == course_id and row.state == "PUBLISHED"
        ]
        for row in rows:
            if row.due_at is not None:
                row.due_at = row.due_at.astimezone(self.settings.zone)
        return rows

    def list_submissions(self, course_id: str) -> list[Submission]:
        return [row for row in self._submissions if row.course_id == course_id]
