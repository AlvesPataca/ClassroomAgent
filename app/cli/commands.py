from datetime import UTC, datetime
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.attachments.drive import DriveClient
from app.attachments.materials import plain
from app.attachments.service import AttachmentService
from app.auth.google import authenticate
from app.classroom.client import ClassroomClient
from app.config import load_settings
from app.context.builder import build_assignment_context
from app.domain.models import SubmissionStatus
from app.domain.status import is_overdue, normalize_status
from app.errors import AppError
from app.llm.providers import get_provider
from app.llm.service import list_solutions, solve_context
from app.persistence.database import open_database
from app.persistence.models import AssignmentRecord
from app.persistence.reader import LocalReader
from app.sync import synchronize

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
console = Console()
errors = Console(stderr=True)
ArchiveOption = Annotated[
    bool, typer.Option("--include-archived", help="Inclui cursos arquivados.")
]
ApiOption = Annotated[bool, typer.Option("--api", help="Consulta API diretamente, sem persistir.")]
CourseOption = Annotated[str | None, typer.Option("--course", help="ID Google do curso.")]


@app.callback()
def main(ctx: typer.Context, debug: bool = False) -> None:
    """Classroom Agent — consultas somente leitura para alunos."""
    ctx.obj = {"debug": debug}


def fail(ctx: typer.Context, exc: AppError) -> None:
    errors.print(Text(f"Erro: {exc}"), style="red")
    if ctx.obj and ctx.obj.get("debug"):
        cause = exc.__cause__
        errors.print(f"Diagnóstico seguro: {type(cause).__name__ if cause else type(exc).__name__}")
    raise typer.Exit(1)


def get_client() -> ClassroomClient:
    settings = load_settings()
    return ClassroomClient.from_credentials(authenticate(settings), settings)


def get_reader(api: bool) -> ClassroomClient | LocalReader:
    if api:
        return get_client()
    settings = load_settings()
    engine = open_database(settings.database_file)
    try:
        reader = LocalReader(engine, settings)
    finally:
        engine.dispose()
    errors.print(f"Snapshot local: {reader.sync_status}; última conclusão: {reader.synced_at}.")
    if reader.sync_status != "SUCCESS":
        errors.print("Atenção: dados podem estar incompletos ou desatualizados. Execute sync.")
    return reader


@app.command()
def sync(ctx: typer.Context) -> None:
    """Atualiza o SQLite com cursos, atividades e submissões da conta real."""
    try:
        engine = open_database(load_settings().database_file)
        try:
            result = synchronize(engine, get_client)
        finally:
            engine.dispose()
        console.print(f"Sync #{result.id}: {result.status}")
        console.print(result.counters)
        if result.error:
            errors.print(result.error)
        if result.status != "SUCCESS":
            raise typer.Exit(1)
    except (SQLAlchemyError, OSError, ValueError):
        fail(ctx, AppError("Falha no banco/configuração local; confira caminho e permissões."))
    except AppError as exc:
        fail(ctx, exc)


@app.command()
def auth(ctx: typer.Context, debug: bool = False) -> None:
    """Autoriza a conta Google e salva/renova o token local."""
    try:
        debug = debug or bool(ctx.obj and ctx.obj.get("debug"))
        ctx.obj = {"debug": debug}

        def diagnostic(message: str) -> None:
            errors.print(Text(message))

        authenticate(load_settings(), interactive=True, diagnostic=diagnostic if debug else None)
        console.print("Conta autenticada. Acesso somente leitura.")
    except (SQLAlchemyError, OSError, ValueError):
        fail(ctx, AppError("Falha no banco/configuração local; confira caminho e permissões."))
    except AppError as exc:
        fail(ctx, exc)


@app.command()
def courses(
    ctx: typer.Context, include_archived: ArchiveOption = False, api: ApiOption = False
) -> None:
    """Lista as disciplinas em que você é aluno."""
    try:
        rows = get_reader(api).list_courses(include_archived=include_archived)
        table = Table("ID Google", "Disciplina", "Turma", "Estado")
        for course in rows:
            table.add_row(
                *(
                    Text(value)
                    for value in (
                        course.google_id,
                        course.name,
                        course.section or "—",
                        course.state,
                    )
                )
            )
        console.print(table)
        console.print(f"{len(rows)} disciplina(s).")
    except (SQLAlchemyError, OSError, ValueError):
        fail(ctx, AppError("Falha no banco/configuração local; confira caminho e permissões."))
    except AppError as exc:
        fail(ctx, exc)


def display_assignments(
    ctx: typer.Context,
    course_id: str | None,
    include_archived: bool,
    pending_only: bool,
    api: bool = False,
) -> None:
    try:
        client = get_reader(api)
        course_rows = client.list_courses(include_archived=include_archived)
        if course_id is not None:
            course_rows = [course for course in course_rows if course.google_id == course_id]
            if not course_rows:
                raise AppError("Curso não encontrado no filtro. Confira ID e --include-archived.")
        table = Table(
            "Curso / Atividade (Google)",
            "Disciplina",
            "Atividade",
            "Prazo",
            "Status",
            "Atraso",
            "late API",
        )
        failed = 0
        unknown = 0
        total = 0
        now = datetime.now(UTC)
        for course in course_rows:
            try:
                assignments = client.list_assignments(course.google_id)
                submissions = client.list_submissions(course.google_id)
                by_assignment = {(s.course_id, s.assignment_id): s for s in submissions}
                for assignment in sorted(
                    assignments, key=lambda a: (a.due_at is None, a.due_at or now, a.google_id)
                ):
                    submission = by_assignment.get((course.google_id, assignment.google_id))
                    status = normalize_status(assignment, submission, now)
                    unknown += status == SubmissionStatus.UNKNOWN
                    if pending_only and status not in {
                        SubmissionStatus.PENDING,
                        SubmissionStatus.MISSING,
                    }:
                        continue
                    due = assignment.due_at
                    table.add_row(
                        *(
                            Text(value)
                            for value in (
                                f"{course.google_id} / {assignment.google_id}",
                                course.name,
                                assignment.title,
                                due.strftime("%d/%m/%Y %H:%M:%S %z") if due else "Sem prazo",
                                status.value,
                                "sim" if is_overdue(assignment, submission, now) else "não",
                                str(submission.late)
                                if submission and submission.late is not None
                                else "—",
                            )
                        )
                    )
                    total += 1
            except AppError as exc:
                failed += 1
                errors.print(Text(f"Falha no curso {course.google_id}: {exc}"))
        console.print(table)
        console.print(f"{total} atividade(s). Timezone: {client.settings.timezone}.")
        if unknown:
            errors.print(f"{unknown} atividade(s) com status UNKNOWN; consulte assignments.")
        if failed:
            errors.print(f"Resultado parcial: {failed} curso(s) não consultado(s) completamente.")
            raise typer.Exit(1)
    except (SQLAlchemyError, OSError, ValueError):
        fail(ctx, AppError("Falha no banco/configuração local; confira caminho e permissões."))
    except AppError as exc:
        fail(ctx, exc)


@app.command()
def assignments(
    ctx: typer.Context,
    course: CourseOption = None,
    include_archived: ArchiveOption = False,
    api: ApiOption = False,
) -> None:
    """Lista atividades publicadas, com status da sua submissão."""
    display_assignments(ctx, course, include_archived, False, api)


@app.command()
def pending(
    ctx: typer.Context,
    course: CourseOption = None,
    include_archived: ArchiveOption = False,
    api: ApiOption = False,
) -> None:
    """Lista PENDING e MISSING (pendentes com prazo vencido)."""
    display_assignments(ctx, course, include_archived, True, api)


@app.command("local-assignments")
def local_assignments(ctx: typer.Context) -> None:
    """Lista IDs locais inteiros para os comandos de anexos."""
    try:
        engine = open_database(load_settings().database_file)
        try:
            with Session(engine) as session:
                table = Table("ID local", "Curso local", "Atividade", "Presente")
                for row in session.scalars(select(AssignmentRecord).order_by(AssignmentRecord.id)):
                    table.add_row(
                        str(row.id),
                        str(row.course_id),
                        Text(plain(row.snapshot.get("title"), 100)),
                        str(row.present),
                    )
                console.print(table)
        finally:
            engine.dispose()
    except AppError as exc:
        fail(ctx, exc)
    except (SQLAlchemyError, OSError, ValueError):
        fail(ctx, AppError("Falha no banco/configuração local."))


def attachment_command(ctx: typer.Context, assignment_id: int, action: str) -> None:
    try:
        settings = load_settings()
        engine = open_database(settings.database_file)
        try:
            service = AttachmentService(engine, settings)
            rows: list[dict[str, Any]] = service.list(assignment_id)
            if action == "fetch":
                drive = DriveClient.from_credentials(authenticate(settings), settings)
                try:
                    rows = service.fetch(assignment_id, drive)
                finally:
                    drive.close()
            elif action == "extract":
                rows = service.extract(assignment_id)
            table = Table("ID", "Nome", "Tipo / MIME", "Status", "Chars / páginas", "Caminho")
            for row in rows:
                status = row["status"] + (" / REMOVED" if not row["present"] else "")
                values = (
                    str(row["id"]),
                    plain(row["title"], 100),
                    f"{row['kind']} / {row['mime_type'] or '—'}",
                    status,
                    f"{row['characters'] or 0} / {row['pages'] or '—'}",
                    row["path"] or "—",
                )
                table.add_row(*(Text(plain(value, 512)) for value in values))
            console.print(table)
            for row in rows:
                if action == "list":
                    console.print(
                        Text(
                            f"Anexo #{row['id']} — Drive ID: {plain(row['drive_id']) or '—'}; "
                            f"URL: {plain(row['url']) or '—'}"
                        )
                    )
                if row["error"]:
                    console.print(Text(f"Anexo #{row['id']}: {plain(row['error'])}"))
            if not rows:
                console.print("Nenhum anexo descoberto. Após migrar a Fase 2, execute sync.")
            if action != "list" and any(r["status"] == "FAILED" and r["present"] for r in rows):
                raise typer.Exit(1)
        finally:
            engine.dispose()
    except AppError as exc:
        fail(ctx, exc)
    except (SQLAlchemyError, OSError, ValueError):
        fail(ctx, AppError("Falha no banco, arquivo ou configuração local."))


@app.command()
def attachments(ctx: typer.Context, assignment_local_id: int) -> None:
    """Lista metadata dos anexos persistidos, inclusive removidos; sem acesso à rede."""
    attachment_command(ctx, assignment_local_id, "list")


@app.command("fetch-attachments")
def fetch_attachments(ctx: typer.Context, assignment_local_id: int) -> None:
    """Obtém/exporta apenas arquivos Drive autorizados e suportados."""
    attachment_command(ctx, assignment_local_id, "fetch")


@app.command()
def extract(ctx: typer.Context, assignment_local_id: int) -> None:
    """Extrai texto local não confiável e mostra somente resumo seguro."""
    attachment_command(ctx, assignment_local_id, "extract")


def phase4_command(
    ctx: typer.Context,
    assignment_id: int,
    action: str,
    provider: str | None,
    offline: bool,
    json_output: bool = False,
) -> None:
    try:
        settings = load_settings()
        engine = open_database(settings.database_file)
        try:
            context = build_assignment_context(
                engine, assignment_id, settings, read_forms=not offline
            )
            if action == "context":
                if json_output:
                    console.print(
                        context.model_dump_json(indent=2),
                        markup=False,
                        highlight=False,
                        soft_wrap=True,
                    )
                    return
                console.print(
                    Text(
                        f"Disciplina: {context.course.text}\n"
                        f"Atividade: {context.assignment.text}\n"
                        f"Fonte: {' + '.join(context.source_types) or 'UNKNOWN'}\n"
                        f"Tipo acadêmico: {', '.join(context.assignment_types)}\n"
                        f"Enunciado:\n{context.description.text or '(nenhum)'}\n"
                        f"Prazo: {context.due_at or 'Sem prazo'}\nEstado: {context.state}\n"
                        f"Pontos máximos: {context.max_points}\n"
                        f"Anexos: {len(context.attachments) or 'nenhum'}\n"
                        f"Forms: {len(context.forms) or 'nenhum'}\n"
                        f"Ready for AI: {'SIM' if context.ready_for_ai else 'NÃO'}"
                    )
                )
                for sub in context.submissions:
                    console.print(
                        Text(
                            f"Submissão: {sub.state}; nota: {sub.assigned_grade}; "
                            f"nota provisória: {sub.draft_grade}; late: {sub.late}"
                        )
                    )
                for link in context.links:
                    console.print(Text(f"Link/material: {link.url}"))
                for form in context.forms:
                    console.print(
                        Text(f"Forms: {form.status}; {len(form.questions)} questões; {form.reason}")
                    )
                for attachment in context.attachments:
                    console.print(
                        Text(
                            f"Anexo #{attachment.local_id}: {attachment.title.text}; "
                            f"{attachment.status}; texto disponível: {bool(attachment.content)}"
                        )
                    )
                for item in context.missing_context + context.warnings:
                    console.print(Text(item))
            elif action == "solve":
                if not context.ready_for_ai:
                    raise AppError("Solve bloqueado: " + "; ".join(context.missing_context))
                selected = get_provider(settings, provider)
                console.print(
                    Text(f"Provider: {selected.name} / {selected.model}; revisão humana.")
                )
                row = solve_context(engine, context, selected)
                console.print(Text(f"Solução #{row.id} v{row.version}: {row.status}"))
                console.print(Text(str((row.response or {}).get("summary", ""))))
                console.print(Text(row.answer or ""))
                for question in (row.response or {}).get("question_answers", []):
                    console.print(Text(f"Questão {question['question_id']}: {question['answer']}"))
            else:
                rows = list_solutions(engine, context)
                table = Table("ID", "Versão", "Provider/model", "Status", "Stale", "Criada")
                for row, stale in rows:
                    table.add_row(
                        str(row.id),
                        str(row.version),
                        Text(f"{row.provider}/{row.model}"),
                        row.status,
                        "SIM" if stale else "NÃO",
                        str(row.created_at),
                    )
                console.print(table)
                if not rows:
                    console.print("Nenhuma solução preparada.")
                if offline and context.forms:
                    console.print(
                        "Stale compara contexto offline; disponibilidade de Forms pode diferir."
                    )
        finally:
            engine.dispose()
    except AppError as exc:
        fail(ctx, exc)
    except (SQLAlchemyError, OSError, ValueError, TypeError):
        fail(
            ctx,
            AppError("Falha segura no contexto/banco/configuração; confira dados e execute sync."),
        )


@app.command()
def context(
    ctx: typer.Context,
    assignment_local_id: int,
    offline: bool = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Contexto canônico sanitizado com provenance.")
    ] = False,
) -> None:
    """Descobre enunciado e materiais; --offline desativa leitura oficial de Forms."""
    phase4_command(ctx, assignment_local_id, "context", None, offline, json_output)


@app.command()
def solve(
    ctx: typer.Context,
    assignment_local_id: int,
    provider: str | None = None,
    offline: bool = False,
) -> None:
    """Prepara solução versionada para revisão. --provider mock não chama LLM externo."""
    phase4_command(ctx, assignment_local_id, "solve", provider, offline)


@app.command()
def solutions(ctx: typer.Context, assignment_local_id: int, offline: bool = False) -> None:
    """Lista versões e compara hash com contexto atual; nunca aprova/entrega."""
    phase4_command(ctx, assignment_local_id, "solutions", None, offline)
