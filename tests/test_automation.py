from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from app.automation import reached_midpoint, run_cycle
from app.domain.models import Assignment, Submission
from app.llm.providers import MockProvider
from tests.test_phase4 import phase4 as phase4  # noqa: F401

NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)


def assignment(created: datetime, due: datetime) -> Assignment:
    return Assignment(
        google_id="a",
        course_id="c",
        title="Task",
        description="Crie uma resposta completa.",
        state="PUBLISHED",
        creation_time=created,
        due_at=due,
    )


def test_midpoint_requires_open_pending_second_half():
    pending = Submission(google_id="s", course_id="c", assignment_id="a", state="NEW")
    task = assignment(NOW - timedelta(days=2), NOW + timedelta(days=1))
    assert reached_midpoint(task, pending, NOW)
    assert not reached_midpoint(
        assignment(NOW - timedelta(hours=1), NOW + timedelta(days=2)), pending, NOW
    )
    assert not reached_midpoint(task, pending.model_copy(update={"state": "TURNED_IN"}), NOW)
    assert not reached_midpoint(task, pending, NOW + timedelta(days=1))


def test_cycle_solves_and_generates_once_per_context(phase4):
    settings, engine, source = phase4
    source.assignments[0].creation_time = NOW - timedelta(days=2)
    source.assignments[0].due_at = NOW + timedelta(days=1)
    settings = settings.model_copy(
        update={"generated_root": settings.database_file.parent / "generated"}
    )
    drive = MagicMock()

    first = run_cycle(
        engine, settings, source, drive, provider=MockProvider(), now=NOW
    )
    second = run_cycle(
        engine, settings, source, drive, provider=MockProvider(), now=NOW
    )

    assert first.eligible == first.solved == first.generated == [1]
    assert second.eligible == second.skipped == [1]
    assert second.solved == second.generated == []
