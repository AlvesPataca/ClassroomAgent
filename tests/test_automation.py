import io
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from app.automation import CycleResult, reached_midpoint, run_cycle, run_forever
from app.config import Settings
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


def automation_clients(source):
    classroom = MagicMock()
    classroom.list_courses.side_effect = source.list_courses
    classroom.list_assignments.side_effect = source.list_assignments
    classroom.list_submissions.side_effect = source.list_submissions
    classroom.list_course_resources.side_effect = source.list_course_resources
    classroom.list_topics.side_effect = source.list_topics
    classroom.submission_material_ids.return_value = set()
    drive = MagicMock()
    drive.service.files.return_value.list.return_value.execute.return_value = {
        "files": [{"id": "folder-id"}]
    }
    drive.service.files.return_value.create.return_value.execute.return_value = {
        "id": "generated-file-id",
        "webViewLink": "https://drive.google.com/generated-file-id",
    }
    return classroom, drive


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
    classroom, drive = automation_clients(source)

    first = run_cycle(
        engine, settings, classroom, drive, provider=MockProvider(), now=NOW
    )
    second = run_cycle(
        engine, settings, classroom, drive, provider=MockProvider(), now=NOW
    )

    assert first.eligible == first.solved == first.generated == [1]
    assert second.eligible == second.skipped == [1]
    assert second.solved == second.generated == []


def test_cycle_logs_operational_metadata_without_answer_content(phase4):
    settings, engine, source = phase4
    source.assignments[0].creation_time = NOW - timedelta(days=2)
    source.assignments[0].due_at = NOW + timedelta(days=1)
    settings = settings.model_copy(
        update={"generated_root": settings.database_file.parent / "generated"}
    )
    stream = io.StringIO()
    logger = logging.getLogger("test.automation.observability")
    logger.handlers = [logging.StreamHandler(stream)]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    classroom, drive = automation_clients(source)

    result = run_cycle(
        engine,
        settings,
        classroom,
        drive,
        provider=MockProvider(),
        now=NOW,
        logger=logger,
    )

    output = stream.getvalue()
    assert result.errors == {}
    assert "[ATIVIDADE]" in output
    assert "Sincronização concluída" in output
    assert "Contexto extraído" in output
    assert "Resposta gerada" in output
    assert "Rascunho atualizado" in output
    assert "Explique os princípios SOLID" not in output


def test_continuous_flow_logs_summary_duration_and_next_run(tmp_path):
    stream = io.StringIO()
    logger = logging.getLogger("test.automation.schedule")
    logger.handlers = [logging.StreamHandler(stream)]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    drive = MagicMock()
    times = iter(
        (
            NOW,
            NOW,
            NOW + timedelta(seconds=3),
            NOW + timedelta(seconds=4),
        )
    )
    settings = Settings(
        automation_interval_hours=8,
        database_file=tmp_path / "classroom.db",
        cycle_lock_file=tmp_path / "cycle.lock",
    )

    with (
        patch("app.automation.authenticate", return_value=object()),
        patch("app.automation.ClassroomClient.from_credentials", return_value=MagicMock()),
        patch("app.automation.DriveClient.from_credentials", return_value=drive),
        patch(
            "app.automation.run_cycle",
            return_value=CycleResult("SUCCESS", eligible=[1], solved=[1], generated=[1]),
        ),
        pytest.raises(RuntimeError, match="stop after scheduling"),
    ):
        run_forever(
            settings,
            logger=logger,
            clock=lambda: next(times),
            timer=iter((10.0, 12.5)).__next__,
            wait=lambda _: (_ for _ in ()).throw(RuntimeError("stop after scheduling")),
        )

    output = stream.getvalue()
    assert "[RESUMO]" in output and "duração=2.5s" in output
    assert "Próxima verificação: 20/09/2026 17:00:00" in output
    drive.close.assert_called_once()
