from datetime import timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.api import create_app
from app.domain.models import Assignment, Course, Submission
from app.monitoring import finish_cycle, start_cycle
from app.persistence.database import open_database
from app.persistence.models import (
    AssignmentRecord,
    AttachmentRecord,
    CourseRecord,
    GeneratedArtifact,
    SolutionRecord,
    SubmissionRecord,
)
from app.persistence.repository import upsert
from tests.test_api import NOW, api_settings


@pytest.fixture
def management(tmp_path):
    settings = api_settings(tmp_path).model_copy(
        update={
            "generated_root": tmp_path / "generated",
            "credentials_file": tmp_path / "credentials.json",
            "gemini_api_key": SecretStr("SUPER_SECRET_API_KEY_123"),
        }
    )
    settings.token_file.write_text('{"refresh_token":"SUPER_SECRET_REFRESH_TOKEN_456"}')
    settings.generated_root.mkdir()
    engine = open_database(settings.database_file)
    with Session(engine) as session, session.begin():
        course, _ = upsert(
            session,
            CourseRecord,
            Course(google_id="course", name="Disciplina exemplo"),
            NOW,
        )

        for index in range(1, 4):
            assignment, _ = upsert(
                session,
                AssignmentRecord,
                Assignment(
                    google_id=str(index),
                    course_id="course",
                    title=f"Atividade {index}",
                    description="Enunciado para revisão",
                    state="PUBLISHED",
                ),
                NOW,
                course_id=course.id,
            )
            upsert(
                session,
                SubmissionRecord,
                Submission(
                    google_id=str(index),
                    course_id="course",
                    assignment_id=str(index),
                    state="TURNED_IN" if index == 3 else "NEW",
                ),
                NOW,
                assignment_id=assignment.id,
            )
        for version in (1, 2):
            solution = SolutionRecord(
                assignment_id=1,
                version=version,
                status="READY",
                provider="mock",
                model="mock",
                context_hash="hash",
                request_metadata={"prompt": "SUPER_SECRET_API_KEY_123"},
                response={
                    "assignment_types": ["LONG_FORM"],
                    "deliverable": "Texto final",
                    "understanding": "PRIVATE_REASONING",
                    "answer": "INTERNAL_PREVIEW",
                },
                answer="INTERNAL_PREVIEW",
                created_at=NOW,
                updated_at=NOW,
            )
            session.add(solution)
            session.flush()
            path = settings.generated_root / f"example-{version}.pdf"
            path.write_bytes(b"%PDF-1.7\nexample content\n")
            session.add(
                GeneratedArtifact(
                    solution_id=solution.id,
                    assignment_id=1,
                    version=version,
                    artifact_type="PDF",
                    template="ACADEMIC_REPORT",
                    local_path=str(path),
                    content_hash="hash",
                    status="READY",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        session.add(
            AttachmentRecord(
                assignment_id=1,
                identity_key="attachment",
                kind="DRIVE_FILE",
                title="Referência",
                mime_type="application/pdf",
                fingerprint="hash",
                raw_payload={"secret": "SUPER_SECRET_REFRESH_TOKEN_456"},
                local_path="PRIVATE_PATH",
                status="DISCOVERED",
                first_seen_at=NOW,
                last_seen_at=NOW,
                updated_at=NOW,
            )
        )
    with TestClient(create_app(settings)) as client:
        yield client, settings, engine
    engine.dispose()


def test_assignment_list_filter_pagination_and_detail(management):
    client, _, _ = management
    assert len(client.get("/api/v1/assignments").json()) == 3
    assert len(client.get("/api/v1/assignments?status=NEW").json()) == 2
    assert client.get("/api/v1/assignments?course_id=999").json() == []
    assert client.get("/api/v1/assignments?limit=1&offset=1").json()[0]["id"] == 2
    detail = client.get("/api/v1/assignments/1").json()
    assert detail["course_name"] == "Disciplina exemplo"
    assert detail["submission_state"] == "NEW"
    assert detail["description"] == "Enunciado para revisão"
    assert detail["attachments"][0]["title"] == "Referência"
    assert detail["solution_count"] == detail["artifact_count"] == 2
    assert detail["latest_solution_version"] == 2
    assert [s["version"] for s in detail["solutions"]] == [2, 1]
    assert "local_path" not in str(detail)


def test_solution_review_and_order(management):
    client, _, engine = management
    versions = client.get("/api/v1/assignments/1/solutions").json()
    assert [s["version"] for s in versions] == [2, 1]
    assert "deliverable" not in versions[0]
    detail = client.get("/api/v1/solutions/2").json()
    assert detail["review_kind"] == "deliverable"
    assert detail["deliverable"] == "Texto final"
    assert detail["answer"] is None
    assert "PRIVATE_REASONING" not in str(detail)
    with Session(engine) as session, session.begin():
        row = session.get(SolutionRecord, 2)
        row.response = {"assignment_types": ["LONG_FORM"], "answer": "INTERNAL_PREVIEW"}
    detail = client.get("/api/v1/solutions/2").json()
    assert detail["review_kind"] == "unavailable"
    assert detail["answer"] is None


@pytest.mark.parametrize(
    "types,extra,kind",
    [
        (
            ["SHORT_ANSWER"],
            {"answer": "42", "question_answers": [{"question_id": "1", "answer": "42"}]},
            "question_answer",
        ),
        (
            ["PROGRAMMING"],
            {"artifacts": [{"kind": "CODE", "title": "main.py", "specification": "print(42)"}]},
            "code",
        ),
    ],
)
def test_nonacademic_review(management, types, extra, kind):
    client, _, engine = management
    with Session(engine) as session, session.begin():
        row = session.get(SolutionRecord, 2)
        row.response = {"assignment_types": types, "understanding": "PRIVATE_REASONING", **extra}
    response = client.get("/api/v1/solutions/2")
    assert response.json()["review_kind"] == kind
    assert "PRIVATE_REASONING" not in response.text


@pytest.mark.parametrize(
    "route",
    [
        "assignments/999",
        "assignments/999/solutions",
        "solutions/999",
        "solutions/999/artifacts",
        "artifacts/999",
        "artifacts/999/content",
    ],
)
def test_missing_resources(management, route):
    response = management[0].get("/api/v1/" + route)
    assert response.status_code == 404
    assert response.json()["error"]["code"].endswith("_not_found")


@pytest.mark.parametrize(
    "route",
    [
        "assignments?limit=0",
        "assignments?offset=-1",
        "assignments?course_id=-1",
        "solutions/nope",
        "artifacts/0/content",
        "history?offset=-1",
    ],
)
def test_invalid_input(management, route):
    response = management[0].get("/api/v1/" + route)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_parameters"


def test_artifact_pdf_metadata_and_range(management):
    client, settings, _ = management
    assert len(client.get("/api/v1/solutions/2/artifacts").json()) == 1
    metadata = client.get("/api/v1/artifacts/2").json()
    assert metadata["solution_version"] == 2
    assert metadata["size_bytes"] > 0
    assert str(settings.generated_root) not in str(metadata)
    response = client.get("/api/v1/artifacts/2/content")
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("inline;")
    partial = client.get("/api/v1/artifacts/2/content", headers={"Range": "bytes=0-3"})
    assert partial.status_code == 206
    assert partial.content == b"%PDF"
    assert (
        client.get("/api/v1/artifacts/2/content", headers={"Range": "bytes=9999-"}).status_code
        == 416
    )


@pytest.mark.parametrize(
    "format,suffix,mime",
    [
        (
            "DOCX",
            ".docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        ("TEXT", ".txt", "text/plain"),
        ("MARKDOWN", ".md", "text/markdown"),
        ("HTML", ".html", "text/html"),
    ],
)
def test_artifact_formats(management, format, suffix, mime):
    client, settings, engine = management
    path = settings.generated_root / ("example" + suffix)
    path.write_bytes(b"example")
    with Session(engine) as session, session.begin():
        row = session.get(GeneratedArtifact, 2)
        row.local_path = str(path)
        row.artifact_type = format
    response = client.get("/api/v1/artifacts/2/content")
    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] == mime
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("attack", ["outside", "traversal", "missing", "token", "dotfile"])
def test_artifact_file_boundary(management, attack):
    client, settings, engine = management
    outside = settings.generated_root.parent / "private.txt"
    outside.write_text("PRIVATE")
    dotfile = settings.generated_root / ".env"
    dotfile.write_text("PRIVATE")
    paths = {
        "outside": outside,
        "traversal": settings.generated_root / ".." / "private.txt",
        "missing": settings.generated_root / "missing.pdf",
        "token": settings.token_file,
        "dotfile": dotfile,
    }
    with Session(engine) as session, session.begin():
        session.get(GeneratedArtifact, 2).local_path = str(paths[attack])
    response = client.get("/api/v1/artifacts/2/content")
    assert response.status_code == 404
    assert "PRIVATE" not in response.text


def test_client_path_is_never_used(management):
    client, settings, _ = management
    response = client.get("/api/v1/artifacts/2/content", params={"path": str(settings.token_file)})
    assert response.content.startswith(b"%PDF")
    assert client.get("/api/v1/artifacts/%2E%2E%2Ftoken.json/content").status_code in (404, 422)


def test_settings_and_all_json_are_secret_free(management):
    client, _, _ = management
    for route in [
        "health",
        "status",
        "history",
        "activities",
        "assignments",
        "assignments/1",
        "assignments/1/solutions",
        "solutions/2",
        "solutions/2/artifacts",
        "artifacts/2",
        "settings",
    ]:
        response = client.get("/api/v1/" + route)
        assert response.status_code == 200
        for secret in [
            "SUPER_SECRET_API_KEY_123",
            "SUPER_SECRET_REFRESH_TOKEN_456",
            "PRIVATE_REASONING",
            "INTERNAL_PREVIEW",
            "PRIVATE_PATH",
        ]:
            assert secret not in response.text
    settings = client.get("/api/v1/settings").json()
    assert settings["google_token_present"] is True
    assert settings["google_authentication_status"] == "not_verified"
    assert settings["turn_in_enabled"] is False
    assert settings["interval_hours"] == 8


def test_history_pagination_and_normal_status(management):
    client, _, engine = management
    assert client.get("/api/v1/history").json() == []
    for day in range(3):
        now = NOW + timedelta(days=day)
        cycle = start_cycle(engine, "manual", now)
        finish_cycle(engine, cycle, now, 2, None, safe_error="safe")
    all_rows = client.get("/api/v1/history").json()
    assert len(all_rows) == 3
    assert client.get("/api/v1/history?limit=1&offset=1").json() == all_rows[1:2]
    assert client.get("/api/v1/status").json()["last_cycle_result"] == "failed"


def test_empty_assignments_and_openapi(tmp_path):
    with TestClient(create_app(api_settings(tmp_path))) as client:
        assert client.get("/api/v1/assignments").json() == []
        spec = client.get("/openapi.json").json()
        assert "SolutionDetail" in spec["components"]["schemas"]
        assert "422" in spec["paths"]["/api/v1/assignments"]["get"]["responses"]
        assert (
            spec["components"]["schemas"]["SolutionDetail"]["properties"]["created_at"]["format"]
            == "date-time"
        )


def test_unexpected_errors_are_sanitized(management):
    client, settings, _ = management
    with patch(
        "app.management_api.open_database", side_effect=RuntimeError("SUPER_SECRET_API_KEY_123")
    ):
        with TestClient(create_app(settings), raise_server_exceptions=False) as safe_client:
            response = safe_client.get("/api/v1/assignments")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "SUPER_SECRET" not in response.text


@pytest.mark.parametrize("types", [[], ["UNKNOWN"]])
def test_unknown_classification_does_not_expose_answer(management, types):
    client, _, engine = management
    with Session(engine) as session, session.begin():
        session.get(SolutionRecord, 2).response = {
            "assignment_types": types,
            "answer": "INTERNAL_PREVIEW",
        }
    response = client.get("/api/v1/solutions/2")
    assert response.json()["review_kind"] == "unavailable"
    assert "INTERNAL_PREVIEW" not in response.text


def test_input_secrets_are_not_logged_or_echoed(management, caplog):
    client, _, _ = management
    with caplog.at_level("INFO", logger="classroom_agent.api"):
        response = client.get("/api/v1/solutions/SUPER_SECRET_API_KEY_123")
        client.get("/api/v1/SUPER_SECRET_REFRESH_TOKEN_456")
    assert response.status_code == 422
    assert "SUPER_SECRET" not in response.text
    records = [r.getMessage() for r in caplog.records if r.name == "classroom_agent.api"]
    assert all("SUPER_SECRET" not in message for message in records)


def test_symlink_outside_root_is_not_served(management):
    client, settings, engine = management
    link = settings.generated_root / "linked.pdf"
    try:
        link.symlink_to(settings.token_file)
    except OSError:
        pytest.skip("Creating symlinks requires Windows developer mode or privilege")
    with Session(engine) as session, session.begin():
        session.get(GeneratedArtifact, 2).local_path = str(link)
    assert client.get("/api/v1/artifacts/2/content").status_code == 404


def test_openapi_error_contract(management):
    spec = management[0].get("/openapi.json").json()
    for route in ("/api/v1/assignments", "/api/v1/history", "/api/v1/solutions/{solution_id}"):
        for status in ("404", "422", "500"):
            schema = spec["paths"][route]["get"]["responses"][status]["content"][
                "application/json"
            ]["schema"]
            assert schema["$ref"].endswith("/ErrorResponse")
    content = spec["paths"]["/api/v1/artifacts/{artifact_id}/content"]["get"]["responses"]
    assert "application/pdf" in content["200"]["content"]
    assert "206" in content and "416" in content


def test_latest_removed_submission_matches_core(management):
    from app.persistence.models import SubmissionRecord

    client, _, engine = management
    with Session(engine) as session, session.begin():
        session.get(SubmissionRecord, 1).present = False
    response = client.get("/api/v1/assignments/1").json()
    assert response["submission_state"] is None
    assert response["status"] == "UNKNOWN"
    assert response["eligible"] is False
