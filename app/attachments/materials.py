import hashlib
import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.persistence.models import AttachmentRecord


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def safe_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or any(not c.isprintable() for c in value)
        ):
            return None
        # Only public document/video identifiers survive; no auth/query secrets or fragments.
        query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if k in {"id", "v"}])
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
    except ValueError:
        return None


def plain(value: object, limit: int = 512) -> str:
    text = value if isinstance(value, str) else ""
    return "".join(c for c in text if c.isprintable())[:limit]


def parse_materials(materials: object) -> list[dict[str, Any]]:
    if not isinstance(materials, list):
        raise ValueError("Invalid materials list")
    rows: dict[str, dict[str, Any]] = {}
    for index, material in enumerate(materials):
        raw = material if isinstance(material, dict) else {}
        kind, item = "UNKNOWN", {}
        for key, normalized in (
            ("driveFile", "DRIVE_FILE"),
            ("link", "LINK"),
            ("youtubeVideo", "YOUTUBE"),
            ("form", "FORM"),
        ):
            if isinstance(raw.get(key), dict):
                kind, item = normalized, raw[key]
                if key == "driveFile":
                    item = item.get("driveFile", {})
                break
        if not isinstance(item, dict):
            item = {}
        identifier = plain(item.get("id"), 256)
        drive_id = identifier if kind == "DRIVE_FILE" else None
        if drive_id and not re.fullmatch(r"[A-Za-z0-9_-]+", drive_id):
            drive_id = None
        url = safe_url(item.get("alternateLink") or item.get("url") or item.get("formUrl"))
        title = plain(item.get("title") or item.get("name")) or "Sem título"
        identity = kind + ":" + (drive_id or identifier or url or f"slot-{index}")
        sanitized = dict(kind=kind, title=title, drive_id=drive_id, url=url)
        rows[identity] = dict(identity_key=identity, **sanitized, raw_payload=sanitized)
    return list(rows.values())


def archive(row: AttachmentRecord, now: datetime) -> None:
    row.history = [
        *row.history,
        {
            "at": now.isoformat(),
            "title": row.title,
            "raw_payload": row.raw_payload,
            "drive_metadata": row.drive_metadata,
            "status": row.status,
            "local_path": row.local_path,
            "content_hash": row.content_hash,
            "materialized_mime": row.materialized_mime,
        },
    ]


def discover(session: Session, assignment_id: int, materials: object, now: datetime) -> bool:
    previous = {
        r.identity_key: r
        for r in session.scalars(
            select(AttachmentRecord).where(AttachmentRecord.assignment_id == assignment_id)
        )
    }
    seen: set[str] = set()
    changed = False
    for fields in parse_materials(materials):
        key = fields["identity_key"]
        seen.add(key)
        fingerprint = digest(fields["raw_payload"])
        row = previous.get(key)
        if row is None:
            row = AttachmentRecord(
                assignment_id=assignment_id,
                **fields,
                fingerprint=fingerprint,
                first_seen_at=now,
                last_seen_at=now,
                updated_at=now,
                drive_metadata={},
                history=[],
                status="DISCOVERED",
                present=True,
            )
            session.add(row)
            changed = True
        else:
            if row.fingerprint != fingerprint or not row.present:
                archive(row, now)
                for name, value in fields.items():
                    setattr(row, name, value)
                row.fingerprint = fingerprint
                row.updated_at = now
                row.status = "DISCOVERED"
                row.extracted = None
                row.error = row.error_code = None
                changed = True
            row.present, row.removed_at, row.last_seen_at = True, None, now
    for key, row in previous.items():
        if key not in seen and row.present:
            archive(row, now)
            row.present, row.removed_at, row.updated_at = False, now, now
            changed = True
    session.flush()
    return changed
