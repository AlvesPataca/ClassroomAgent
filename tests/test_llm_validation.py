import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.context.models import Question, SourceText
from app.errors import AppError
from app.llm.service import SolutionValidationError, solve_context, validate_solution
from app.persistence.models import GeneratedArtifact, SolutionRecord
from tests.test_phase4 import context_for
from tests.test_phase4 import phase4 as phase4


def solution_data(**updates):
    result = {
        "summary": "Relatório de infraestrutura para uma clínica.",
        "assignment_types": ["RESEARCH"],
        "understanding": "Interpretação interna do cenário.",
        "answer": "Prévia curta do relatório.",
        "deliverable": "# Infraestrutura da clínica\n\nA solução separa redes e serviços.",
        "question_answers": [],
        "artifacts": [],
        "assumptions": [],
        "uncertainties": [],
        "sources_used": [],
        "requires_user_input": False,
        "warnings": [],
    }
    result.update(updates)
    return result


class ScriptedProvider:
    name = "fake"
    model = "fake-v1"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def generate(self, system, data, schema):
        self.calls.append((system, data, schema))
        assert "deliverable" in schema["required"]
        assert schema["properties"]["deliverable"]["type"] == "string"
        return self.responses.pop(0)


def test_academic_report_with_deliverable_passes_even_if_context_is_unknown(phase4):
    context = context_for(phase4)
    assert context.assignment_types == ["UNKNOWN"]

    result = validate_solution(json.dumps(solution_data()), context)

    assert result.assignment_types == ["RESEARCH"]
    assert result.deliverable.startswith("# Infraestrutura")


def test_missing_and_empty_deliverable_have_distinct_reasons(phase4):
    context = context_for(phase4)
    missing = solution_data()
    del missing["deliverable"]

    with pytest.raises(SolutionValidationError) as missing_error:
        validate_solution(json.dumps(missing), context)
    with pytest.raises(SolutionValidationError) as empty_error:
        validate_solution(json.dumps(solution_data(deliverable="")), context)

    assert missing_error.value.reason == "missing_deliverable"
    assert empty_error.value.reason == "empty_deliverable"


def test_repair_uses_academic_template_and_preserves_candidate_content(phase4):
    settings, engine, _ = phase4
    context = context_for(phase4)
    first = solution_data()
    del first["deliverable"]
    final = solution_data(deliverable="# Clínica\n\nTexto acadêmico revisado.")
    provider = ScriptedProvider([json.dumps(first), json.dumps(final)])

    saved = solve_context(engine, context, provider)

    assert saved.status == "NEEDS_REVIEW"
    assert saved.request_metadata["validation_reasons"] == ["missing_deliverable"]
    assert saved.request_metadata["repair_attempted"] is True
    assert saved.request_metadata["repair_valid"] is True
    repair_system, repair_data, _ = provider.calls[1]
    assert "FORMATO DE TRABALHO ACADÊMICO" in repair_system
    assert "deliverable deve ser uma string não vazia" in repair_system
    assert "preserve e mova o trabalho final" in repair_system
    payload = json.loads(repair_data)
    assert payload["repair_candidate"]["answer"] == first["answer"]
    assert "interna do cenário" in payload["repair_candidate"]["understanding"]
    assert settings.database_file.exists()


def test_repair_still_missing_deliverable_fails_with_specific_reason_and_no_artifact(phase4):
    _, engine, _ = phase4
    context = context_for(phase4)
    first = solution_data()
    del first["deliverable"]
    second = solution_data(deliverable="")
    provider = ScriptedProvider([json.dumps(first), json.dumps(second)])

    with pytest.raises(AppError, match="empty_deliverable"):
        solve_context(engine, context, provider)
    with Session(engine) as session:
        failed = session.scalar(select(SolutionRecord))
        assert failed is not None
        assert failed.status == "FAILED"
        assert failed.error == "Resposta inválida após reparo: empty_deliverable."
        assert failed.request_metadata["validation_reasons"] == [
            "missing_deliverable",
            "empty_deliverable",
        ]
        assert failed.request_metadata["repair_valid"] is False
        assert session.scalar(select(func.count()).select_from(GeneratedArtifact)) == 0


def test_invalid_json_is_repaired_and_logged_without_content(phase4, caplog):
    _, engine, _ = phase4
    context = context_for(phase4)
    provider = ScriptedProvider(["{bad json", json.dumps(solution_data())])

    with caplog.at_level("WARNING", logger="app.llm.service"):
        saved = solve_context(engine, context, provider)

    assert saved.status == "NEEDS_REVIEW"
    assert saved.request_metadata["validation_reasons"] == ["invalid_json"]
    assert "validation_reason=invalid_json" in caplog.text
    assert "deliverable_length=0" in caplog.text
    assert "Infraestrutura da clínica" not in caplog.text
    assert "prompt" not in caplog.text.lower()


def test_sources_and_questions_get_specific_reasons(phase4):
    context = context_for(phase4)
    with pytest.raises(SolutionValidationError) as source_error:
        validate_solution(json.dumps(solution_data(sources_used=["not-in-provenance"])), context)
    assert source_error.value.reason == "invalid_sources"

    question = Question(
        id="official-question",
        title=SourceText(text="Questão oficial", source="forms.question", trust="UNTRUSTED_DATA"),
    )
    question_context = context.model_copy(
        update={
            "assignment_types": ["SHORT_ANSWER"],
            "questions": [question],
            "provenance": {"forms.question": "UNTRUSTED_DATA"},
        }
    )
    invalid_answers = solution_data(
        assignment_types=["SHORT_ANSWER"],
        deliverable="",
        question_answers=[{"question_id": "invented-question", "answer": "Resposta."}],
    )
    with pytest.raises(SolutionValidationError) as question_error:
        validate_solution(json.dumps(invalid_answers), question_context)
    assert question_error.value.reason == "questions_inconsistent"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("answer", "", "empty_answer"),
        ("understanding", "", "empty_understanding"),
        ("answer", None, "missing_answer"),
        ("understanding", None, "missing_understanding"),
        ("summary", None, "missing_summary"),
    ],
)
def test_required_fields_have_safe_specific_reasons(phase4, field, value, reason):
    data = solution_data()
    if value is None:
        del data[field]
    else:
        data[field] = value

    with pytest.raises(SolutionValidationError) as error:
        validate_solution(json.dumps(data), context_for(phase4))
    assert error.value.reason == reason


def test_invalid_schema_and_classification_template_get_safe_reasons(phase4):
    context = context_for(phase4)
    with pytest.raises(SolutionValidationError) as schema_error:
        validate_solution(json.dumps(solution_data(summary=17)), context)
    assert schema_error.value.reason == "invalid_schema"

    with pytest.raises(SolutionValidationError) as classification_error:
        validate_solution(
            json.dumps(solution_data(assignment_types=["SHORT_ANSWER"], deliverable="texto")),
            context,
        )
    assert classification_error.value.reason == "classification_template_inconsistent"


def test_requires_user_input_does_not_make_academic_deliverable_optional(phase4):
    data = solution_data(deliverable="", requires_user_input=True)

    with pytest.raises(SolutionValidationError) as error:
        validate_solution(json.dumps(data), context_for(phase4))

    assert error.value.reason == "empty_deliverable"
