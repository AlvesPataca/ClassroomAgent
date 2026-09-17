"""Data-only parsing. No Office automation, external links, code execution or OCR."""

import io
import logging
import multiprocessing
import re
import zipfile
from dataclasses import asdict, dataclass, field
from multiprocessing.connection import Connection
from typing import Any, Literal

from defusedxml.ElementTree import fromstring
from pypdf import PdfReader

from app.attachments.safety import (
    DOCX,
    PPTX,
    XLSX,
    AttachmentError,
    check_name,
    reject_active_bytes,
)
from app.config import Settings


@dataclass(frozen=True)
class TextSection:
    label: str
    text: str


@dataclass(frozen=True)
class ExtractedContent:
    status: str
    sections: list[TextSection] = field(default_factory=list)
    characters: int = 0
    pages: int | None = None
    error_code: str | None = None
    trust: Literal["UNTRUSTED_DATA"] = "UNTRUSTED_DATA"
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def preflight_zip(data: bytes, limit: int) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > 5000 or sum(e.file_size for e in entries) > limit:
            raise AttachmentError("TOO_LARGE", "Documento compactado excede limite de extração.")
        seen: set[str] = set()
        for entry in entries:
            name = entry.filename
            check_name(name)
            if (
                name.startswith(("/", "\\"))
                or "\\" in name
                or ":" in name
                or ".." in name.split("/")
                or name in seen
            ):
                raise AttachmentError("UNSAFE_ARCHIVE", "Estrutura ZIP não permitida.")
            if "vbaproject" in name.lower() or "/embeddings/" in name.lower():
                raise AttachmentError("BLOCKED", "Macros/objetos incorporados bloqueados.")
            if entry.flag_bits & 1:
                raise AttachmentError("ENCRYPTED", "Documento protegido não suportado.")
            seen.add(name)


class ContentExtractor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def extract(self, data: bytes, mime: str) -> ExtractedContent:
        """Pure parsing entry point; application calls extract_isolated for a time limit."""
        sections: list[TextSection] = []
        total = 0
        pages: int | None = None

        def add(label: str, text: str) -> None:
            nonlocal total
            total += len(text)
            if total > self.settings.extraction_max_chars:
                raise AttachmentError("TOO_LARGE", "Texto excede EXTRACTION_MAX_CHARS.")
            sections.append(TextSection(label, text))

        try:
            if len(data) > min(
                self.settings.attachment_max_bytes, self.settings.extraction_max_bytes
            ):
                raise AttachmentError("TOO_LARGE", "Arquivo excede limite de extração.")
            reject_active_bytes(data)
            if mime in {"text/plain", "text/markdown"}:
                encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
                text = data.decode(encoding)
                if "\x00" in text:
                    raise ValueError("Binary text")
                add("Documento", text)
            elif mime == "application/pdf":
                # pypdf never executes JavaScript/actions. Disable document-controlled logs.
                logging.getLogger("pypdf").disabled = True
                logging.getLogger("pypdf").addHandler(logging.NullHandler())
                logging.getLogger("pypdf").propagate = False
                from pypdf import overwrite_configuration

                limit = self.settings.extraction_max_bytes
                overwrite_configuration(
                    maximum_declared_stream_length=limit,
                    array_based_stream_maximum_output_length=limit,
                    zlib_maximum_output_length=limit,
                    lzw_maximum_output_length=limit,
                    run_length_maximum_output_length=limit,
                    image_maximum_buffer_size=limit,
                    jbig2dec_binary=None,
                    page_tree_maximum_entries=2000,
                )
                reader = PdfReader(io.BytesIO(data), strict=True)
                if reader.is_encrypted:
                    raise AttachmentError("ENCRYPTED", "PDF protegido não suportado.")
                pages = len(reader.pages)
                if pages > 2000:
                    raise AttachmentError("TOO_LARGE", "PDF excede o limite de 2000 páginas.")
                for index, page in enumerate(reader.pages, 1):
                    add(f"Página {index}", page.extract_text() or "")
                if not any(s.text.strip() for s in sections):
                    return ExtractedContent("UNSUPPORTED", pages=pages, error_code="NO_TEXT_LAYER")
            elif mime in {DOCX, PPTX, XLSX}:
                preflight_zip(data, self.settings.extraction_max_bytes)
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    if mime == DOCX:
                        tree = fromstring(archive.read("word/document.xml"))
                        for index, paragraph in enumerate(tree.iter(), 1):
                            if paragraph.tag.endswith("}p"):
                                pieces = []
                                for node in paragraph.iter():
                                    tag = node.tag.rsplit("}", 1)[-1]
                                    if tag == "t":
                                        pieces.append(node.text or "")
                                    elif tag in {"tab", "br", "cr"}:
                                        pieces.append("\t" if tag == "tab" else "\n")
                                add(f"Parágrafo {index}", "".join(pieces))
                    elif mime == PPTX:
                        names = sorted(
                            (
                                n
                                for n in archive.namelist()
                                if re.fullmatch(r"ppt/slides/slide[0-9]+\.xml", n)
                            ),
                            key=lambda n: int(
                                n.removeprefix("ppt/slides/slide").removesuffix(".xml")
                            ),
                        )
                        if not names:
                            raise ValueError("Missing slides")
                        pages = len(names)
                        for index, name in enumerate(names, 1):
                            tree = fromstring(archive.read(name))
                            add(
                                f"Slide {index}",
                                "\n".join(
                                    node.text or ""
                                    for node in tree.iter()
                                    if node.tag.endswith("}t")
                                ),
                            )
                    else:
                        self._sheets(archive, add)
            else:
                return ExtractedContent("UNSUPPORTED", error_code="UNSUPPORTED_MIME")
            return ExtractedContent("EXTRACTED", sections, total, pages)
        except AttachmentError as exc:
            return ExtractedContent(
                "UNSUPPORTED" if exc.unsupported else "FAILED", error_code=exc.code
            )
        except Exception:
            # Library errors may contain document bytes: never persist or print them.
            return ExtractedContent("FAILED", error_code="CORRUPT_DOCUMENT")

    def _sheets(self, archive: zipfile.ZipFile, add: Any) -> None:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared = [
                "".join(n.text or "" for n in si.iter() if n.tag.endswith("}t"))
                for si in fromstring(archive.read("xl/sharedStrings.xml"))
            ]
        names = sorted(
            n for n in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet[0-9]+\.xml", n)
        )
        if not names:
            raise ValueError("Missing sheets")
        for index, name in enumerate(names, 1):
            tree = fromstring(archive.read(name))
            for row in tree.iter():
                if not row.tag.endswith("}row"):
                    continue
                values = []
                for cell in row:
                    value = ""
                    for node in cell.iter():
                        if node.tag.endswith(("}v", "}t")):
                            value += node.text or ""
                    if cell.get("t") == "s" and value:
                        value = shared[int(value)]
                    # Formulas are never evaluated. Cached results only, with cell coordinates.
                    values.append(f"{cell.get('r', '?')}: {value}")
                add(f"Planilha {index}, linha {row.get('r', '?')}", "\t".join(values))


def _worker(connection: Connection, data: bytes, mime: str, settings: Settings) -> None:
    try:
        logging.disable(logging.CRITICAL)
        connection.send(ContentExtractor(settings).extract(data, mime))
    finally:
        connection.close()


def extract_isolated(data: bytes, mime: str, settings: Settings) -> ExtractedContent:
    """Only the trusted parser is run in a child process; input is bytes, never a command/path."""
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=_worker, args=(send, data, mime, settings), daemon=True)
    try:
        process.start()
        send.close()
        if not receive.poll(settings.extraction_timeout_seconds):
            return ExtractedContent("FAILED", error_code="EXTRACTION_TIMEOUT")
        result = receive.recv()
        if not isinstance(result, ExtractedContent):
            return ExtractedContent("FAILED", error_code="CORRUPT_DOCUMENT")
        return result
    except (EOFError, OSError):
        return ExtractedContent("FAILED", error_code="PARSER_FAILED")
    finally:
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
            process.close()
        receive.close()
        send.close()
