"""Synchronization orchestration; providers can be tested without Google credentials."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from app.classroom.resources import (
    attach_resource_materials,
    save_resource_links,
    select_resources,
    upsert_resources,
)
from app.domain.models import Assignment, Course, CourseResource, Submission, Topic
from app.errors import ReauthenticationRequired
from app.persistence.models import AssignmentRecord, CourseRecord, SubmissionRecord, SyncRun
from app.persistence.repository import Change, upsert_assignment, upsert_course, upsert_submission

logger = logging.getLogger("classroom_agent")


class Source(Protocol):
    def list_courses(self, *, include_archived: bool = False) -> list[Course]: ...
    def list_assignments(self, course_id: str) -> list[Assignment]: ...
    def list_submissions(self, course_id: str) -> list[Submission]: ...
    def list_course_resources(self, course_id: str) -> list[CourseResource]: ...
    def list_topics(self, course_id: str) -> list[Topic]: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


def empty_counts() -> dict[str, Any]:
    return {
        kind: dict(found=0, created=0, updated=0, unchanged=0)
        for kind in ("courses", "assignments", "submissions")
    }


def count_change(counts: dict[str, Any], kind: str, change: Change) -> None:
    key = {Change.NEW: "created", Change.UPDATED: "updated", Change.UNCHANGED: "unchanged"}[change]
    counts[kind][key] += 1


def synchronize(
    engine: Engine, source_factory: Callable[[], Source], *, clock: Callable[[], datetime] = utc_now
) -> SyncRun:
    counts = empty_counts()
    with Session(engine) as session, session.begin():
        run = SyncRun(started_at=clock(), status="RUNNING", counters=counts)
        session.add(run)
        session.flush()
        run_id = run.id
    failures: list[str] = []
    successes = 0
    status = "FAILED"
    try:
        source = source_factory()
        courses = source.list_courses(include_archived=True)
        counts["courses"]["found"] = len(courses)
        for course_number, course in enumerate(courses, start=1):
            delta = empty_counts()
            try:
                assignments = source.list_assignments(course.google_id)
                counts["assignments"]["found"] += len(assignments)
                submissions = source.list_submissions(course.google_id)
                counts["submissions"]["found"] += len(submissions)
                resource_loader = getattr(source, "list_course_resources", None)
                topic_loader = getattr(source, "list_topics", None)
                resources = resource_loader(course.google_id) if resource_loader else []
                topics = topic_loader(course.google_id) if topic_loader else []
                now = clock()
                # Fetch complete pages before locking; rollback an entire course on any error.
                with Session(engine) as session:
                    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                    try:
                        course_row, change = upsert_course(session, course, now)
                        count_change(delta, "courses", change)
                        resource_rows = upsert_resources(
                            session, course_row, resources, topics, now
                        )
                        known: dict[str, AssignmentRecord] = {}
                        for assignment in assignments:
                            selected = select_resources(assignment, resource_rows)
                            enriched = attach_resource_materials(assignment, selected)
                            row, change = upsert_assignment(session, enriched, now, course_row)
                            save_resource_links(session, row, selected, now)
                            known[assignment.google_id] = row
                            count_change(delta, "assignments", change)
                        seen_submissions: set[int] = set()
                        for submission in submissions:
                            # API can return submissions for nonpublished coursework.
                            if submission.course_id != course.google_id:
                                raise ValueError("Submission course mismatch")
                            if submission.assignment_id not in known:
                                continue
                            sub, change = upsert_submission(
                                session, submission, now, known[submission.assignment_id]
                            )
                            seen_submissions.add(sub.id)
                            count_change(delta, "submissions", change)
                        active_ids = [row.id for row in known.values()]
                        session.execute(
                            update(AssignmentRecord)
                            .where(
                                AssignmentRecord.course_id == course_row.id,
                                AssignmentRecord.id.not_in(active_ids),
                            )
                            .values(present=False)
                        )
                        all_ids = select(AssignmentRecord.id).where(
                            AssignmentRecord.course_id == course_row.id
                        )
                        session.execute(
                            update(SubmissionRecord)
                            .where(
                                SubmissionRecord.assignment_id.in_(all_ids),
                                SubmissionRecord.id.not_in(seen_submissions),
                            )
                            .values(present=False)
                        )
                        # Persist counters in the same transaction as the course snapshot.
                        committed = {
                            kind: {
                                key: counts[kind][key] + delta[kind][key] for key in counts[kind]
                            }
                            for kind in counts
                        }
                        session.execute(
                            update(SyncRun).where(SyncRun.id == run_id).values(counters=committed)
                        )
                        session.commit()
                        counts = committed
                    except Exception:
                        session.rollback()
                        raise
                successes += 1
            except Exception as exc:
                # Never persist provider exception text, SQL parameters, or credential material.
                logger.error(
                    "Falha ao sincronizar disciplina; posição=%s; tipo=%s; snapshot=preservado",
                    course_number,
                    type(exc).__name__,
                )
                failures.append(
                    f"Falha na disciplina #{course_number}; snapshot anterior preservado."
                )
        # Course list was complete, so hide courses that disappeared without deleting history.
        with Session(engine) as session, session.begin():
            session.execute(
                update(CourseRecord)
                .where(CourseRecord.google_id.not_in([course.google_id for course in courses]))
                .values(present=False)
            )
        status = "PARTIAL" if failures and successes else "FAILED" if failures else "SUCCESS"
    except ReauthenticationRequired:
        logger.error("Sincronização requer nova autenticação Google")
        failures.append(
            "Reautenticação necessária para os novos scopes readonly. "
            "Renomeie GOOGLE_TOKEN_FILE (padrão token.json) e execute python main.py auth."
        )
        status = "PARTIAL" if successes else "FAILED"
    except Exception as exc:
        logger.error("Falha geral na sincronização; tipo=%s", type(exc).__name__)
        failures.append(
            "Falha ao iniciar ou concluir sincronização; confira conexão e autenticação."
        )
        status = "PARTIAL" if successes else "FAILED"
    with Session(engine) as session, session.begin():
        stored = session.get(SyncRun, run_id)
        assert stored is not None
        stored.status = status
        stored.finished_at = clock()
        stored.counters = counts
        stored.error = " ".join(failures) if failures else None
        session.flush()
        session.expunge(stored)
        return stored
