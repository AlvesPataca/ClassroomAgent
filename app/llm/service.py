import json
import logging
import re
from datetime import UTC, datetime
from typing import Any, get_args

from pydantic import ValidationError
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.context.builder import context_hash
from app.context.models import AssignmentContext, TaskType
from app.context.safety import sanitize
from app.errors import AppError
from app.llm.errors import MESSAGES, ProviderFailure
from app.llm.prompt import (
    build_prompt,
    build_repair_system_prompt,
    build_system_prompt,
    solution_template,
)
from app.llm.providers import LLMProvider
from app.llm.schema import Solution
from app.persistence.models import SolutionRecord

logger = logging.getLogger(__name__)
SOLUTION_SCHEMA_VERSION = 6
SOLUTION_PROMPT_VERSION = 4
_TASK_TYPES = frozenset(get_args(TaskType))


class SolutionValidationError(ValueError):
    """A validation failure represented only by a safe, stable reason code."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _validation_reason(exc: ValidationError) -> str:
    errors = exc.errors(include_input=False)
    if any(item.get("type") == "json_invalid" for item in errors):
        return "invalid_json"
    for field, missing, empty in (
        ("deliverable", "missing_deliverable", "empty_deliverable"),
        ("answer", "missing_answer", "empty_answer"),
        ("understanding", "missing_understanding", "empty_understanding"),
    ):
        matches = [item for item in errors if item.get("loc", (None,))[0] == field]
        if matches:
            if any(item.get("type") == "missing" for item in matches):
                return missing
            if any(item.get("type") == "string_too_short" for item in matches):
                return empty
            return "invalid_schema"
    for item in errors:
        location = item.get("loc", ())
        field_name = location[0] if location else None
        if field_name == "sources_used":
            return "invalid_sources"
        if field_name == "question_answers":
            return "questions_inconsistent"
        if field_name == "assignment_types":
            return (
                "missing_assignment_types"
                if item.get("type") == "missing"
                else ("classification_template_inconsistent")
            )
        if (
            item.get("type") == "missing"
            and isinstance(field_name, str)
            and field_name in Solution.model_fields
        ):
            return f"missing_{field_name}"
    return "invalid_schema"


def _candidate_types(candidate: dict[str, Any] | None) -> list[str] | None:
    if candidate is None:
        return None
    values = candidate.get("assignment_types")
    if (
        isinstance(values, list)
        and values
        and all(isinstance(value, str) and value in _TASK_TYPES for value in values)
    ):
        return values
    return None


def _safe_repair_candidate(raw: str) -> dict[str, Any] | None:
    """Keep only bounded, parsed, schema-shaped and sanitized prior model content."""
    if len(raw) > 200_000:
        return None
    try:
        candidate = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(candidate, dict) or not set(candidate) <= set(Solution.model_fields):
        return None

    def clean(value: object) -> bool:
        if isinstance(value, str):
            return sanitize(value) == value
        if isinstance(value, list):
            return all(clean(item) for item in value)
        if isinstance(value, dict):
            return all(isinstance(key, str) and clean(item) for key, item in value.items())
        return value is None or isinstance(value, (bool, int, float))

    if not clean(candidate):
        return None
    for key, allowed in (
        ("question_answers", {"question_id", "answer"}),
        ("artifacts", {"kind", "title", "specification"}),
    ):
        values = candidate.get(key, [])
        if not isinstance(values, list) or any(
            not isinstance(item, dict) or not set(item) <= allowed for item in values
        ):
            return None
    return candidate


def _validation_fields(raw: str, context: AssignmentContext) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    types = value.get("assignment_types")
    if (
        isinstance(types, list)
        and types
        and all(isinstance(item, str) and item in _TASK_TYPES for item in types)
    ):
        template = solution_template(types).replace("-", "_").upper()
    elif context.assignment_types and set(context.assignment_types) != {"UNKNOWN"}:
        template = solution_template(list(context.assignment_types)).replace("-", "_").upper()
    else:
        template = "UNKNOWN"
    answer = value.get("answer")
    deliverable = value.get("deliverable")
    return {
        "template": template,
        "answer_present": isinstance(answer, str) and bool(answer.strip()),
        "answer_length": len(answer) if isinstance(answer, str) else 0,
        "deliverable_present": isinstance(deliverable, str) and bool(deliverable.strip()),
        "deliverable_length": len(deliverable) if isinstance(deliverable, str) else 0,
    }


def validate_solution(raw: str, context: AssignmentContext) -> Solution:
    if len(raw) > 200000:
        raise SolutionValidationError("response_too_large")
    try:
        result = Solution.model_validate_json(raw, strict=True)
    except ValidationError as exc:
        raise SolutionValidationError(_validation_reason(exc)) from None

    # Refuse recognizable credentials/paths in generated fields, never persist raw failures.
    def inspect_text(value: object) -> None:
        if isinstance(value, str) and sanitize(value) != value:
            raise SolutionValidationError("unsafe_response_content")
        if isinstance(value, dict):
            for child in value.values():
                inspect_text(child)
        if isinstance(value, list):
            for child in value:
                inspect_text(child)

    inspect_text(result.model_dump())
    unknown_sources = set(result.sources_used) - set(context.provenance)
    if unknown_sources:
        raise SolutionValidationError("invalid_sources")
    questions = context.questions + [q for f in context.forms for q in f.questions]
    expected = {q.id for q in questions}
    supplied = [q.question_id for q in result.question_answers]
    if len(supplied) != len(set(supplied)) or not set(supplied) <= expected:
        raise SolutionValidationError("questions_inconsistent")
    if not result.requires_user_input and expected != set(supplied):
        raise SolutionValidationError("questions_inconsistent")
    # Match the same template source as DocumentBuilder, which classifies from
    # the validated solution. Context classification can be UNKNOWN or disagree.
    task_types = set(result.assignment_types)
    academic_report = "PROGRAMMING" not in task_types and bool(
        task_types & {"LONG_FORM", "RESEARCH"}
    )
    if academic_report and not result.deliverable.strip():
        raise SolutionValidationError("empty_deliverable")
    if not academic_report and result.deliverable:
        raise SolutionValidationError("classification_template_inconsistent")
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
                request_metadata={
                    "schema_version": SOLUTION_SCHEMA_VERSION,
                    "prompt_version": SOLUTION_PROMPT_VERSION,
                    "attempts": 0,
                },
            )
            session.add(row)
            session.flush()
            row_id = row.id
        connection.commit()
    solution: Solution | None = None
    attempts = 0
    validation_reasons: list[str] = []
    repair_candidate: dict[str, Any] | None = None
    repair_attempted = False
    repair_status = "not_attempted"
    repair_valid: bool | None = None
    try:
        for attempt in range(2):
            attempts += 1
            if attempt:
                repair_attempted = True
                repair_status = "running"
                candidate_types = _candidate_types(repair_candidate)
                system = build_repair_system_prompt(
                    context, validation_reasons[-1], candidate_types
                )
            else:
                system = build_system_prompt(context)
            data = build_prompt(context, repair_candidate if attempt else None)
            raw = provider.generate(system, data, Solution.model_json_schema())
            try:
                solution = validate_solution(raw, context)
                if attempt:
                    repair_status = "succeeded"
                    repair_valid = True
                stats = _validation_fields(raw, context)
                logger.info(
                    "event=solution_validation validation_status=valid solution_version=%d "
                    "template=%s schema_version=%d validation_reason=none blocking=false "
                    "answer_present=%s answer_length=%d deliverable_present=%s "
                    "deliverable_length=%d repair_attempted=%s repair_status=%s repair_valid=%s",
                    version,
                    stats["template"],
                    SOLUTION_SCHEMA_VERSION,
                    stats["answer_present"],
                    stats["answer_length"],
                    stats["deliverable_present"],
                    stats["deliverable_length"],
                    repair_attempted,
                    repair_status,
                    repair_valid,
                )
                break
            except SolutionValidationError as exc:
                validation_reasons.append(exc.reason)
                if attempt:
                    repair_status = "invalid"
                    repair_valid = False
                stats = _validation_fields(raw, context)
                logger.warning(
                    "event=%s validation_status=failed solution_version=%d template=%s "
                    "schema_version=%d validation_reason=%s blocking=true answer_present=%s "
                    "answer_length=%d deliverable_present=%s deliverable_length=%d "
                    "repair_attempted=%s repair_status=%s next_action=%s",
                    "repair_validation" if attempt else "solution_validation",
                    version,
                    stats["template"],
                    SOLUTION_SCHEMA_VERSION,
                    exc.reason,
                    stats["answer_present"],
                    stats["answer_length"],
                    stats["deliverable_present"],
                    stats["deliverable_length"],
                    repair_attempted,
                    repair_status if attempt else "scheduled",
                    "fail" if attempt else "repair",
                )
                if attempt:
                    raise ProviderFailure("VALIDATION", validation_reason=exc.reason) from None
                repair_candidate = _safe_repair_candidate(raw)
                continue
        if solution is None:
            raise AppError("Nenhuma solução válida.")
    except Exception as exc:
        code = exc.code if isinstance(exc, ProviderFailure) else "UNKNOWN"
        message = MESSAGES.get(code, MESSAGES["UNKNOWN"])
        validation_reason = exc.validation_reason if isinstance(exc, ProviderFailure) else None
        if validation_reason:
            message = f"Resposta inválida após reparo: {validation_reason}."
        if repair_attempted and repair_status == "running":
            repair_status = "error"
        final_validation_status = "failed" if repair_status == "invalid" else "not_completed"
        logger.error(
            "event=solution_completion solution_status=FAILED solution_version=%d "
            "attempts=%d validation_status=%s validation_reason=%s repair_attempted=%s "
            "repair_status=%s repair_valid=%s validation_reasons=%s provider_error_code=%s",
            version,
            attempts,
            final_validation_status,
            validation_reasons[-1] if validation_reasons else "none",
            repair_attempted,
            repair_status,
            repair_valid,
            ",".join(validation_reasons) or "none",
            code,
        )
        # Failure bodies and exception messages can contain keys or hostile content.
        with Session(engine) as session, session.begin():
            failed = session.get(SolutionRecord, row_id)
            assert failed is not None
            failed.status = "FAILED"
            failed.model = provider.model
            failed.error = message
            failed.updated_at = datetime.now(UTC)
            failed.request_metadata = {
                **failed.request_metadata,
                "attempts": attempts,
                "validation_status": final_validation_status,
                "validation_reason": validation_reasons[-1] if validation_reasons else None,
                **(
                    {
                        "validation_reasons": validation_reasons,
                        "repair_attempted": repair_attempted,
                        "repair_status": repair_status,
                        "repair_valid": repair_valid,
                    }
                    if validation_reasons
                    else {}
                ),
            }
        raise AppError(f"Solução #{row_id} FAILED: {message}") from None
    with Session(engine, expire_on_commit=False) as session, session.begin():
        saved = session.get(SolutionRecord, row_id)
        assert saved is not None
        saved.response = solution.model_dump()
        saved.model = provider.model
        saved.answer = solution.answer
        saved.status = "NEEDS_REVIEW"  # No automatic approval, even for valid structured responses.
        saved.updated_at = datetime.now(UTC)
        saved.request_metadata = {
            **saved.request_metadata,
            "attempts": attempts,
            "validation_status": "valid",
            "validation_reason": "none",
            "validation_reasons": validation_reasons,
            "repair_attempted": repair_attempted,
            "repair_status": repair_status,
            "repair_valid": repair_valid,
        }
        logger.info(
            "event=solution_completion solution_status=NEEDS_REVIEW validation_status=valid "
            "validation_reason=none solution_version=%d repair_attempted=%s "
            "repair_status=%s repair_valid=%s validation_reasons=%s",
            version,
            repair_attempted,
            repair_status,
            repair_valid,
            ",".join(validation_reasons) or "none",
        )
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
