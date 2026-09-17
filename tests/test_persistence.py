from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from app.cli.commands import app
from app.config import Settings
from app.domain.models import Assignment, Course, Submission
from app.domain.status import normalize_status
from app.persistence.database import open_database
from app.persistence.models import AssignmentRecord, CourseRecord, SubmissionRecord, SyncRun
from app.persistence.reader import LocalReader
from app.persistence.repository import Change, upsert_assignment, upsert_course, upsert_submission
from app.sync import synchronize

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)


class FakeSource:
    """Test-only data, never imported by production code."""

    def __init__(self):
        self.courses = [Course(google_id="c", name="Math", state="ACTIVE")]
        self.assignments = [
            Assignment(
                google_id="a",
                course_id="c",
                title="Task",
                description="Original",
                state="PUBLISHED",
                due_at=NOW + timedelta(days=1),
                max_points=10,
            )
        ]
        self.submissions = [
            Submission(
                google_id="s",
                course_id="c",
                assignment_id="a",
                state="NEW",
                late=False,
            )
        ]
        self.fail_courses: set[str] = set()
        self.fail_list = False

    def list_courses(self, *, include_archived=False):
        if self.fail_list:
            raise RuntimeError("SECRET access_token=abc")
        return deepcopy(self.courses)

    def list_assignments(self, course_id):
        if course_id in self.fail_courses:
            raise RuntimeError("SECRET refresh_token=xyz")
        return deepcopy([a for a in self.assignments if a.course_id == course_id])

    def list_submissions(self, course_id):
        return deepcopy([s for s in self.submissions if s.course_id == course_id])


@pytest.fixture
def database(tmp_path):
    engine = open_database(tmp_path / "classroom.db")
    yield engine
    engine.dispose()


def test_schema_upsert_and_time(database):
    source = FakeSource()
    source.courses[0].raw_payload = {"id": "c", "name": "Math", "access_token": "SECRET"}
    with Session(database) as session, session.begin():
        c, change = upsert_course(session, source.courses[0], NOW)
        assert change == Change.NEW and c.id == 1
        a, change = upsert_assignment(session, source.assignments[0], NOW, c)
        assert change == Change.NEW and a.id == 1
        s, change = upsert_submission(session, source.submissions[0], NOW, a)
        assert change == Change.NEW and s.id == 1
        assert "SECRET" not in str(c.raw_payload)
    with Session(database) as session, session.begin():
        c, change = upsert_course(session, source.courses[0], NOW + timedelta(hours=1))
        assert change == Change.UNCHANGED
        assert c.first_seen_at == NOW
        assert c.last_synced_at == NOW + timedelta(hours=1)
        assert c.last_seen_at.utcoffset() is not None
        a, change = upsert_assignment(session, source.assignments[0], NOW, c)
        assert change == Change.UNCHANGED
        _, change = upsert_submission(session, source.submissions[0], NOW, a)
        assert change == Change.UNCHANGED
    with database.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert conn.exec_driver_sql("PRAGMA user_version").scalar() == 3


def test_sync_repeated(database):
    source = FakeSource()
    first = synchronize(database, lambda: source, clock=lambda: NOW)
    second = synchronize(database, lambda: source, clock=lambda: NOW + timedelta(hours=1))
    assert first.status == second.status == "SUCCESS"
    assert first.counters["assignments"]["created"] == 1
    assert second.counters["assignments"]["unchanged"] == 1
    assert second.counters["courses"]["updated"] == 0
    with Session(database) as session:
        for model in (CourseRecord, AssignmentRecord, SubmissionRecord):
            assert session.scalar(select(func.count()).select_from(model)) == 1
        assert session.scalar(select(func.count()).select_from(SyncRun)) == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("title", "Changed"),
        ("description", "New description"),
        ("due_at", NOW + timedelta(days=3)),
        ("max_points", 20),
        ("state", "DELETED"),
    ],
)
def test_assignment_changes(database, field, value):
    source = FakeSource()
    synchronize(database, lambda: source)
    setattr(source.assignments[0], field, value)
    result = synchronize(database, lambda: source)
    assert result.counters["assignments"]["updated"] == 1
    assert synchronize(database, lambda: source).counters["assignments"]["unchanged"] == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "TURNED_IN"),
        ("assigned_grade", 0),
        ("draft_grade", 9),
        ("late", True),
    ],
)
def test_submission_changes(database, field, value):
    source = FakeSource()
    synchronize(database, lambda: source)
    setattr(source.submissions[0], field, value)
    assert synchronize(database, lambda: source).counters["submissions"]["updated"] == 1


def test_course_change_and_google_timestamp_only(database):
    source = FakeSource()
    synchronize(database, lambda: source)
    source.courses[0].name = "New name"
    assert synchronize(database, lambda: source).counters["courses"]["updated"] == 1
    source.assignments[0].update_time = NOW
    assert synchronize(database, lambda: source).counters["assignments"]["unchanged"] == 1
    with Session(database) as session:
        row = session.scalar(select(AssignmentRecord))
        assert row is not None and row.update_time == NOW


def test_partial_failed_and_sanitized_errors(database):
    source = FakeSource()
    source.courses.append(Course(google_id="d", name="Other", state="ACTIVE"))
    source.fail_courses.add("d")
    partial = synchronize(database, lambda: source)
    assert partial.status == "PARTIAL" and partial.finished_at is not None
    assert partial.counters["courses"]["found"] == 2
    assert partial.counters["courses"]["created"] == 1
    assert partial.error is not None and "SECRET" not in partial.error
    source.fail_courses.add("c")
    assert synchronize(database, lambda: source).status == "FAILED"
    source.fail_list = True
    assert synchronize(database, lambda: source).status == "FAILED"
    assert len(LocalReader(database, Settings()).list_courses()) == 1


def test_factory_failure_is_recorded_and_running_visible(database):
    def fail():
        with Session(database) as session:
            row = session.scalar(select(SyncRun))
            assert row is not None and row.status == "RUNNING"
        raise RuntimeError("SECRET")

    result = synchronize(database, fail)
    assert result.status == "FAILED" and result.error is not None and "SECRET" not in result.error


def test_rollback_mid_course_preserves_snapshot_and_counters(database):
    source = FakeSource()
    synchronize(database, lambda: source)
    source.courses[0].name = "Must roll back"
    source.assignments[0].title = "Must roll back"
    with patch("app.sync.upsert_submission", side_effect=RuntimeError("SECRET")):
        result = synchronize(database, lambda: source)
    assert result.status == "FAILED"
    assert result.counters["courses"]["updated"] == 0
    reader = LocalReader(database, Settings())
    assert reader.list_courses()[0].name == "Math"
    assert reader.list_assignments("c")[0].title == "Task"


def test_constraints_and_naive_time_rejected(database):
    source = FakeSource()
    synchronize(database, lambda: source)
    with Session(database) as session, pytest.raises(IntegrityError):
        row = session.scalar(select(AssignmentRecord))
        assert row is not None
        row.course_id = 999
        session.commit()
    with Session(database) as session, pytest.raises(ValueError, match="timezone"):
        upsert_course(session, source.courses[0], datetime(2026, 1, 1))
    with Session(database) as session, pytest.raises(IntegrityError):
        original = session.scalar(select(CourseRecord))
        fields = {
            column.name: getattr(original, column.name)
            for column in CourseRecord.__table__.columns
            if column.name != "id"
        }
        session.add(CourseRecord(**fields))
        session.commit()


def test_absence_hides_and_reappearance_reuses_ids(database):
    source = FakeSource()
    synchronize(database, lambda: source)
    assignments, submissions = source.assignments, source.submissions
    source.assignments, source.submissions = [], []
    synchronize(database, lambda: source)
    assert LocalReader(database, Settings()).list_assignments("c") == []
    source.assignments, source.submissions = assignments, submissions
    result = synchronize(database, lambda: source)
    assert result.counters["assignments"]["updated"] == 1
    with Session(database) as session:
        row = session.scalar(select(AssignmentRecord))
        assert row is not None and row.id == 1
    source.submissions = []
    synchronize(database, lambda: source)
    reader = LocalReader(database, Settings())
    assert reader.list_submissions("c") == []
    assert normalize_status(reader.list_assignments("c")[0], None, NOW) == "UNKNOWN"


def test_local_pending_order_status_timezone_and_no_auth(database):
    source = FakeSource()
    for identifier, due in [("z", None), ("b", NOW - timedelta(days=2))]:
        source.assignments.append(
            Assignment(
                google_id=identifier,
                course_id="c",
                title=f"Task-{identifier}",
                due_at=due,
                state="PUBLISHED",
            )
        )
        source.submissions.append(
            Submission(
                google_id=f"s-{identifier}",
                course_id="c",
                assignment_id=identifier,
                state="NEW",
                late=True,
            )
        )
    synchronize(database, lambda: source)
    reader = LocalReader(database, Settings())
    rows = reader.list_assignments("c")
    assert rows[0].due_at is not None and rows[0].due_at.utcoffset() == timedelta(hours=-3)
    assert normalize_status(rows[0], reader.list_submissions("c")[0], NOW) == "PENDING"
    with (
        patch("app.cli.commands.get_reader", return_value=reader),
        patch("app.cli.commands.authenticate", side_effect=AssertionError("No auth")),
    ):
        result = CliRunner().invoke(app, ["pending"], env={"COLUMNS": "240"})
    assert result.exit_code == 0, result.output
    assert result.output.index("Task-b") < result.output.index("Task-z")
    assert "MISSING" in result.output and "PENDING" in result.output
    assert "-0300" in result.output and "True" in result.output


def test_cli_empty_local_help_and_failed_sync(tmp_path):
    settings = Settings(database_file=tmp_path / "fresh.db")
    with (
        patch("app.cli.commands.load_settings", return_value=settings),
        patch("app.cli.commands.get_client", side_effect=RuntimeError("SECRET")),
    ):
        assert CliRunner().invoke(app, ["sync", "--help"]).exit_code == 0
        empty = CliRunner().invoke(app, ["courses"])
        assert empty.exit_code == 1 and "sync" in empty.output
        failed = CliRunner().invoke(app, ["sync"])
        assert failed.exit_code == 1 and "FAILED" in failed.output
        assert "SECRET" not in failed.output


def test_init_is_idempotent_and_future_schema_rejected(tmp_path):
    path = tmp_path / "schema.db"
    engine = open_database(path)
    engine.dispose()
    engine = open_database(path)
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA user_version=99")
    engine.dispose()
    with pytest.raises(ValueError, match="schema"):
        open_database(path)
