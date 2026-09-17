"""Official Forms GET only; reuse Phase 3 drive.readonly. No respondent-page fetches."""

import json
import re
from typing import Any
from urllib.parse import urlsplit

import requests
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import AuthorizedSession

from app.auth.google import authenticate
from app.config import Settings
from app.context.models import FormContext, Question, SourceText, TaskType
from app.context.safety import sanitize
from app.errors import AppError


def form_identity(url: str) -> tuple[bool, str | None]:
    parts = urlsplit(url)
    if parts.hostname == "forms.gle":
        return True, None  # No redirect resolution / scraping.
    if parts.hostname != "docs.google.com":
        return False, None
    match = re.fullmatch(r"/forms/(?:u/\d+/)?d/(e/)?([A-Za-z0-9_-]+)(?:/.*)?", parts.path)
    if not match:
        return False, None
    return True, None if match[1] else match[2]


class FormsReader:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def read(self, form: FormContext) -> FormContext:
        if form.form_id is None:
            return form.model_copy(
                update={
                    "reason": "URL pública/encurtada sem formId da API; entrada manual necessária."
                }
            )
        try:
            credentials = authenticate(self.settings)
            # The API supports the already-required drive.readonly scope.
            with AuthorizedSession(credentials) as http:  # type: ignore[no-untyped-call]
                with http.get(
                    "https://forms.googleapis.com/v1/forms/" + form.form_id,
                    timeout=self.settings.http_timeout_seconds,
                    allow_redirects=False,
                    stream=True,
                ) as response:
                    if response.status_code != 200:
                        reasons = {
                            401: "Autenticação indisponível; execute auth.",
                            403: "Sem acesso de leitura ou Forms API desativada no projeto OAuth.",
                            404: "Formulário não encontrado ou não acessível à conta.",
                            429: "Limite temporário da API; tente mais tarde.",
                        }
                        return form.model_copy(
                            update={
                                "reason": reasons.get(
                                    response.status_code,
                                    "API de Forms indisponível; tente mais tarde.",
                                )
                            }
                        )
                    chunks = bytearray()
                    for chunk in response.iter_content(65536):
                        chunks.extend(chunk)
                        if len(chunks) > 2_000_000:
                            return form.model_copy(
                                update={"reason": "Forms excede limite de 2 MB."}
                            )
                    payload = json.loads(chunks)
            return parse_form(form, payload)
        except (AppError, GoogleAuthError, requests.RequestException, ValueError, TypeError):
            return form.model_copy(
                update={
                    "reason": "Leitura indisponível. Confira auth/Forms API; entrada manual futura."
                }
            )


def parse_form(form: FormContext, payload: Any) -> FormContext:
    if not isinstance(payload, dict) or payload.get("formId") != form.form_id:
        raise ValueError("Invalid form response")
    info = payload.get("info", {})
    questions: list[Question] = []
    partial = False
    items = payload.get("items", [])
    if not isinstance(items, list):
        raise ValueError("Invalid items")

    def source(value: object, name: str) -> SourceText:
        nonlocal partial
        text = value if isinstance(value, str) else ""
        if len(text) > 8000:
            partial = True
        return SourceText(text=sanitize(text[:8000]), source=f"forms:{form.form_id}:{name}")

    for item in items[:200]:
        if not isinstance(item, dict):
            raise ValueError("Invalid item")
        entry = item.get("questionItem", {})
        q = entry.get("question") if isinstance(entry, dict) else None
        if not isinstance(q, dict):
            # Grids/media/sections need richer semantic handling, never claim completeness.
            partial = True
            continue
        identifier = q.get("questionId")
        if not isinstance(identifier, str) or not re.fullmatch(r"[\w-]{1,256}", identifier):
            raise ValueError("Invalid question id")
        task: TaskType = "UNKNOWN"
        options: list[SourceText] = []
        if "choiceQuestion" in q:
            task = "MULTIPLE_CHOICE"
            choices = q["choiceQuestion"].get("options", [])
            if len(choices) > 100:
                partial = True
            options = [
                source(o.get("value"), identifier + f":option:{i}")
                for i, o in enumerate(choices[:100])
            ]
            if q["choiceQuestion"].get("type") not in {"RADIO", "CHECKBOX", "DROP_DOWN"}:
                partial = True
            if any(
                o.get("isOther") or o.get("goToSectionId") or o.get("goToAction") or o.get("image")
                for o in choices
            ):
                partial = True
        elif "textQuestion" in q:
            task = "LONG_FORM" if q["textQuestion"].get("paragraph") else "SHORT_ANSWER"
        else:
            partial = True
        if entry.get("image") or item.get("imageItem"):
            partial = True
        title = source(item.get("title", ""), identifier)
        if item.get("description"):
            title = source(title.text + "\n" + str(item["description"]), identifier)
        if not title.text.strip():
            partial = True
        questions.append(
            Question(
                id=identifier,
                title=title,
                task_type=task,
                required=q.get("required", False),
                options=options,
            )
        )
    partial = partial or len(items) > 200
    result = form.model_copy(
        update={
            "title": source(info.get("title", ""), "title"),
            "description": source(info.get("description", ""), "description"),
            "questions": questions,
        }
    )
    result.status = (
        ("PARTIAL_QUESTIONS" if partial else "QUESTIONS_AVAILABLE")
        if questions
        else ("FORM_DETECTED_BUT_QUESTIONS_UNAVAILABLE")
    )
    result.reason = (
        "Conteúdo parcial/não suportado/truncado; revisão e entrada manual necessárias."
        if partial
        else ""
        if questions
        else "API não retornou perguntas utilizáveis."
    )
    return result
