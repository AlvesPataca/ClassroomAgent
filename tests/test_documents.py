from pathlib import Path

from sqlalchemy.orm import Session

from app.documents.engine import DocumentBuilder, _deliverable_content, _deliverable_name
from app.llm.providers import MockProvider
from app.llm.service import solve_context
from app.persistence.models import SolutionRecord
from tests.test_phase4 import context_for  # noqa: F401
from tests.test_phase4 import phase4 as phase4


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
