from pathlib import Path
from unittest.mock import patch

import pytest
from docx import Document
from pypdf import PdfReader
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from app.cli.commands import app
from app.documents.engine import DocumentBuilder, _deliverable_content, _deliverable_name
from app.errors import AppError
from app.llm.providers import MockProvider
from app.llm.service import solve_context
from app.persistence.models import GeneratedArtifact, SolutionRecord
from tests.test_phase4 import context_for  # noqa: F401
from tests.test_phase4 import phase4 as phase4

FORMAT_FIXTURE = """# Infraestrutura de TI

A **infraestrutura de TI** inclui *redes* e o termo `servidor Linux`.

## Principais componentes

- Servidores
- Redes
- Armazenamento

1. Planejamento
2. Implantação

```python
if 2 < 3:
    print("Olá, mundo")
```

| Componente | Função |
| --- | --- |
| Rede | Comunicação |
"""


def prepared_builder(phase4):
    settings, engine, _ = phase4
    solution = solve_context(engine, context_for(phase4), MockProvider())
    with Session(engine) as session, session.begin():
        row = session.get(SolutionRecord, solution.id)
        assert row is not None
        row.response = {
            "assignment_types": ["LONG_FORM"],
            "understanding": "",
            "answer": FORMAT_FIXTURE,
            "question_answers": [],
            "artifacts": [],
        }
    configured = settings.model_copy(
        update={"generated_root": settings.database_file.parent / "format-output"}
    )
    return configured, engine, DocumentBuilder(engine, configured)


def test_requested_html_is_materialized_alongside_pdf(phase4):
    settings, engine, _ = phase4
    solution = solve_context(engine, context_for(phase4), MockProvider())
    with Session(engine) as session, session.begin():
        row = session.get(SolutionRecord, solution.id)
        assert row and row.response
        response = dict(row.response)
        response["assignment_types"] = ["PROGRAMMING"]
        response["artifacts"] = [
            {
                "kind": "CODE",
                "title": "index.html",
                "specification": "<!doctype html>\n<html><body>OK</body></html>",
            }
        ]
        row.response = response

    artifacts = DocumentBuilder(
        engine,
        settings.model_copy(update={"generated_root": settings.database_file.parent / "out"}),
    ).build_all(1)

    assert [artifact.artifact_type for artifact in artifacts] == ["PDF", "HTML"]
    html = Path(artifacts[1].local_path)
    assert html.name == "index.html"
    assert html.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert all(artifact.status == "UPLOAD_PENDING" for artifact in artifacts)


def test_deliverable_rejects_paths_executables_and_nul():
    assert _deliverable_name("index.html") == "index.html"
    assert _deliverable_name("../index.html") is None
    assert _deliverable_name("run.exe") is None
    assert _deliverable_name("token.json") is None
    assert _deliverable_content("```html\n<p>OK</p>\n```") == "<p>OK</p>\n"
    assert _deliverable_content("bad\x00data") is None


def test_pdf_format_renders_markdown_structure_without_tokens(phase4):
    _, _, builder = prepared_builder(phase4)
    artifact = builder.build(1, override="academic-report", output_format="pdf")

    extracted = "\n".join(
        page.extract_text() or "" for page in PdfReader(artifact.local_path).pages
    )
    assert "Infraestrutura de TI" in extracted
    assert "Principais componentes" in extracted
    assert "infraestrutura de TI" in extracted
    assert "servidor Linux" in extracted and 'print("Olá, mundo")' in extracted
    assert "2 < 3" in extracted
    assert "Servidores" in extracted and "Planejamento" in extracted
    assert "Componente" in extracted and "Comunicação" in extracted
    assert "**" not in extracted and "```" not in extracted
    assert not any(line.lstrip().startswith("#") for line in extracted.splitlines())


def test_docx_is_valid_and_preserves_heading_emphasis_lists_and_code(phase4):
    _, _, builder = prepared_builder(phase4)
    artifact = builder.build(1, override="academic-report", output_format="docx")

    document = Document(artifact.local_path)
    paragraphs = document.paragraphs
    assert any(p.text == "Infraestrutura de TI" and p.style.name == "Heading 1" for p in paragraphs)
    assert any(
        p.text == "Principais componentes" and p.style.name == "Heading 2" for p in paragraphs
    )
    assert any(
        run.text == "infraestrutura de TI" and run.bold for p in paragraphs for run in p.runs
    )
    assert any(run.text == "redes" and run.italic for p in paragraphs for run in p.runs)
    assert any(p.text == "Servidores" and p.style.name == "List Bullet" for p in paragraphs)
    assert any(p.text == "1. Planejamento" or p.text == "Planejamento" for p in paragraphs)
    assert any(p.style.name == "List Number" for p in paragraphs)
    assert any('print("Olá, mundo")' in p.text and "2 < 3" in p.text for p in paragraphs)
    assert len(document.tables) == 1
    assert document.tables[0].cell(0, 0).text == "Componente"
    assert document.tables[0].cell(1, 1).text == "Comunicação"
    assert any(
        run.text == "servidor Linux" and run.font.name == "Consolas"
        for paragraph in paragraphs
        for run in paragraph.runs
    )
    assert all("**" not in p.text and "```" not in p.text for p in paragraphs)


def test_txt_is_clean_and_markdown_tokens_are_removed(phase4):
    _, _, builder = prepared_builder(phase4)
    artifact = builder.build(1, override="academic-report", output_format="txt")
    text = Path(artifact.local_path).read_text(encoding="utf-8")

    assert "Infraestrutura de TI" in text and "Principais componentes" in text
    assert "infraestrutura de TI" in text and "redes" in text and "servidor Linux" in text
    assert "Servidores" in text and "1. Planejamento" in text
    assert 'print("Olá, mundo")' in text and "2 < 3" in text and "Comunicação" in text
    assert "**" not in text and "```" not in text and "`" not in text
    assert not any(line.lstrip().startswith("#") for line in text.splitlines())


def test_md_preserves_markdown_syntax(phase4):
    _, _, builder = prepared_builder(phase4)
    artifact = builder.build(1, override="academic-report", output_format="md")
    text = Path(artifact.local_path).read_text(encoding="utf-8")

    assert "# Infraestrutura de TI" in text
    assert "**infraestrutura de TI**" in text
    assert "*redes*" in text and "`servidor Linux`" in text
    assert "## Principais componentes" in text
    assert "- Servidores" in text and "1. Planejamento" in text
    assert '```python\nif 2 < 3:\n    print("Olá, mundo")\n```' in text
    assert "| Componente | Função |" in text


def test_code_assignment_removes_outer_fence_without_changing_code(phase4):
    _, engine, builder = prepared_builder(phase4)
    with Session(engine) as session, session.begin():
        row = session.query(SolutionRecord).one()
        row.response = {
            "assignment_types": ["PROGRAMMING"],
            "understanding": "",
            "answer": "Código de exemplo.",
            "question_answers": [],
            "artifacts": [
                {
                    "kind": "CODE",
                    "title": "main.py",
                    "specification": "```python\ndef main():\n    print('ok')\n```",
                }
            ],
        }
    artifact = builder.build(1, override="code-assignment", output_format="txt")
    text = Path(artifact.local_path).read_text(encoding="utf-8")

    assert "def main():" in text and "    print('ok')" in text
    assert "```" not in text


@pytest.mark.parametrize("invalid", ["xyz", "../../arquivo", "/tmp/test.pdf", "foo/bar.docx"])
def test_format_rejects_unknown_values_and_paths(phase4, invalid):
    _, _, builder = prepared_builder(phase4)
    with pytest.raises(AppError, match="pdf, docx, txt ou md"):
        builder.build(1, output_format=invalid)


def test_cli_template_and_format_are_independent_and_legacy_defaults_to_pdf(phase4):
    settings, engine, _ = phase4
    solution = solve_context(engine, context_for(phase4), MockProvider())
    with Session(engine) as session, session.begin():
        row = session.get(SolutionRecord, solution.id)
        assert row is not None
        row.response = {
            "assignment_types": ["LONG_FORM"],
            "understanding": "",
            "answer": FORMAT_FIXTURE,
            "question_answers": [],
            "artifacts": [],
        }
    settings = settings.model_copy(
        update={"generated_root": settings.database_file.parent / "format-cli"}
    )
    with patch("app.cli.commands.load_settings", return_value=settings):
        result = CliRunner().invoke(
            app,
            ["generate", "1", "--template", "academic-report", "--format", "docx"],
        )
    assert result.exit_code == 0, result.output
    assert "DOCX:" in result.output
    with Session(engine) as session:
        artifact = session.query(GeneratedArtifact).filter_by(artifact_type="DOCX").one()
        assert artifact.template == "ACADEMIC_REPORT"
        assert Document(artifact.local_path).paragraphs[0].style.name == "Title"


def test_generate_help_and_invalid_format_are_clear(phase4):
    settings, _, _ = phase4
    runner = CliRunner()
    help_result = runner.invoke(app, ["generate", "--help"])
    assert help_result.exit_code == 0
    flattened_help = " ".join(help_result.output.split())
    assert "--format" in flattened_help
    assert all(value in flattened_help for value in ("pdf", "docx", "txt", "md"))
    with patch("app.cli.commands.load_settings", return_value=settings):
        result = runner.invoke(app, ["generate", "1", "--format", "../../escape"])
    assert result.exit_code == 1
    assert "Formato inválido" in result.output
    assert "pdf, docx, txt ou md" in result.output
