import re
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.context.builder import context_hash
from app.context.models import AssignmentContext
from app.context.safety import sanitize
from app.errors import AppError
from app.llm.errors import MESSAGES, ProviderFailure
from app.llm.prompt import build_prompt, build_system_prompt
from app.llm.providers import LLMProvider
from app.llm.schema import Solution
from app.persistence.models import SolutionRecord


def validate_solution(raw: str, context: AssignmentContext) -> Solution:
    if len(raw) > 200000:
        raise ValueError("Response too large")
    result = Solution.model_validate_json(raw, strict=True)

    # Refuse recognizable credentials/paths in generated fields, never persist raw failures.
    def inspect_text(value: object) -> None:
        if isinstance(value, str) and sanitize(value) != value:
            raise ValueError("Unsafe response text")
        if isinstance(value, dict):
            for child in value.values():
                inspect_text(child)
        if isinstance(value, list):
            for child in value:
                inspect_text(child)

    inspect_text(result.model_dump())
    unknown_sources = set(result.sources_used) - set(context.provenance)
    if unknown_sources:
        # Models sometimes paraphrase a provenance key. Never trust the paraphrase;
        # discard it and keep the structured answer reviewable with an explicit warning.
        result.sources_used = [s for s in result.sources_used if s in context.provenance]
        result.warnings.append("Fontes citadas pelo modelo não reconhecidas foram descartadas.")
    questions = context.questions + [q for f in context.forms for q in f.questions]
    expected = {q.id for q in questions}
    supplied = [q.question_id for q in result.question_answers]
    if len(supplied) != len(set(supplied)) or not set(supplied) <= expected:
        raise ValueError("Unknown or duplicate questions")
    if not result.requires_user_input and expected != set(supplied):
        raise ValueError("Missing answers")
    # Match the same template source as DocumentBuilder, which classifies from
    # the validated solution. Context classification can be UNKNOWN or disagree.
    task_types = set(result.assignment_types)
    academic_report = "PROGRAMMING" not in task_types and bool(
        task_types & {"LONG_FORM", "RESEARCH"}
    )
    if academic_report and not result.deliverable.strip():
        raise ValueError("Missing academic deliverable")
    if not academic_report and result.deliverable:
        raise ValueError("Unexpected deliverable for specialized template")
    return result


def solve_context(
    engine: Engine, context: AssignmentContext, provider: LLMProvider
) -> SolutionRecord:
    if not context.ready_for_ai:
        raise AppError("Solve bloqueado: " + "; ".join(context.missing_context))
    if not all(re.fullmatch(r"[A-Za-z0-9._-]{1,80}", v) for v in (provider.name, provider.model)):
        raise AppError("Identificação de provider inválida.")
    now = datetime.now(UTC)
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        with Session(bind=connection, expire_on_commit=False) as session:
            version = (
                session.scalar(
                    select(func.max(SolutionRecord.version)).where(
                        SolutionRecord.assignment_id == context.assignment_local_id
                    )
                )
                or 0
            ) + 1
            row = SolutionRecord(
                assignment_id=context.assignment_local_id,
                version=version,
                provider=provider.name,
                model=provider.model,
                status="GENERATING",
                context_hash=context_hash(context),
                created_at=now,
                updated_at=now,
                request_metadata={"schema_version": 6, "prompt_version": 3, "attempts": 0},
            )
            session.add(row)
            session.flush()
            row_id = row.id
        connection.commit()
    solution: Solution | None = None
    attempts = 0
    try:
        for attempt in range(2):
            attempts += 1
            system = build_system_prompt(context)
            if attempt:
                # Controlled regeneration: don't echo hostile invalid output or validation values.
                system += (
                    "\nA tentativa anterior falhou na validação. Gere novamente seguindo o schema."
                    " Use exatamente estas chaves obrigatórias: summary, assignment_types, "
                    "understanding, answer, deliverable, question_answers, artifacts, assumptions, "
                    "uncertainties, sources_used, requires_user_input, warnings. "
                    "Para uma atividade sem questões, use question_answers=[]; "
                    "sources_used=[] é aceitável; não invente IDs. Retorne somente JSON."
                )
                valid_questions = [q.id for q in context.questions] + [
                    q.id for form in context.forms for q in form.questions
                ]
                system += (
                    "\nREGRAS EXATAS DE REPARO: question_answers só pode usar estes IDs: "
                    + repr(valid_questions)
                    + ". Se a lista estiver vazia, use question_answers=[] e responda em answer. "
                    "sources_used só pode usar estas chaves de provenance: "
                    + repr(sorted(context.provenance))
                    + ". Não crie IDs, fontes ou perguntas alternativas."
                )
            raw = provider.generate(system, build_prompt(context), Solution.model_json_schema())
            try:
                solution = validate_solution(raw, context)
                break
            except (ValidationError, ValueError):
                if attempt:
                    raise ProviderFailure("VALIDATION") from None
        if solution is None:
            raise AppError("Nenhuma solução válida.")
    except Exception as exc:
        code = exc.code if isinstance(exc, ProviderFailure) else "UNKNOWN"
        message = MESSAGES.get(code, MESSAGES["UNKNOWN"])
        # Failure bodies and exception messages can contain keys or hostile content.
        with Session(engine) as session, session.begin():
            failed = session.get(SolutionRecord, row_id)
            assert failed is not None
            failed.status = "FAILED"
            failed.model = provider.model
            failed.error = message
            failed.updated_at = datetime.now(UTC)
            failed.request_metadata = {**failed.request_metadata, "attempts": attempts}
        raise AppError(f"Solução #{row_id} FAILED: {message}") from None
    with Session(engine, expire_on_commit=False) as session, session.begin():
        saved = session.get(SolutionRecord, row_id)
        assert saved is not None
        saved.response = solution.model_dump()
        saved.model = provider.model
        saved.answer = solution.answer
        saved.status = "NEEDS_REVIEW"  # No automatic approval, even for valid structured responses.
        saved.updated_at = datetime.now(UTC)
        saved.request_metadata = {**saved.request_metadata, "attempts": attempts}
        return saved


def list_solutions(engine: Engine, context: AssignmentContext) -> list[tuple[SolutionRecord, bool]]:
    current = context_hash(context)
    with Session(engine) as session:
        return [
            (r, r.context_hash != current)
            for r in session.scalars(
                select(SolutionRecord)
                .where(SolutionRecord.assignment_id == context.assignment_local_id)
                .order_by(SolutionRecord.version.desc())
            )
        ]
