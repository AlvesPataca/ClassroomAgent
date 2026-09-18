from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from googleapiclient.errors import HttpError
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError
from app.persistence.models import GeneratedArtifact


class DriveUploader:
    def __init__(self, engine: Engine, settings: Settings, service: Any) -> None:
        self.engine, self.settings, self.service = engine, settings, service

    def upload(self, artifact_id: int) -> GeneratedArtifact:
        with Session(self.engine) as s:
            artifact = s.get(GeneratedArtifact, artifact_id)
            if artifact is None:
                raise AppError("Artifact local não encontrado.")
            if artifact.status == "UPLOADED" and artifact.drive_file_id:
                return artifact
            path = Path(artifact.local_path).resolve()
            root = self.settings.generated_root.resolve()
            if root not in path.parents or path.suffix.lower() != ".pdf" or not path.is_file():
                raise AppError("Caminho do artifact recusado por segurança.")
            if path.name.lower() in {".env", "token.json", "credentials.json", "classroom.db"}:
                raise AppError("Arquivo sensível recusado.")
            folder_id = self.settings.drive_responses_folder_id
            if folder_id and (len(folder_id) < 10 or any(c in folder_id for c in " /\\")):
                raise AppError("DRIVE_RESPONSES_FOLDER_ID inválido.")
            try:
                parent = folder_id or self._create_folder(self.settings.drive_responses_folder_name)
                if self.settings.drive_organize_by_course:
                    parent = self._create_folder(self._course_name(artifact.assignment_id), parent)
                media = __import__(
                    "googleapiclient.http", fromlist=["MediaFileUpload"]
                ).MediaFileUpload(str(path), mimetype="application/pdf", resumable=True)
                result = (
                    self.service.files()
                    .create(
                        body={
                            "name": path.name,
                            "parents": [parent],
                            "mimeType": "application/pdf",
                        },
                        media_body=media,
                        fields="id,parents,webViewLink",
                    )
                    .execute()
                )
                (
                    artifact.status,
                    artifact.drive_file_id,
                    artifact.drive_folder_id,
                    artifact.drive_web_view_link,
                    artifact.uploaded_at,
                    artifact.updated_at,
                ) = (
                    "UPLOADED",
                    result.get("id"),
                    parent,
                    result.get("webViewLink"),
                    datetime.now(UTC),
                    datetime.now(UTC),
                )
                s.commit()
                s.refresh(artifact)
                return artifact
            except HttpError as exc:
                status = getattr(exc.resp, "status", "desconhecido")
                safe_error = f"Drive recusou a operação (HTTP {status})."
                artifact.status, artifact.error, artifact.updated_at = (
                    "FAILED",
                    safe_error,
                    datetime.now(UTC),
                )
                s.commit()
                raise AppError(
                    safe_error
                    + " Confira DRIVE_RESPONSES_FOLDER_ID, Drive API habilitada e permissões."
                ) from None
            except Exception as exc:
                artifact.status, artifact.error, artifact.updated_at = (
                    "FAILED",
                    "Falha de conexão/operação no Drive",
                    datetime.now(UTC),
                )
                s.commit()
                raise AppError("Falha de conexão/operação no Drive; tente novamente.") from exc

    def _create_folder(self, name: str, parent: str | None = None) -> str:
        query = (
            "name = '" + name.replace("'", "\\'") + "' and "
            "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )
        if parent:
            query += " and '" + parent + "' in parents"
        found = (
            self.service.files()
            .list(q=query, spaces="drive", fields="files(id)", pageSize=1)
            .execute()
            .get("files", [])
        )
        if found:
            return str(found[0]["id"])
        body: dict[str, Any] = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
        if parent:
            body["parents"] = [parent]
        result = self.service.files().create(body=body, fields="id").execute()
        return str(result["id"])

    def _course_name(self, assignment_id: int) -> str:
        from app.persistence.models import AssignmentRecord, CourseRecord

        with Session(self.engine) as s:
            a = s.get(AssignmentRecord, assignment_id)
            c = s.get(CourseRecord, a.course_id) if a else None
            return (c.snapshot.get("name") if c else None) or self.settings.course_name
