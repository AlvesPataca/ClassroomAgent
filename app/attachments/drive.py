"""Official Drive v3 metadata + authenticated streaming of library-built media requests."""

import hashlib
import json
import random
import re
import time
from collections.abc import Callable
from typing import Any, TypeVar
from urllib.parse import urlsplit

import httplib2
import requests
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import AuthorizedSession
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.attachments.materials import plain, safe_url
from app.attachments.safety import AttachmentError, choose_format
from app.config import Settings

T = TypeVar("T")
TRANSIENT = {429, 500, 502, 503, 504}
RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "RATE_LIMIT_EXCEEDED"}


def reasons(content: bytes) -> set[str]:
    try:
        error = json.loads(content).get("error", {})
        return {
            str(e.get("reason"))
            for key in ("errors", "details")
            for e in error.get(key, [])
            if isinstance(e, dict)
        }
    except (ValueError, TypeError, AttributeError):
        return set()


class DriveFailure(Exception):
    def __init__(self, status: int, content: bytes = b"") -> None:
        self.status = status
        self.reasons = reasons(content)


def friendly(failure: DriveFailure) -> AttachmentError:
    code, why = failure.status, failure.reasons
    if why & {"SERVICE_DISABLED", "accessNotConfigured"}:
        return AttachmentError("API_DISABLED", "Ative Google Drive API no projeto OAuth e repita.")
    if why & {"ACCESS_TOKEN_SCOPE_INSUFFICIENT", "insufficientPermissions"}:
        return AttachmentError(
            "SCOPE_INSUFFICIENT",
            "Drive: reautenticação necessária. Renomeie "
            "GOOGLE_TOKEN_FILE (token.json) e execute python main.py auth.",
        )
    if code == 401:
        return AttachmentError("UNAUTHENTICATED", "Token inválido; reautentique com auth.")
    if code == 404:
        return AttachmentError("NOT_FOUND", "Arquivo removido ou não acessível à conta (404).")
    if code == 429 or why & RATE_REASONS:
        return AttachmentError("RATE_LIMIT", "Limite temporário do Drive; tente mais tarde.")
    if why & {"dailyLimitExceeded", "quotaExceeded", "downloadQuotaExceeded"}:
        return AttachmentError("QUOTA", "Quota do Drive esgotada; tente mais tarde.")
    if why & {"exportSizeLimitExceeded"}:
        return AttachmentError("TOO_LARGE", "Exportação excede o limite de 10 MB do Drive.")
    if code == 403:
        return AttachmentError(
            "FORBIDDEN", "Drive negou acesso/download (403); confira permissões."
        )
    return AttachmentError("DRIVE_ERROR", f"Drive indisponível (HTTP {code}).")


class DriveClient:
    def __init__(self, service: Any, transport: Any, settings: Settings) -> None:
        self.service, self.transport, self.settings = service, transport, settings

    @classmethod
    def from_credentials(cls, credentials: Any, settings: Settings) -> "DriveClient":
        service = build(
            "drive",
            "v3",
            http=AuthorizedHttp(
                credentials, http=httplib2.Http(timeout=settings.http_timeout_seconds)
            ),
            cache_discovery=False,
            static_discovery=True,
        )
        return cls(service, AuthorizedSession(credentials), settings)  # type: ignore[no-untyped-call]

    def close(self) -> None:
        self.transport.close()
        self.service.close()

    def retry(self, action: Callable[[], T]) -> T:
        for attempt in range(self.settings.max_retries + 1):
            try:
                return action()
            except HttpError as exc:
                failure = DriveFailure(int(exc.resp.status), exc.content)
            except DriveFailure as exc:
                failure = exc
            except (requests.RequestException, OSError, httplib2.HttpLib2Error) as exc:
                if attempt == self.settings.max_retries:
                    raise AttachmentError("TIMEOUT", "Falha de conexão/timeout no Drive.") from exc
                failure = DriveFailure(503)
            except GoogleAuthError as exc:
                raise AttachmentError("UNAUTHENTICATED", "Token inválido; execute auth.") from exc
            transient = failure.status in TRANSIENT or (
                failure.status == 403 and bool(failure.reasons & RATE_REASONS)
            )
            if not transient or attempt == self.settings.max_retries:
                raise friendly(failure) from None
            time.sleep(min(2**attempt, 32) + random.uniform(0, 1))
        raise AssertionError("Unreachable")

    def metadata(self, drive_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", drive_id):
            raise AttachmentError("INVALID_ID", "ID Drive inválido.")
        raw = self.retry(
            lambda: (
                self.service.files()
                .get(
                    fileId=drive_id,
                    supportsAllDrives=True,
                    fields="id,name,mimeType,size,modifiedTime,version,md5Checksum,trashed,"
                    "capabilities(canDownload),webViewLink",
                )
                .execute(num_retries=0)
            )
        )
        if not isinstance(raw, dict) or raw.get("id") != drive_id:
            raise AttachmentError("INVALID_METADATA", "Metadata Drive inválida.")
        result = {
            key: plain(raw.get(key))
            for key in ("id", "name", "mimeType", "modifiedTime", "version", "md5Checksum")
        }
        result["webViewLink"] = safe_url(raw.get("webViewLink")) or ""
        safe: dict[str, Any] = dict(result)
        safe["trashed"] = raw.get("trashed") is True
        caps = raw.get("capabilities", {})
        safe["canDownload"] = isinstance(caps, dict) and caps.get("canDownload") is True
        try:
            safe["size"] = int(raw["size"]) if "size" in raw else None
        except (ValueError, TypeError) as exc:
            raise AttachmentError("INVALID_METADATA", "Tamanho Drive inválido.") from exc
        return safe

    def download(self, meta: dict[str, Any]) -> tuple[bytes, str]:
        if meta.get("trashed"):
            raise AttachmentError("NOT_FOUND", "Arquivo está na lixeira do Drive.")
        if not meta.get("canDownload"):
            raise AttachmentError("FORBIDDEN", "Proprietário não permite download/exportação.")
        mime, export = choose_format(meta["mimeType"], meta["name"])
        limit = self.settings.attachment_max_bytes
        if not export and meta.get("size") is not None and meta["size"] > limit:
            raise AttachmentError("TOO_LARGE", "Arquivo maior que ATTACHMENT_MAX_BYTES.")
        request = (
            self.service.files().export_media(fileId=meta["id"], mimeType=mime)
            if export
            else self.service.files().get_media(fileId=meta["id"], supportsAllDrives=True)
        )
        # Use the official discovery request URI, never a material URL or exportLinks.
        uri = request.uri
        parts = urlsplit(uri)
        if parts.scheme != "https" or parts.hostname != "www.googleapis.com":
            raise AttachmentError("UNSAFE_URL", "Destino de download não permitido.")

        def stream() -> bytes:
            with self.transport.get(
                uri, stream=True, allow_redirects=False, timeout=self.settings.http_timeout_seconds
            ) as response:
                if response.status_code != 200:
                    # Read at most one small error chunk; never echo provider content.
                    error = next(response.iter_content(chunk_size=8192), b"")
                    raise DriveFailure(response.status_code, error[:8192])
                length = response.headers.get("Content-Length")
                if length is not None and int(length) > limit:
                    raise AttachmentError("TOO_LARGE", "Download excede o limite configurado.")
                data = bytearray()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if len(data) + len(chunk) > limit:
                        raise AttachmentError("TOO_LARGE", "Download excede o limite configurado.")
                    data.extend(chunk)
                if not export and meta.get("size") is not None and len(data) != meta["size"]:
                    raise DriveFailure(503)
                if (
                    not export
                    and meta.get("md5Checksum")
                    and hashlib.md5(data, usedforsecurity=False).hexdigest() != meta["md5Checksum"]
                ):
                    raise AttachmentError("CHECKSUM_MISMATCH", "Integridade do download inválida.")
                return bytes(data)

        return self.retry(stream), mime
