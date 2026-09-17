from __future__ import annotations

import builtins
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.attachments.drive import DriveClient
from app.attachments.extraction import extract_isolated, preflight_zip
from app.attachments.materials import archive, digest
from app.attachments.safety import (
    DOCX,
    PPTX,
    XLSX,
    AttachmentError,
    AttachmentStore,
    check_name,
    choose_format,
)
from app.config import Settings
from app.errors import AppError
from app.persistence.models import AssignmentRecord, AttachmentRecord, CourseRecord


class AttachmentService:
    def __init__(self, engine: Engine, settings: Settings) -> None:
        self.engine, self.settings = engine, settings
        self.store = AttachmentStore(settings.attachments_root, settings.attachment_max_bytes)

    def assignment(self, session: Session, assignment_id: int) -> AssignmentRecord:
        row = session.get(AssignmentRecord, assignment_id)
        if row is None:
            raise AppError("Atividade local não encontrada. Execute sync e local-assignments.")
        return row

    def list(self, assignment_id: int) -> list[dict[str, Any]]:
        with Session(self.engine) as session:
            assignment = self.assignment(session, assignment_id)
            output = []
            for row in session.scalars(
                select(AttachmentRecord)
                .where(AttachmentRecord.assignment_id == assignment_id)
                .order_by(AttachmentRecord.id)
            ):
                path = None
                if row.local_path:
                    try:
                        path = str(
                            self.store.path(row.local_path, assignment.course_id, assignment_id)
                        )
                    except AttachmentError:
                        path = "Caminho bloqueado"
                output.append(
                    dict(
                        id=row.id,
                        title=row.title,
                        kind=row.kind,
                        mime_type=row.mime_type,
                        present=row.present,
                        url=row.url,
                        drive_id=row.drive_id,
                        status=row.status,
                        error=row.error,
                        error_code=row.error_code,
                        path=path,
                        characters=(row.extracted or {}).get("characters"),
                        pages=(row.extracted or {}).get("pages"),
                    )
                )
            return output

    def _ids(self, assignment_id: int) -> builtins.list[int]:
        with Session(self.engine) as session:
            assignment = self.assignment(session, assignment_id)
            course = session.get(CourseRecord, assignment.course_id)
            if not assignment.present or course is None or not course.present:
                raise AppError("Atividade/disciplina ausente no último sync; histórico preservado.")
            return list(
                session.scalars(
                    select(AttachmentRecord.id)
                    .where(
                        AttachmentRecord.assignment_id == assignment_id,
                        AttachmentRecord.present.is_(True),
                    )
                    .order_by(AttachmentRecord.id)
                )
            )

    def fetch(self, assignment_id: int, drive: DriveClient) -> builtins.list[dict[str, Any]]:
        for attachment_id in self._ids(assignment_id):
            # Serialize with sync so a stale fetch cannot resurrect a removed attachment.
            with Session(self.engine) as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                row = session.get(AttachmentRecord, attachment_id)
                assert row is not None
                if not row.present:
                    session.rollback()
                    continue
                assignment = self.assignment(session, assignment_id)
                try:
                    self._fetch_one(row, assignment, drive)
                except AttachmentError as exc:
                    self._failure(row, exc)
                except Exception:
                    self._failure(
                        row,
                        AttachmentError(
                            "FETCH_FAILED", "Falha ao obter arquivo; repita o comando."
                        ),
                    )
                session.commit()
        return self.list(assignment_id)

    def _fetch_one(
        self, row: AttachmentRecord, assignment: AssignmentRecord, drive: DriveClient
    ) -> None:
        if row.kind != "DRIVE_FILE" or not row.drive_id:
            raise AttachmentError(
                "METADATA_ONLY", "Material disponível somente como metadata.", unsupported=True
            )
        check_name(row.title)
        meta = drive.metadata(row.drive_id)
        now = datetime.now(UTC)
        previous_meta = row.drive_metadata
        if meta != previous_meta:
            archive(row, now)
            row.extracted = None
            row.status = "DISCOVERED"
        row.drive_metadata = meta
        row.mime_type = meta["mimeType"]
        mime, _ = choose_format(meta["mimeType"], meta["name"])
        if meta.get("trashed"):
            raise AttachmentError("NOT_FOUND", "Arquivo removido no Drive.")
        if not meta.get("canDownload"):
            raise AttachmentError("FORBIDDEN", "Download não autorizado pelo proprietário.")
        if (
            digest(meta) == row.materialized_fingerprint
            and meta.get("version")
            and row.local_path
            and row.content_hash
            and row.materialized_mime == mime
        ):
            try:
                self.store.read(
                    row.local_path, assignment.course_id, assignment.id, row.content_hash
                )
                row.status = "EXTRACTED" if row.extracted else "DOWNLOADED"
                row.error = row.error_code = None
                row.fetched_at = now
                return
            except AttachmentError:
                pass
        data, mime = drive.download(meta)
        if mime in {DOCX, PPTX, XLSX}:
            preflight_zip(data, self.settings.extraction_max_bytes)
        # Check metadata again: do not publish bytes if the document changed during transfer.
        after = drive.metadata(row.drive_id)
        if meta != after:
            raise AttachmentError("REMOTE_CHANGED", "Arquivo mudou durante download; repita.")
        relative, content_hash = self.store.save(
            assignment.course_id, assignment.id, row.id, meta["name"], mime, data
        )
        if row.content_hash != content_hash or row.local_path != relative:
            archive(row, now)
            row.extracted = None
            row.extracted_at = None
        row.local_path, row.content_hash, row.materialized_mime = relative, content_hash, mime
        row.materialized_fingerprint = digest(meta)
        row.status = "EXTRACTED" if row.extracted else "DOWNLOADED"
        row.error = row.error_code = None
        row.fetched_at = row.updated_at = now

    @staticmethod
    def _failure(row: AttachmentRecord, exc: AttachmentError) -> None:
        row.status = "UNSUPPORTED" if exc.unsupported else "FAILED"
        row.error_code, row.error = exc.code, str(exc)
        row.extracted = None
        row.updated_at = datetime.now(UTC)

    def extract(self, assignment_id: int) -> builtins.list[dict[str, Any]]:
        for attachment_id in self._ids(assignment_id):
            with Session(self.engine) as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                row = session.get(AttachmentRecord, attachment_id)
                assert row is not None
                if not row.present:
                    session.rollback()
                    continue
                assignment = self.assignment(session, assignment_id)
                try:
                    if row.status not in {"DOWNLOADED", "EXTRACTED"}:
                        # Never extract stale bytes after a permission/metadata failure.
                        if row.status == "DISCOVERED":
                            row.error_code = "NOT_DOWNLOADED"
                            row.error = "Execute fetch-attachments antes de extract."
                            session.commit()
                        else:
                            session.rollback()
                        continue
                    if not row.local_path or not row.content_hash or not row.materialized_mime:
                        raise AttachmentError(
                            "NOT_DOWNLOADED", "Execute fetch-attachments primeiro."
                        )
                    data = self.store.read(
                        row.local_path, assignment.course_id, assignment_id, row.content_hash
                    )
                    if row.extracted is None:
                        result = extract_isolated(data, row.materialized_mime, self.settings)
                        row.status, row.error_code = result.status, result.error_code
                        row.extracted = (
                            dict(result.to_dict(), content_hash=row.content_hash)
                            if result.status == "EXTRACTED"
                            else None
                        )
                        row.error = (
                            "Extração não concluída: " + str(result.error_code)
                            if result.error_code
                            else None
                        )
                        row.extracted_at = row.updated_at = datetime.now(UTC)
                except AttachmentError as exc:
                    self._failure(row, exc)
                except Exception:
                    self._failure(row, AttachmentError("EXTRACTION_FAILED", "Falha na extração."))
                session.commit()
        return self.list(assignment_id)
