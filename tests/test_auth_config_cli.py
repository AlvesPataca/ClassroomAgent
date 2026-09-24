import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from google.auth.exceptions import RefreshError
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from app.auth.google import authenticate, save_token
from app.cli.commands import app
from app.config import SCOPES, Settings, load_settings
from app.domain.models import Assignment, Course, Submission
from app.errors import AppError
from app.persistence.database import open_database
from app.persistence.models import AssignmentRecord
from app.sync import synchronize
from tests.test_persistence import FakeSource


def test_config_relative_paths_and_environment(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("TIMEZONE=UTC\nMAX_RETRIES=2\n")
    monkeypatch.setenv("TIMEZONE", "America/Sao_Paulo")
    settings = load_settings(tmp_path)
    assert settings.timezone == "America/Sao_Paulo"
    assert settings.credentials_file == tmp_path / "credentials.json"
    assert settings.max_retries == 2
    monkeypatch.setenv("TIMEZONE", "invalid")
    with pytest.raises(AppError):
        load_settings(tmp_path)


def settings_for(tmp_path):
    return Settings(
        credentials_file=tmp_path / "credentials.json", token_file=tmp_path / "token.json"
    )


def test_missing_files_never_open_browser(tmp_path):
    with patch("app.auth.google.InstalledAppFlow") as flow:
        with pytest.raises(AppError, match="não autenticada"):
            authenticate(settings_for(tmp_path))
        with pytest.raises(AppError, match="não encontrado"):
            authenticate(settings_for(tmp_path), interactive=True)
        flow.assert_not_called()


@pytest.mark.parametrize("valid", [True, False])
def test_cached_token_and_refresh(tmp_path, valid):
    settings = settings_for(tmp_path)
    settings.token_file.write_text(json.dumps({"scopes": SCOPES}))
    cred = MagicMock(
        valid=valid, expired=not valid, refresh_token="secret", granted_scopes=None, scopes=SCOPES
    )

    def refreshed(request):
        cred.valid = True
        cred.expired = False

    cred.refresh.side_effect = refreshed
    cred.to_json.return_value = json.dumps({"scopes": SCOPES, "token": "refreshed"})
    with patch("app.auth.google.Credentials.from_authorized_user_info", return_value=cred):
        assert authenticate(settings) is cred
    assert cred.refresh.call_count == (0 if valid else 1)
    assert not list(tmp_path.glob(".oauth-*"))


def test_refresh_failure_does_not_overwrite_or_leak(tmp_path):
    settings = settings_for(tmp_path)
    original = json.dumps({"scopes": SCOPES})
    settings.token_file.write_text(original)
    cred = MagicMock(valid=False, refresh_token="secret")
    cred.refresh.side_effect = RefreshError("SECRET")
    with patch("app.auth.google.Credentials.from_authorized_user_info", return_value=cred):
        with pytest.raises(AppError) as caught:
            authenticate(settings)
    assert "SECRET" not in str(caught.value)
    assert settings.token_file.read_text() == original


def test_rejects_incomplete_token_scopes(tmp_path):
    settings = settings_for(tmp_path)
    settings.token_file.write_text(json.dumps({"scopes": [SCOPES[0], "write"]}))
    with pytest.raises(AppError, match="Scopes"):
        authenticate(settings)


def test_desktop_flow_uses_only_readonly_scopes(tmp_path):
    settings = settings_for(tmp_path)
    settings.credentials_file.write_text('{"installed": {}}')
    cred = MagicMock(granted_scopes=SCOPES, valid=True, expired=False, refresh_token="secret")
    cred.to_json.return_value = json.dumps({"scopes": SCOPES})
    with patch("app.auth.google.InstalledAppFlow") as flow:
        flow.from_client_config.return_value.run_local_server.return_value = cred
        authenticate(settings, interactive=True)
        assert flow.from_client_config.call_args.kwargs["scopes"] == SCOPES
        assert flow.from_client_config.return_value.run_local_server.call_args.kwargs["port"] == 0
    assert settings.token_file.exists()


def test_atomic_token_write_failure_preserves_original(tmp_path):
    target = tmp_path / "token.json"
    target.write_text("original")
    cred = MagicMock()
    cred.to_json.return_value = "new"
    with patch("app.auth.google.os.replace", side_effect=OSError()), pytest.raises(OSError):
        save_token(cred, target)
    assert target.read_text() == "original"
    assert not list(tmp_path.glob(".oauth-*"))


@pytest.mark.parametrize(
    "command",
    [
        [],
        ["auth", "--help"],
        ["courses", "--help"],
        ["assignments", "--help"],
        ["pending", "--help"],
    ],
)
def test_cli_help(command):
    with patch("app.cli.commands._run_automation"):
        result = CliRunner().invoke(app, command or [])
    assert result.exit_code == 0


def test_cli_pending_and_partial_failure():
    client = MagicMock(settings=Settings())
    client.list_courses.return_value = [
        Course(google_id="c", name="Math"),
        Course(google_id="d", name="Other"),
    ]
    client.list_assignments.side_effect = [
        [Assignment(google_id="a", course_id="c", title="[bold]Literal[/bold]", state="PUBLISHED")],
        AppError("Acesso negado"),
    ]
    client.list_submissions.return_value = [
        Submission(google_id="s", course_id="c", assignment_id="a", state="NEW")
    ]
    with patch("app.cli.commands.get_client", return_value=client):
        result = CliRunner().invoke(app, ["pending", "--api"], env={"COLUMNS": "220"})
    assert result.exit_code == 1
    assert "PENDING" in result.output
    assert "[bold]Literal[/bold]" in result.output
    assert "Resultado parcial" in result.output


def test_pending_hides_overdue_and_completed_but_keeps_undated_open_work():
    now = datetime.now(UTC)
    client = MagicMock(settings=Settings())
    course = Course(google_id="c", name="Math", state="ACTIVE")
    client.list_courses.return_value = [course]
    client.list_assignments.return_value = [
        Assignment(
            google_id="future",
            course_id="c",
            title="PENDING futuro",
            state="PUBLISHED",
            due_at=now + timedelta(days=2),
        ),
        Assignment(
            google_id="late",
            course_id="c",
            title="PENDING vencido",
            state="PUBLISHED",
            due_at=now - timedelta(days=1),
        ),
        Assignment(
            google_id="done",
            course_id="c",
            title="Concluída",
            state="PUBLISHED",
            due_at=now + timedelta(days=2),
        ),
        Assignment(google_id="no-due", course_id="c", title="Sem prazo", state="PUBLISHED"),
    ]
    client.list_submissions.return_value = [
        Submission(google_id="s1", course_id="c", assignment_id="future", state="NEW"),
        Submission(google_id="s2", course_id="c", assignment_id="late", state="NEW"),
        Submission(google_id="s3", course_id="c", assignment_id="done", state="TURNED_IN"),
        Submission(google_id="s4", course_id="c", assignment_id="no-due", state="CREATED"),
    ]

    with patch("app.cli.commands.get_client", return_value=client):
        result = CliRunner().invoke(app, ["pending", "--api"], env={"COLUMNS": "220"})

    assert result.exit_code == 0, result.output
    assert "PENDING futuro" in result.output and "Sem prazo" in result.output
    assert "PENDING vencido" not in result.output
    assert "Concluída" not in result.output
    assert "2 atividade(s)" in result.output


def test_assignments_has_operational_default_and_overdue_history_flag():
    now = datetime.now(UTC)
    client = MagicMock(settings=Settings())
    client.list_courses.return_value = [Course(google_id="c", name="Math", state="ACTIVE")]
    client.list_assignments.return_value = [
        Assignment(
            google_id="future",
            course_id="c",
            title="Atual",
            state="PUBLISHED",
            due_at=now + timedelta(days=1),
        ),
        Assignment(
            google_id="old",
            course_id="c",
            title="Histórica atrasada",
            state="PUBLISHED",
            due_at=now - timedelta(days=300),
        ),
    ]
    client.list_submissions.return_value = [
        Submission(google_id="s1", course_id="c", assignment_id="future", state="NEW"),
        Submission(google_id="s2", course_id="c", assignment_id="old", state="NEW"),
    ]

    with patch("app.cli.commands.get_client", return_value=client):
        current = CliRunner().invoke(app, ["assignments", "--api"], env={"COLUMNS": "220"})
        history = CliRunner().invoke(
            app, ["assignments", "--api", "--include-overdue"], env={"COLUMNS": "220"}
        )

    assert current.exit_code == history.exit_code == 0
    assert "Atual" in current.output and "Histórica atrasada" not in current.output
    assert "Histórica atrasada" in history.output


def test_local_assignments_preserves_old_database_history(tmp_path):
    settings = Settings(database_file=tmp_path / "classroom.db")
    source = FakeSource()
    source.assignments[0].creation_time = datetime.now(UTC) - timedelta(days=500)
    source.assignments[0].due_at = datetime.now(UTC) - timedelta(days=300)
    engine = open_database(settings.database_file)
    assert synchronize(engine, lambda: source).status == "SUCCESS"
    before = engine.connect().exec_driver_sql("SELECT COUNT(*) FROM assignments").scalar_one()
    engine.dispose()

    with patch("app.cli.commands.load_settings", return_value=settings):
        result = CliRunner().invoke(app, ["local-assignments"])

    assert result.exit_code == 0 and "Task" in result.output and "True" in result.output
    engine = open_database(settings.database_file)
    try:
        with Session(engine) as session:
            row = session.query(AssignmentRecord).one()
            after = session.query(AssignmentRecord).count()
        assert before == after == 1
        assert row.present is True
        assert row.snapshot["title"] == "Task"
    finally:
        engine.dispose()


def test_debug_never_prints_exception_payload():
    try:
        raise ValueError("SECRET_TOKEN")
    except ValueError as cause:
        safe = AppError("Erro seguro")
        safe.__cause__ = cause
    with patch("app.cli.commands.get_client", side_effect=safe):
        result = CliRunner().invoke(app, ["--debug", "courses", "--api"])
    assert result.exit_code == 1
    assert "SECRET_TOKEN" not in result.output
    assert "ValueError" in result.output
