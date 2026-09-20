import hashlib
import json
import re
import unicodedata
from copy import deepcopy
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from app.attachments.materials import parse_materials, safe_url
from app.domain.models import Assignment, CourseResource, Topic
from app.persistence.models import (
    AssignmentRecord,
    AssignmentResourceLink,
    CourseRecord,
    CourseResourceRecord,
)

STOPWORDS = {
    "a", "ao", "aos", "as", "com", "da", "das", "de", "do", "dos", "e", "em",
    "essa", "esse", "esta", "este", "o", "os", "para", "por", "que", "sobre", "um",
    "uma", "atividade", "material", "trabalho", "aula",
}
REFERENCE_WORDS = re.compile(
    r"(?i)\b(?:slide|slides|material|arquivo|apresenta(?:ção|cao)|conteúdo|conteudo|aula)\b"
)
URLS = re.compile(r"https?://[^\s<>\"']+")
GOOGLE_FILE = re.compile(
    r"^/(?:presentation|document|spreadsheets)/d/([A-Za-z0-9_-]+)(?:/.*)?$"
)
DRIVE_FILE = re.compile(r"^/file/d/([A-Za-z0-9_-]+)(?:/.*)?$")


def _tokens(value: str | None) -> set[str]:
    folded = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    return {
        token
        for token in re.findall(r"[a-z0-9]{3,}", folded.lower())
        if token not in STOPWORDS
    }


def _normalized_materials(materials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for item in parse_materials(materials):
        title, url, drive_id = item["title"], item["url"], item["drive_id"]
        if item["kind"] == "DRIVE_FILE" and drive_id:
            output.append(
                {"driveFile": {"driveFile": {"id": drive_id, "title": title, "alternateLink": url}}}
            )
        elif item["kind"] == "LINK" and url:
            output.append({"link": {"url": url, "title": title}})
        elif item["kind"] == "YOUTUBE" and url:
            output.append(
                {
                    "youtubeVideo": {
                        "id": item["identity_key"].split(":", 1)[1],
                        "title": title,
                        "alternateLink": url,
                    }
                }
            )
        elif item["kind"] == "FORM" and url:
            output.append({"form": {"title": title, "formUrl": url}})
    return output


def _materials_from_text(resource: CourseResource) -> list[dict[str, Any]]:
    """Turn teacher-pasted document URLs into normal Classroom materials."""
    found: list[dict[str, Any]] = []
    text = "\n".join(filter(None, (resource.title, resource.description)))
    # Announcement titles can be synthesized from their entire body.  Never carry
    # the pasted URL into the filename: dots such as ``docs.google.com`` are
    # intentionally treated as suspicious filename suffixes by attachment safety.
    clean_title = URLS.sub("", resource.title).strip(" -:\n\t")
    material_title = clean_title[:512] or "Material de apoio"
    for match in URLS.finditer(text):
        url = safe_url(match[0].rstrip(".,;!?)"))
        if not url:
            continue
        parts = urlsplit(url)
        identifier: str | None = None
        if parts.hostname == "docs.google.com":
            document = GOOGLE_FILE.fullmatch(parts.path)
            identifier = document.group(1) if document else None
        elif parts.hostname == "drive.google.com":
            document = DRIVE_FILE.fullmatch(parts.path)
            identifier = document.group(1) if document else None
        if identifier:
            found.append(
                {
                    "driveFile": {
                        "driveFile": {
                            "id": identifier,
                            "title": material_title,
                            "alternateLink": url,
                        }
                    }
                }
            )
        else:
            found.append({"link": {"url": url, "title": material_title}})
    return found


def upsert_resources(
    session: Session,
    course: CourseRecord,
    resources: list[CourseResource],
    topics: list[Topic],
    now: datetime,
) -> list[tuple[CourseResourceRecord, CourseResource]]:
    topic_names = {topic.google_id: topic.name for topic in topics}
    seen: set[tuple[str, str]] = set()
    output: list[tuple[CourseResourceRecord, CourseResource]] = []
    for resource in resources:
        if resource.course_id != course.google_id:
            raise ValueError("Course resource mismatch")
        materials = _normalized_materials(
            [*resource.materials, *_materials_from_text(resource)]
        )
        semantic = {
            "title": resource.title,
            "description": resource.description,
            "topic_id": resource.topic_id,
            "topic_name": topic_names.get(resource.topic_id or ""),
            "materials": materials,
        }
        fingerprint = hashlib.sha256(
            json.dumps(semantic, sort_keys=True, ensure_ascii=True).encode()
        ).hexdigest()
        identity = {"course_id": course.id, "kind": resource.kind, "google_id": resource.google_id}
        seen.add((resource.kind, resource.google_id))
        values = {
            **identity,
            **semantic,
            "alternate_link": safe_url(resource.alternate_link),
            "fingerprint": fingerprint,
            "creation_time": resource.creation_time,
            "update_time": resource.update_time,
            "first_seen_at": now,
            "last_seen_at": now,
            "present": True,
        }
        statement = insert(CourseResourceRecord).values(**values)
        session.execute(
            statement.on_conflict_do_update(
                index_elements=["course_id", "kind", "google_id"],
                set_={k: v for k, v in values.items() if k not in {*identity, "first_seen_at"}},
            )
        )
        row = session.scalar(select(CourseResourceRecord).filter_by(**identity))
        assert row is not None
        output.append((row, resource))
    existing = list(
        session.scalars(
            select(CourseResourceRecord).where(CourseResourceRecord.course_id == course.id)
        )
    )
    for row in existing:
        if (row.kind, row.google_id) not in seen:
            row.present = False
    session.flush()
    return output


def score_resource(assignment: Assignment, resource: CourseResource) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    if assignment.topic_id and assignment.topic_id == resource.topic_id:
        score += 60
        reasons.append("mesmo tópico da atividade")
    assignment_tokens = _tokens(f"{assignment.title} {assignment.description or ''}")
    resource_tokens = _tokens(f"{resource.title} {resource.description or ''}")
    overlap = assignment_tokens & resource_tokens
    if overlap:
        text_score = min(45, 12 + len(overlap) * 8)
        score += text_score
        reasons.append("termos em comum: " + ", ".join(sorted(overlap)[:6]))
    if assignment.creation_time and resource.creation_time:
        days = (assignment.creation_time - resource.creation_time).total_seconds() / 86400
        if 0 <= days <= 21:
            score += max(8, 25 - int(days))
            reasons.append(f"publicado {int(days)} dia(s) antes da atividade")
        elif -2 <= days < 0:
            score += 8
            reasons.append("publicado próximo da atividade")
    return min(score, 100), reasons


def select_resources(
    assignment: Assignment,
    resources: list[tuple[CourseResourceRecord, CourseResource]],
) -> list[tuple[CourseResourceRecord, CourseResource, int, list[str]]]:
    ranked = [(*pair, *score_resource(assignment, pair[1])) for pair in resources]
    ranked.sort(key=lambda item: (-item[2], item[0].id))
    selected = [item for item in ranked if item[2] >= 45]
    if not selected and REFERENCE_WORDS.search(assignment.description or ""):
        selected = [item for item in ranked[:2] if item[2] >= 8]
    return selected[:5]


def attach_resource_materials(
    assignment: Assignment,
    selected: list[tuple[CourseResourceRecord, CourseResource, int, list[str]]],
) -> Assignment:
    copy = assignment.model_copy(deep=True)
    direct = copy.raw_payload.get("materials", [])
    if not isinstance(direct, list):
        raise ValueError("Invalid assignment materials")
    related: list[dict[str, Any]] = []
    for row, resource, score, reasons in selected:
        for material in row.materials:
            enriched = deepcopy(material)
            enriched["_classroom_agent"] = {
                "resource_id": row.id,
                "resource_kind": resource.kind,
                "resource_title": resource.title,
                "score": score,
                "reasons": reasons,
            }
            related.append(enriched)
    copy.raw_payload["materials"] = [*related, *direct]
    return copy


def save_resource_links(
    session: Session,
    assignment: AssignmentRecord,
    selected: list[tuple[CourseResourceRecord, CourseResource, int, list[str]]],
    now: datetime,
) -> None:
    resource_ids = [row.id for row, _, _, _ in selected]
    session.execute(
        update(AssignmentResourceLink)
        .where(
            AssignmentResourceLink.assignment_id == assignment.id,
            AssignmentResourceLink.resource_id.not_in(resource_ids),
        )
        .values(selected=False, updated_at=now)
    )
    for resource, _, score, reasons in selected:
        statement = insert(AssignmentResourceLink).values(
            assignment_id=assignment.id,
            resource_id=resource.id,
            score=score,
            reasons=reasons,
            selected=True,
            updated_at=now,
        )
        session.execute(
            statement.on_conflict_do_update(
                index_elements=["assignment_id", "resource_id"],
                set_={"score": score, "reasons": reasons, "selected": True, "updated_at": now},
            )
        )
