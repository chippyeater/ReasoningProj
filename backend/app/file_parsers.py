"""Parse once on upload. Only successful text may enter the cached pipeline."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader

from app.errors import AppError
from app.schemas import FileType

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff"}


def file_type_for(filename: str, mime_type: str | None = None) -> FileType:
    extension = Path(filename).suffix.lower()
    types = {".pdf": FileType.pdf, ".docx": FileType.docx, ".txt": FileType.txt,
             ".md": FileType.markdown, ".markdown": FileType.markdown}
    if extension in types:
        return types[extension]
    if extension in IMAGE_EXTENSIONS or (mime_type or "").startswith("image/"):
        return FileType.image
    return FileType.other


def _docx_text(raw: bytes) -> str:
    document = Document(BytesIO(raw))
    blocks: list[str] = []
    # Include tables where they occur, rather than moving them after paragraphs.
    for block in document.iter_inner_content():
        if isinstance(block, Paragraph):
            blocks.append(block.text)
        elif isinstance(block, Table):
            blocks.extend("\t".join(cell.text for cell in row.cells) for row in block.rows)
    # Canonical DOCX text: retain nonempty paragraph order and one separator.
    # Empty layout paragraphs and edge whitespace carry no source characters.
    # This is the fixed case-A TXT normalization, also applied to new documents.
    return "\n".join(block.strip() for block in blocks if block.strip())


def parse_text(file_type: FileType, raw: bytes) -> str:
    if file_type in {FileType.image, FileType.other}:
        raise AppError("unsupported_file", f"Text extraction is not supported for {file_type.value}")
    try:
        if file_type in {FileType.txt, FileType.markdown}:
            text = raw.decode("utf-8-sig")
        elif file_type == FileType.docx:
            text = _docx_text(raw)
        else:
            reader = PdfReader(BytesIO(raw))
            pages = []
            for index, page in enumerate(reader.pages, 1):
                content = page.extract_text() or ""
                if content.strip():
                    pages.append(f"[Page {index}]\n{content}")
            text = "\n\n".join(pages)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if not text.strip():
            raise ValueError("No extractable text")
        if "\x00" in text:
            raise ValueError("NUL characters are not valid parsed text")
        return text
    except Exception as exc:
        raise AppError("parse_failed", f"{file_type.value} parsing failed: {exc}") from exc
