import hashlib
import json
import re

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.attachments.materials import digest as material_digest
from app.attachments.safety import AttachmentError, AttachmentStore
from app.config import Settings
from app.context.forms import FormsReader, form_identity
from app.context.models import (
    AssignmentContext,
    AttachmentContext,
    FormContext,
    LinkContext,
    Question,
    SourceText,
    SourceType,
    SubmissionContext,
    TaskType,
)
from app.context.safety import URL_PATTERN, public_url, sanitize
from app.errors import AppError
from app.persistence.models import (
    AssignmentRecord,
    AttachmentRecord,
    CourseRecord,
    SubmissionRecord,
    SyncRun,
)


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()


def context_hash(context: AssignmentContext) -> str:
    # No wall clock, OAuth state, sync timestamps, or provider settings in semantic hash.
    return digest(context.model_dump(exclude={"warnings"}))


def sufficient(text: str) -> bool:
    """Conservative evidence, never a guarantee of semantic completeness."""
    text = URL_PATTERN.sub("", text).strip()
    # Routing-only descriptions don't contain the actual exercise.
    if re.fullmatch(
        r"(?is)(?:responda|acesse|preencha|veja|clique|faça|consulte|answer|open)\s+"
        r"(?:o |a |ao |no |the )?(?:formulário|forms?|link|anexo|atividade|questionário)"
        r"(?:\s+(?:abaixo|acima|anexo|a seguir|disponível|pelo link|below))*[\s.!:;-]*",
        text,
    ):
        return False
    return bool(
        re.search(
            r"(?i)\b(?:explique|compare|descreva|calcule|resolva|implemente|escreva|elabore|"
            r"analise|demonstre|justifique|defina|pesquise|crie|construa|construir|"
            r"realizar|realize|produza|"
            r"investigue|pesquisa|explain|compare|calculate|implement|write|describe|solve)"
            r"\b\s+\S.{2,}|\S.{4,}\?",
            text,
        )
    )


class Budget:
    def __init__(self, maximum: int) -> None:
        self.remaining = maximum
        self.warnings: list[str] = []
        self.provenance: dict[str, str] = {}
        self.hashes: dict[str, str] = {}

    def text(self, value: object, source: str, limit: int = 16000) -> SourceText:
        clean = sanitize(value if isinstance(value, str) else "")
        self.hashes[source] = digest(clean)
        self.provenance[source] = "UNTRUSTED_DATA: " + source
        length = min(limit, self.remaining)
        if len(clean) > length:
            self.warnings.append("TRUNCATED: " + source)
        result = clean[:length]
        self.remaining -= len(result)
        return SourceText(text=result, source=source)


def build_assignment_context(
    engine: Engine,
    assignment_id: int,
    settings: Settings,
    forms_reader: FormsReader | None = None,
    *,
    read_forms: bool = True,
) -> AssignmentContext:
    budget = Budget(settings.context_max_chars)
    warnings = budget.warnings
    store = AttachmentStore(settings.attachments_root, settings.attachment_max_bytes)
    reader = forms_reader or FormsReader(settings)
    with Session(engine) as session:
        assignment = session.get(AssignmentRecord, assignment_id)
        if assignment is None or not assignment.present:
            raise AppError(
                "Atividade local indisponível. Consulte local-assignments e execute sync."
            )
        course = session.get(CourseRecord, assignment.course_id)
        if course is None or not course.present:
            raise AppError("Disciplina local indisponível.")
        snap = assignment.snapshot
        name = budget.text(course.snapshot.get("name"), "course.name", 512)
        title = budget.text(snap.get("title"), "assignment.title", 2000)
        description = budget.text(snap.get("description"), "assignment.description")
        evidence: list[str] = []
        types: list[TaskType] = ["UNKNOWN"]
        work_type = snap.get("work_type")
        if work_type in {"MULTIPLE_CHOICE_QUESTION", "SHORT_ANSWER_QUESTION"}:
            types = [
                "MULTIPLE_CHOICE" if work_type == "MULTIPLE_CHOICE_QUESTION" else "SHORT_ANSWER"
            ]
            evidence.append("CourseWork.workType=" + str(work_type))
        sources: list[SourceType] = []
        if URL_PATTERN.sub("", description.text).strip():
            sources.append("DESCRIPTION_TASK")
        links: list[LinkContext] = []
        forms: list[FormContext] = []
        seen: set[str] = set()

        def link(value: object, origin: str, form_material: bool = False) -> None:
            url = public_url(value)
            if url is None:
                if form_material:
                    forms.append(
                        FormContext(
                            url=None,
                            title=budget.text("", origin),
                            reason="Material Forms sem URL; execute sync ou forneça perguntas.",
                        )
                    )
                return
            if url in seen:
                budget.provenance[origin] = "UNTRUSTED_DATA: link duplicado " + url
                return
            if len(links) >= 50:
                warnings.append("TRUNCATED: links (máximo 50)")
                return
            seen.add(url)
            links.append(LinkContext(url=url, source=origin))
            budget.provenance[origin] = "UNTRUSTED_DATA: " + origin
            is_form, identifier = form_identity(url)
            if is_form or form_material:
                form = FormContext(
                    url=url,
                    form_id=identifier,
                    title=SourceText(text="Google Forms", source=origin),
                )
                if len(forms) < 10:
                    if read_forms:
                        form = reader.read(form)
                    else:
                        form.reason = (
                            "Leitura da API desativada por --offline; perguntas não coletadas."
                        )
                    form.title = budget.text(form.title.text, origin + ":title", 2000)
                    if form.description:
                        form.description = budget.text(
                            form.description.text, origin + ":description"
                        )
                    for question in form.questions:
                        question.title = budget.text(
                            question.title.text, question.title.source, 8000
                        )
                        question.options = [
                            budget.text(o.text, o.source, 2000) for o in question.options
                        ]
                    forms.append(form)
                else:
                    warnings.append("TRUNCATED: forms (máximo 10)")

        # Discover links from the full description before deterministic text truncation.
        full_description = str(snap.get("description") or "")
        for index, match in enumerate(URL_PATTERN.finditer(full_description)):
            link(match[0].rstrip(".,;!?)"), f"assignment.description:link:{index}")
        rows = list(
            session.scalars(
                select(AttachmentRecord)
                .where(
                    AttachmentRecord.assignment_id == assignment_id,
                    AttachmentRecord.present.is_(True),
                )
                .order_by(AttachmentRecord.id)
            )
        )
        # Hash all relevant source metadata, including anything outside presentation limits.
        budget.hashes["materials"] = digest(
            [
                [
                    r.id,
                    r.kind,
                    sanitize(r.title),
                    public_url(r.url),
                    r.status,
                    r.content_hash,
                    r.fingerprint,
                ]
                for r in rows
            ]
        )
        for row in rows[:100]:
            link(row.url, f"material:{row.id}:url", row.kind == "FORM")
        if len(rows) > 100:
            warnings.append("TRUNCATED: materials (máximo 100)")
        attachments: list[AttachmentContext] = []
        for row in rows[:100]:
            if row.kind in {"FORM", "LINK", "YOUTUBE"}:
                continue
            origin = f"attachment:{row.id}"
            status = row.status
            content = None
            if status == "EXTRACTED" and row.extracted:
                try:
                    if (
                        not row.local_path
                        or not row.content_hash
                        or row.extracted.get("content_hash") != row.content_hash
                        or row.materialized_fingerprint != material_digest(row.drive_metadata)
                    ):
                        raise AttachmentError("STALE", "Extração desatualizada")
                    store.read(row.local_path, course.id, assignment_id, row.content_hash)
                    sections = row.extracted.get("sections", [])
                    if not isinstance(sections, list) or any(
                        not isinstance(p, dict) or not isinstance(p.get("text"), str)
                        for p in sections
                    ):
                        raise AttachmentError("INVALID", "Extração inválida")
                    content = budget.text("\n".join(p["text"] for p in sections), origin + ":text")
                except (AttachmentError, OSError):
                    status = "FAILED"
                    warnings.append(
                        origin + ": extração indisponível/desatualizada; execute fetch/extract."
                    )
            attachments.append(
                AttachmentContext(
                    local_id=row.id,
                    kind=row.kind,
                    title=budget.text(row.title, origin + ":title", 512),
                    status=status,
                    content=content,
                )
            )
        questions: list[Question] = []
        if work_type in {"MULTIPLE_CHOICE_QUESTION", "SHORT_ANSWER_QUESTION"}:
            choices = assignment.raw_payload.get("multipleChoiceQuestion", {}).get("choices", [])
            if not isinstance(choices, list):
                choices = []
            questions.append(
                Question(
                    id="coursework",
                    title=title,
                    task_type=types[0],
                    options=[
                        budget.text(c, f"assignment.choice:{i}", 2000)
                        for i, c in enumerate(choices[:100])
                    ],
                )
            )
            if len(choices) > 100:
                warnings.append("TRUNCATED: assignment choices")
        if forms:
            sources.append("FORM_TASK")
        if attachments or any(not form_identity(link_.url)[0] for link_ in links):
            sources.append("MATERIAL_TASK")
        form_types = {q.task_type for f in forms for q in f.questions} - {"UNKNOWN"}
        if types == ["UNKNOWN"] and form_types:
            types = sorted(form_types)
            evidence.append(
                "Tipos comprovados por questões da Forms API; outros materiais podem diferir."
            )
        readiness: list[str] = []
        if sufficient(description.text):
            readiness.append(
                "description contém enunciado candidato; suficiência semântica exige revisão."
            )
        if (
            questions
            and title.text.strip()
            and (types == ["SHORT_ANSWER"] or bool(questions[0].options))
        ):
            readiness.append("CourseWork contém questão estruturada.")
        if forms and all(f.status == "QUESTIONS_AVAILABLE" and f.questions for f in forms):
            readiness.append("Perguntas obtidas pela Forms API.")
        if any(a.content and sufficient(a.content.text) for a in attachments):
            readiness.append("Texto extraído contém enunciado candidato.")
        missing: list[str] = []
        for form in forms:
            if form.status != "QUESTIONS_AVAILABLE":
                missing.append("Forms: " + form.reason)
        if not readiness:
            missing.append(
                "Enunciado insuficiente/incerto. Forneça as perguntas ou instruções completas."
            )
        if any(w.startswith("TRUNCATED:") for w in warnings):
            # Safe failure: never silently solve a truncated instruction set.
            readiness = []
            missing.append(
                "Contexto truncado; reduza material ou ajuste CONTEXT_MAX_CHARS antes de resolver."
            )
        if not evidence:
            evidence.append("Sem evidência estruturada suficiente: UNKNOWN.")
        submissions: list[SubmissionContext] = []
        for sub in session.scalars(
            select(SubmissionRecord)
            .where(
                SubmissionRecord.assignment_id == assignment_id, SubmissionRecord.present.is_(True)
            )
            .order_by(SubmissionRecord.id)
        ):
            s = sub.snapshot
            submissions.append(
                SubmissionContext(
                    state=sanitize(str(s.get("state", "UNKNOWN"))),
                    assigned_grade=s.get("assigned_grade"),
                    draft_grade=s.get("draft_grade"),
                    late=s.get("late"),
                )
            )
        latest = session.scalar(select(SyncRun).order_by(SyncRun.id.desc()))
        if latest is None or latest.status != "SUCCESS":
            warnings.append("Snapshot local incompleto/incerto; execute sync.")
        warnings.append(
            "Revisão humana obrigatória; ready_for_ai não garante suficiência semântica."
        )
        for field in ("due_at", "state", "max_points", "submissions", "work_type"):
            budget.provenance[field] = "Classroom snapshot local: " + field
        return AssignmentContext(
            assignment_local_id=assignment_id,
            course_local_id=course.id,
            course=name,
            assignment=title,
            description=description,
            due_at=snap.get("due_at"),
            state=sanitize(str(snap.get("state", "UNKNOWN"))),
            max_points=snap.get("max_points"),
            submissions=submissions,
            source_types=sources,
            assignment_types=types,
            classification_evidence=evidence,
            links=links,
            forms=forms,
            questions=questions,
            attachments=attachments,
            ready_for_ai=bool(readiness),
            missing_context=missing,
            readiness_evidence=readiness,
            provenance=budget.provenance,
            source_hashes=budget.hashes,
            warnings=list(dict.fromkeys(warnings)),
        )
