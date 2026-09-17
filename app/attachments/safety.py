"""Only allowlisted document bytes can enter a fixed, application-owned directory."""

import hashlib
import os
import re
import stat
import unicodedata
from pathlib import Path, PureWindowsPath

from app.errors import AppError

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
FORMATS = {
    "text/plain": ".txt",
    "text/markdown": ".md",
    "application/pdf": ".pdf",
    DOCX: ".docx",
    PPTX: ".pptx",
    XLSX: ".xlsx",
}
EXPORTS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.presentation": PPTX,
    "application/vnd.google-apps.spreadsheet": XLSX,
}
BLOCKED = frozenset(
    ".exe .bat .cmd .ps1 .psm1 .psd1 .sh .bash .zsh .jar .com .scr .msi .msp .dll "
    ".cpl .hta .vbs .vbe .js .jse .wsf .wsh .lnk .url .reg .py .pyw .pl .rb .php "
    ".app .dmg .pkg .deb .rpm .so .docm .xlsm .pptm .xlam .dotm .potm .sct .scf "
    ".desktop .command .appx .msix .iso .img .pif .gadget .application .vxd .sys "
    ".csh .ksh .vb .msc".split()
)
RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³])(?:\.|$)", re.I)


class AttachmentError(AppError):
    def __init__(self, code: str, message: str, *, unsupported: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.unsupported = unsupported


def check_name(name: str) -> None:
    normalized = unicodedata.normalize("NFKC", name).lower().rstrip(" .")
    parts = re.split(r"[/\\:]", normalized)
    if any(p in {".env", "credentials.json", "token.json"} for p in parts):
        raise AttachmentError("BLOCKED", "Nome de arquivo sensível bloqueado.", unsupported=True)
    if any(suffix in BLOCKED for p in parts for suffix in Path(p).suffixes):
        raise AttachmentError("BLOCKED", "Arquivo executável/macros bloqueado.", unsupported=True)


def safe_name(name: str) -> str:
    check_name(name)
    name = unicodedata.normalize("NFKC", name)
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .")[:60].rstrip(" .")
    if not name or RESERVED.match(name):
        name = "document_" + name
    return name


def choose_format(mime: str, name: str) -> tuple[str, bool]:
    check_name(name)
    if mime in EXPORTS:
        return EXPORTS[mime], True
    if mime in FORMATS:
        return mime, False
    reason = "IMAGE_NO_OCR" if mime.startswith("image/") else "UNSUPPORTED_MIME"
    raise AttachmentError(
        reason, "Formato sem suporte nesta fase; somente metadata.", unsupported=True
    )


def reject_active_bytes(data: bytes) -> None:
    if data.startswith(
        (b"MZ", b"\x7fELF", b"#!", b"\xca\xfe\xba\xbe", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf")
    ):
        raise AttachmentError("BLOCKED", "Conteúdo executável bloqueado.", unsupported=True)


class AttachmentStore:
    def __init__(self, root: Path, max_bytes: int) -> None:
        # Do not resolve first: resolution would hide a redirected root/junction.
        self.root = root.absolute()
        self.max_bytes = max_bytes

    def _check(self, path: Path) -> Path:
        if not path.is_relative_to(self.root):
            raise AttachmentError("UNSAFE_PATH", "Caminho fora da pasta controlada.")
        for part in (path, *path.parents):
            if part.is_symlink() or (
                part.exists()
                and getattr(part.lstat(), "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            ):
                raise AttachmentError("UNSAFE_PATH", "Links/junctions não são permitidos.")
        if path.exists() and path.is_file() and path.stat().st_nlink != 1:
            raise AttachmentError("UNSAFE_PATH", "Hard links não são permitidos.")
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise AttachmentError("UNSAFE_PATH", "Caminho redirecionado bloqueado.")
        return path

    def path(self, relative: str, course_id: int, assignment_id: int) -> Path:
        parts = relative.split("/")
        if (
            len(parts) != 3
            or parts[:2] != [str(course_id), str(assignment_id)]
            or any(p in {"", ".", ".."} for p in parts)
            or "\\" in relative
            or ":" in relative
            or PureWindowsPath(relative).is_absolute()
        ):
            raise AttachmentError("UNSAFE_PATH", "Caminho de anexo inválido.")
        check_name(parts[-1])
        if not re.fullmatch(r"[0-9]+-[0-9a-f]{64}-[A-Za-z0-9._ -]+", parts[-1]):
            raise AttachmentError("UNSAFE_PATH", "Arquivo não gerenciado pelo aplicativo.")
        return self._check(self.root.joinpath(*parts))

    def read(self, relative: str, course_id: int, assignment_id: int, expected: str) -> bytes:
        path = self.path(relative, course_id, assignment_id)
        if not path.is_file() or path.stat().st_size > self.max_bytes:
            raise AttachmentError("LOCAL_FILE", "Arquivo ausente ou acima do limite configurado.")
        with path.open("rb") as handle:
            data = handle.read(self.max_bytes + 1)
        if len(data) > self.max_bytes:
            raise AttachmentError("TOO_LARGE", "Arquivo excede o limite configurado.")
        if hashlib.sha256(data).hexdigest() != expected:
            raise AttachmentError(
                "HASH_MISMATCH", "Arquivo local alterado; repita fetch-attachments."
            )
        reject_active_bytes(data)
        return data

    def save(
        self,
        course_id: int,
        assignment_id: int,
        attachment_id: int,
        name: str,
        mime: str,
        data: bytes,
    ) -> tuple[str, str]:
        if min(course_id, assignment_id, attachment_id) < 1:
            raise AttachmentError("UNSAFE_PATH", "ID local inválido.")
        if len(data) > self.max_bytes:
            raise AttachmentError("TOO_LARGE", "Download excede o limite configurado.")
        reject_active_bytes(data)
        content_hash = hashlib.sha256(data).hexdigest()
        filename = f"{attachment_id}-{content_hash}-{safe_name(name)}{FORMATS[mime]}"
        relative = f"{course_id}/{assignment_id}/{filename}"
        path = self.path(relative, course_id, assignment_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._check(path)
        try:
            # Exclusive creation: never overwrite a different existing file.
            with path.open("xb") as handle:
                os.chmod(path, 0o600)
                handle.write(data)
        except FileExistsError:
            self.read(relative, course_id, assignment_id, content_hash)
        except OSError:
            # An interrupted write is never published in the DB.
            if path.exists():
                self._check(path).unlink()
            raise
        return relative, content_hash
