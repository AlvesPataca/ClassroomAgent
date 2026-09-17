"""Allowlisted DTOs never serialize configuration, raw records or filesystem locations."""

import re
from urllib.parse import urlsplit, urlunsplit

from app.attachments.materials import safe_url

URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")
SECRET_PATTERN = re.compile(
    r"(?i)(?:access_token|refresh_token|client_secret|api[_-]?key|authorization)"
    r"[\s\"']*[:=][\s\"']*(?:bearer\s+)?[^\s,;\"'}]+"
    r"|\bsk-[A-Za-z0-9_-]{12,}\b|\bya29\.[A-Za-z0-9_-]+"
)
PATH_PATTERN = re.compile(
    r"(?i)file://[^\s<>]+|\b[A-Z]:[\\/][^\s<>]+|\\\\[^\s<>]+"
    r"|(?<![\w:/])/(?:home|Users|etc|tmp|var|root|mnt)/[^\s<>]+"
    r"|(?<!\w)\.\.?[\\/][^\s<>]+"
)


def public_url(value: object) -> str | None:
    url = safe_url(value)
    if url is None or SECRET_PATTERN.search(url):
        return None
    parts = urlsplit(url)
    # Query/fragment values never enter Phase 4. Google document IDs live in path.
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def sanitize(text: str) -> str:
    text = "".join(c for c in text if c.isprintable() or c in "\n\t")
    text = SECRET_PATTERN.sub("[REDACTED_SECRET]", text)
    text = URL_PATTERN.sub(lambda m: public_url(m[0]) or "[REMOVED_URL]", text)
    return PATH_PATTERN.sub("[REDACTED_PATH]", text)
