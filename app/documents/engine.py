from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    Spacer,
)
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError
from app.persistence.models import AssignmentRecord, GeneratedArtifact, SolutionRecord


class Template(StrEnum):
    QUESTION_ANSWER = "question-answer"
    ACADEMIC_REPORT = "academic-report"
    CODE_ASSIGNMENT = "code-assignment"


def _safe(value: str, fallback: str = "document") -> str:
    value = re.sub(r"[<>:\\|?*\x00-\x1f]", " ", value or "")
    value = re.sub(r"[\s./]+", " ", value).strip(" .")
    if not value or value.upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
    }:
        return fallback
    return value[:120]


def _esc(value: Any) -> str:
    from xml.sax.saxutils import escape

    return escape(str(value or "")).replace("\n", "<br/>")


def _markdown_blocks(value: Any, styles: Any) -> list[Any]:
    """Convert the model's lightweight Markdown into readable PDF flowables."""
    from xml.sax.saxutils import escape

    text = str(value or "").replace("```html", "```").replace("```HTML", "```")
    blocks: list[Any] = []
    code: list[str] = []
    in_code = False
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            clean = " ".join(paragraph).strip()
            clean = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"<b>\1</b>", clean)
            clean = re.sub(r"`([^`]+)`", r"<font name='Courier'>\1</font>", clean)
            blocks.append(
                Paragraph(
                    escape(clean).replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>"),
                    styles["Body"],
                )
            )
            paragraph.clear()

    for raw in text.splitlines():
        line = raw.rstrip()
        if line.strip().startswith("```"):
            if in_code:
                blocks.append(Preformatted("\n".join(code), styles["CodeBlock"]))
                code.clear()
            else:
                flush_paragraph()
            in_code = not in_code
            continue
        if in_code:
            code.append(line)
            continue
        if not line.strip():
            flush_paragraph()
            continue
        heading = re.match(r"^#{1,3}\s+(.+)$", line)
        if heading:
            flush_paragraph()
            blocks.append(Paragraph(_esc(heading.group(1)), styles["Section"]))
            continue
        bullet = re.match(r"^\s*[-*]\s+(.+)$", line)
        if bullet:
            flush_paragraph()
            blocks.append(Paragraph("• " + _esc(bullet.group(1)), styles["Body"]))
            continue
        if line.strip().startswith(">"):
            line = line.strip()[1:].strip()
        paragraph.append(line)
    if in_code:
        blocks.append(Preformatted("\n".join(code), styles["CodeBlock"]))
    else:
        flush_paragraph()
    return blocks


class DocumentBuilder:
    def __init__(self, engine: Engine, settings: Settings) -> None:
        self.engine, self.settings = engine, settings

    def choose_template(self, solution: dict[str, Any], override: str | None = None) -> Template:
        if override:
            try:
                return Template(override)
            except ValueError as exc:
                raise AppError(
                    "Template inválido; use question-answer, academic-report ou code-assignment."
                ) from exc
        types = set(solution.get("assignment_types", []))
        if "PROGRAMMING" in types:
            return Template.CODE_ASSIGNMENT
        if types & {"LONG_FORM", "RESEARCH"}:
            return Template.ACADEMIC_REPORT
        return Template.QUESTION_ANSWER

    def _assignment(self, assignment_id: int) -> tuple[AssignmentRecord, dict[str, Any]]:
        with Session(self.engine) as s:
            row = s.get(AssignmentRecord, assignment_id)
            if row is None or not row.present:
                raise AppError("Atividade local não encontrada.")
            course = s.get(
                __import__("app.persistence.models", fromlist=["CourseRecord"]).CourseRecord,
                row.course_id,
            )
            return row, (course.snapshot if course else {})

    def build(
        self, assignment_id: int, solution_version: int | None = None, override: str | None = None
    ) -> GeneratedArtifact:
        assignment, course = self._assignment(assignment_id)
        with Session(self.engine) as s:
            q = select(SolutionRecord).where(
                SolutionRecord.assignment_id == assignment_id,
                SolutionRecord.status.in_(("READY", "NEEDS_REVIEW", "APPROVED")),
            )
            if solution_version is not None:
                q = q.where(SolutionRecord.version == solution_version)
            solution_row = s.scalars(q.order_by(SolutionRecord.version.desc())).first()
            if solution_row is None:
                raise AppError("Nenhuma solução READY/NEEDS_REVIEW disponível para gerar PDF.")
            solution = solution_row.response or {
                "answer": solution_row.answer or "",
                "assignment_types": ["UNKNOWN"],
                "question_answers": [],
            }
            template = self.choose_template(solution, override)
            current = select(func.max(GeneratedArtifact.version)).where(
                GeneratedArtifact.assignment_id == assignment_id
            )
            version = (s.scalar(current) or 0) + 1
            title = _safe(assignment.snapshot.get("title", "Atividade"), "Atividade")
            subject = _safe(course.get("name", self.settings.course_name), "Disciplina")
            folder = (
                self.settings.generated_root.resolve()
                / _safe(str(course.get("id", assignment.course_id)))
                / str(assignment_id)
            )
            folder.mkdir(parents=True, exist_ok=True)
            base = f"{title} - {subject}"
            filename = _safe(base, "atividade") + (".pdf" if version == 1 else f" - v{version}.pdf")
            path = folder / filename
            while path.exists():
                version += 1
                path = folder / (_safe(base, "atividade") + f" - v{version}.pdf")
            artifact = GeneratedArtifact(
                solution_id=solution_row.id,
                assignment_id=assignment_id,
                artifact_type="PDF",
                template=template.name,
                version=version,
                local_path=str(path),
                content_hash="0" * 64,
                status="GENERATING",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            s.add(artifact)
            s.commit()
            s.refresh(artifact)
        try:
            self._render(path, template, solution, assignment.snapshot, course)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            with Session(self.engine) as s:
                saved = s.get(GeneratedArtifact, artifact.id)
                assert saved
                saved.content_hash, saved.status, saved.updated_at = (
                    digest,
                    "UPLOAD_PENDING",
                    datetime.now(UTC),
                )
                s.commit()
                s.refresh(saved)
                return saved
        except Exception as exc:
            with Session(self.engine) as s:
                saved = s.get(GeneratedArtifact, artifact.id)
                assert saved
                saved.status, saved.error, saved.updated_at = (
                    "FAILED",
                    "Falha ao renderizar PDF",
                    datetime.now(UTC),
                )
                s.commit()
            raise AppError("Falha segura ao gerar PDF.") from exc

    def _render(
        self,
        path: Path,
        template: Template,
        solution: dict[str, Any],
        assignment: dict[str, Any],
        course: dict[str, Any],
    ) -> None:
        styles = getSampleStyleSheet()
        styles.add(
            ParagraphStyle(
                name="AcademicTitle",
                parent=styles["Title"],
                alignment=TA_CENTER,
                fontSize=16,
                leading=20,
                spaceAfter=14,
            )
        )
        styles.add(
            ParagraphStyle(
                name="Section",
                parent=styles["Heading2"],
                fontSize=13,
                leading=16,
                spaceBefore=10,
                spaceAfter=6,
            )
        )
        styles.add(
            ParagraphStyle(
                name="Body", parent=styles["BodyText"], fontSize=11, leading=16, spaceAfter=7
            )
        )
        styles.add(
            ParagraphStyle(
                name="Meta", parent=styles["BodyText"], fontSize=10, leading=14, alignment=TA_CENTER
            )
        )
        styles.add(
            ParagraphStyle(
                name="Question",
                parent=styles["BodyText"],
                fontSize=11,
                leading=15,
                spaceBefore=8,
                spaceAfter=3,
            )
        )
        styles.add(
            ParagraphStyle(
                name="CodeBlock",
                fontName="Courier",
                fontSize=8.5,
                leading=10.5,
                leftIndent=10,
                rightIndent=10,
                spaceBefore=6,
                spaceAfter=8,
            )
        )
        doc = SimpleDocTemplate(
            str(path),
            pagesize=A4,
            rightMargin=2 * cm,
            leftMargin=2 * cm,
            topMargin=1.8 * cm,
            bottomMargin=1.8 * cm,
            title=str(assignment.get("title", "Atividade")),
            author=self.settings.student_name or "Classroom Agent",
        )
        story: list[Any] = []
        title = _esc(assignment.get("title", "ATIVIDADE"))
        if template == Template.CODE_ASSIGNMENT:
            for value in (
                self.settings.institution,
                self.settings.unit,
                self.settings.course_name,
                f"Discente: {self.settings.student_name}" if self.settings.student_name else "",
            ):
                if value:
                    story.append(Paragraph(_esc(value), styles["Meta"]))
            story += [Spacer(1, 14), Paragraph(title, styles["AcademicTitle"])]
        else:
            story += [
                Paragraph(title, styles["AcademicTitle"]),
                Paragraph(
                    _esc(f"Data: {datetime.now(self.settings.zone):%d/%m/%Y}"), styles["Meta"]
                ),
            ]
            for label, value in (
                ("Aluno", self.settings.student_name),
                ("Matéria", course.get("name", self.settings.course_name)),
            ):
                if value:
                    story.append(Paragraph(_esc(f"{label}: {value}"), styles["Meta"]))
        if template == Template.QUESTION_ANSWER:
            items = solution.get("question_answers", [])
            if items:
                for i, item in enumerate(items, 1):
                    story += [
                        Paragraph(
                            f"<b>{i}. {_esc(item.get('question_id', 'Questão'))}</b>",
                            styles["Question"],
                        ),
                        *_markdown_blocks(item.get("answer", ""), styles),
                    ]
            elif solution.get("answer"):
                story.extend(_markdown_blocks(solution["answer"], styles))
        else:
            if solution.get("understanding"):
                story.extend(_markdown_blocks(solution["understanding"], styles))
            story.append(Paragraph("Resposta", styles["Section"]))
            if solution.get("answer"):
                story.extend(_markdown_blocks(solution["answer"], styles))
            for i, item in enumerate(solution.get("question_answers", []), 1):
                story += [
                    Paragraph(
                        f"{i}. {_esc(item.get('question_id', 'Questão'))}", styles["Section"]
                    ),
                    *_markdown_blocks(item.get("answer", ""), styles),
                ]
            for spec in solution.get("artifacts", []):
                if spec.get("kind") == "CODE" or "code" in spec.get("title", "").lower():
                    story += [
                        Paragraph(_esc(spec.get("title", "Código")), styles["Section"]),
                        Preformatted(
                            str(spec.get("specification", "")),
                            styles["CodeBlock"],
                        ),
                    ]
        doc.build(story, onFirstPage=self._footer, onLaterPages=self._footer)

    @staticmethod
    def _footer(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 9)
        canvas.drawCentredString(A4[0] / 2, 1 * cm, f"Página {doc.page}")
        canvas.restoreState()
