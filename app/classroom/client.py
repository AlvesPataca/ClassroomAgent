import json
import random
import time
from collections.abc import Callable, Iterator
from typing import Any, TypeVar

import httplib2
from google.auth.exceptions import GoogleAuthError
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from pydantic import ValidationError

from app.classroom.mapper import (
    map_assignment,
    map_course,
    map_course_resource,
    map_submission,
    map_topic,
)
from app.config import Settings
from app.domain.models import Assignment, Course, CourseResource, Submission, Topic
from app.errors import AppError

T = TypeVar("T")
TRANSIENT = {429, 500, 502, 503, 504}


def forbidden_message(content: bytes) -> str:
    """Classify structured reasons without displaying provider text or metadata."""
    fallback = "Acesso negado (HTTP 403): confira permissões e política da escola."
    try:
        payload = json.loads(content)
        error = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(error, dict):
            return fallback
        reasons: set[str] = set()
        for key in ("details", "errors"):
            entries = error.get(key, [])
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict) and isinstance(entry.get("reason"), str):
                        reasons.add(entry["reason"])
        if reasons & {"SERVICE_DISABLED", "accessNotConfigured"}:
            return (
                "Google Classroom API desativada no projeto das credenciais OAuth "
                "(SERVICE_DISABLED). No Google Cloud Console, selecione o projeto do cliente "
                "Desktop e ative Google Classroom API em APIs e serviços > Biblioteca. "
                "Aguarde alguns minutos e repita o comando; não precisa apagar token.json."
            )
        if reasons & {"ACCESS_TOKEN_SCOPE_INSUFFICIENT", "insufficientPermissions"}:
            return (
                "Permissões OAuth insuficientes (HTTP 403). "
                "Confira os scopes com python main.py auth --debug."
            )
        return fallback
    except (ValueError, TypeError):
        return fallback


class ClassroomClient:
    def __init__(self, service: Any, settings: Settings) -> None:
        self._service = service
        self.settings = settings

    @classmethod
    def from_credentials(cls, credentials: Any, settings: Settings) -> "ClassroomClient":
        http = AuthorizedHttp(
            credentials, http=httplib2.Http(timeout=settings.http_timeout_seconds)
        )
        service = build("classroom", "v1", http=http, cache_discovery=False, static_discovery=True)
        return cls(service, settings)

    def _execute(self, request: Any) -> dict[str, Any]:
        for attempt in range(self.settings.max_retries + 1):
            try:
                result = request.execute(num_retries=0)
                if not isinstance(result, dict):
                    raise AppError("Classroom retornou uma resposta inválida.")
                return result
            except HttpError as exc:
                code = int(exc.resp.status)
                if code not in TRANSIENT or attempt == self.settings.max_retries:
                    messages = {
                        401: "Sessão expirada. Execute auth novamente.",
                        403: forbidden_message(exc.content),
                        404: "Curso ou atividade removido ou indisponível.",
                        429: "Limite da API atingido. Tente novamente mais tarde.",
                    }
                    raise AppError(
                        messages.get(code, f"Classroom indisponível (HTTP {code}).")
                    ) from exc
            except (OSError, httplib2.HttpLib2Error) as exc:
                if attempt == self.settings.max_retries:
                    raise AppError("Falha de conexão ou timeout ao consultar Classroom.") from exc
            except GoogleAuthError as exc:
                raise AppError("Token inválido ou revogado. Execute auth novamente.") from exc
            time.sleep(min(2**attempt, 32) + random.uniform(0, 1))
        raise AssertionError("Unreachable")

    def _pages(self, resource: Any, key: str, **params: Any) -> Iterator[dict[str, Any]]:
        seen: set[str] = set()
        while True:
            payload = self._execute(resource.list(pageSize=100, **params))
            items = payload.get(key, [])
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise AppError("Lista inválida recebida do Classroom.")
            yield from items
            token = payload.get("nextPageToken")
            if not token:
                break
            if not isinstance(token, str) or token in seen:
                raise AppError("Token de paginação inválido ou repetido no Classroom.")
            seen.add(token)
            params["pageToken"] = token

    def _mapped(
        self, rows: Iterator[dict[str, Any]], mapper: Callable[[dict[str, Any]], T]
    ) -> list[T]:
        try:
            return [mapper(row) for row in rows]
        except (ValueError, KeyError, TypeError, ValidationError) as exc:
            raise AppError(
                "Dados inválidos recebidos do Classroom; consulta interrompida."
            ) from exc

    def list_courses(self, *, include_archived: bool = False) -> list[Course]:
        states = ["ACTIVE", "ARCHIVED"] if include_archived else ["ACTIVE"]
        return self._mapped(
            self._pages(self._service.courses(), "courses", studentId="me", courseStates=states),
            map_course,
        )

    def list_assignments(self, course_id: str) -> list[Assignment]:
        return self._mapped(
            self._pages(
                self._service.courses().courseWork(),
                "courseWork",
                courseId=course_id,
                courseWorkStates=["PUBLISHED"],
            ),
            lambda raw: map_assignment(raw, self.settings.timezone),
        )

    def list_submissions(self, course_id: str) -> list[Submission]:
        return self._mapped(
            self._pages(
                self._service.courses().courseWork().studentSubmissions(),
                "studentSubmissions",
                courseId=course_id,
                courseWorkId="-",
                userId="me",
            ),
            map_submission,
        )

    def list_course_resources(self, course_id: str) -> list[CourseResource]:
        materials = self._mapped(
            self._pages(
                self._service.courses().courseWorkMaterials(),
                "courseWorkMaterial",
                courseId=course_id,
                courseWorkMaterialStates=["PUBLISHED"],
            ),
            lambda raw: map_course_resource(raw, "COURSE_MATERIAL"),
        )
        announcements = self._mapped(
            self._pages(
                self._service.courses().announcements(),
                "announcements",
                courseId=course_id,
                announcementStates=["PUBLISHED"],
            ),
            lambda raw: map_course_resource(raw, "ANNOUNCEMENT"),
        )
        return [*materials, *announcements]

    def list_topics(self, course_id: str) -> list[Topic]:
        return self._mapped(
            self._pages(self._service.courses().topics(), "topic", courseId=course_id),
            lambda raw: map_topic(raw, course_id),
        )

    def submission_material_ids(
        self, course_id: str, coursework_id: str, submission_id: str
    ) -> set[str]:
        """Read the student's draft attachments without turning the work in."""
        payload = self._execute(
            self._service.courses()
            .courseWork()
            .studentSubmissions()
            .get(courseId=course_id, courseWorkId=coursework_id, id=submission_id)
        )
        materials = payload.get("assignmentSubmission", {}).get("attachments", [])
        return {
            str(item["driveFile"]["id"])
            for item in materials
            if isinstance(item, dict)
            and isinstance(item.get("driveFile"), dict)
            and item["driveFile"].get("id")
        }

    def attach_drive_files(
        self,
        course_id: str,
        coursework_id: str,
        submission_id: str,
        drive_file_ids: list[str],
    ) -> None:
        """Attach Drive files to the draft submission; never calls turnIn."""
        if not drive_file_ids:
            return
        self._execute(
            self._service.courses()
            .courseWork()
            .studentSubmissions()
            .modifyAttachments(
                courseId=course_id,
                courseWorkId=coursework_id,
                id=submission_id,
                body={"addAttachments": [{"driveFile": {"id": value}} for value in drive_file_ids]},
            )
        )
