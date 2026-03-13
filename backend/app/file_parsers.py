"""Parse uploaded files into normalized evidence inputs."""

from io import BytesIO
from pathlib import Path

from docx import Document
from fastapi import UploadFile
from PIL import Image
from pypdf import PdfReader

from app.schemas import EvidenceInput


def _detect_text_content(raw: bytes) -> str:
    """Decode plain text with a few practical fallbacks."""

    for encoding in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def _parse_txt_file(file_name: str, raw: bytes, mime_type: str) -> EvidenceInput:
    """Parse plain text-like files."""

    return EvidenceInput(
        id=f"file-{file_name}",
        type="text",
        name=file_name,
        file_name=file_name,
        mime_type=mime_type,
        content=_detect_text_content(raw),
        notes="Parsed from uploaded text file.",
    )


def _parse_pdf_file(file_name: str, raw: bytes, mime_type: str) -> EvidenceInput:
    """Extract text from PDF pages."""

    reader = PdfReader(BytesIO(raw))
    extracted_pages = []
    for index, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            extracted_pages.append(f"[Page {index}]\n{text}")

    content = "\n\n".join(extracted_pages).strip() or "[No extractable PDF text found]"
    return EvidenceInput(
        id=f"file-{file_name}",
        type="document",
        name=file_name,
        file_name=file_name,
        mime_type=mime_type,
        content=content,
        notes="Parsed from uploaded PDF file.",
    )


def _parse_docx_file(file_name: str, raw: bytes, mime_type: str) -> EvidenceInput:
    """Extract paragraph text from DOCX."""

    document = Document(BytesIO(raw))
    paragraphs = [paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip()]
    content = "\n".join(paragraphs).strip() or "[No extractable DOCX text found]"
    return EvidenceInput(
        id=f"file-{file_name}",
        type="document",
        name=file_name,
        file_name=file_name,
        mime_type=mime_type,
        content=content,
        notes="Parsed from uploaded DOCX file.",
    )


def _parse_image_file(file_name: str, raw: bytes, mime_type: str) -> EvidenceInput:
    """Extract image metadata as a minimal, dependency-safe parse result."""

    with Image.open(BytesIO(raw)) as image:
        content = (
            f"Image file: {file_name}\n"
            f"Format: {image.format}\n"
            f"Size: {image.width}x{image.height}\n"
            f"Mode: {image.mode}\n"
            "OCR text: [not enabled in this minimal backend yet]"
        )

    return EvidenceInput(
        id=f"file-{file_name}",
        type="image",
        name=file_name,
        file_name=file_name,
        mime_type=mime_type,
        content=content,
        notes="Parsed from uploaded image file.",
    )


async def parse_uploaded_file(upload: UploadFile) -> EvidenceInput:
    """Parse one uploaded file into the shared EvidenceInput schema."""

    file_name = upload.filename or "uploaded-file"
    mime_type = upload.content_type or "application/octet-stream"
    suffix = Path(file_name).suffix.lower()
    raw = await upload.read()

    if suffix in {".txt", ".md"}:
        return _parse_txt_file(file_name, raw, mime_type)
    if suffix == ".pdf":
        return _parse_pdf_file(file_name, raw, mime_type)
    if suffix == ".docx":
        return _parse_docx_file(file_name, raw, mime_type)
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp"} or mime_type.startswith("image/"):
        return _parse_image_file(file_name, raw, mime_type)

    return EvidenceInput(
        id=f"file-{file_name}",
        type="document",
        name=file_name,
        file_name=file_name,
        mime_type=mime_type,
        content=f"[Unsupported uploaded file type: {suffix or mime_type}]",
        notes="Upload accepted but parser is not implemented for this file type.",
    )


async def parse_uploaded_files(files: list[UploadFile]) -> list[EvidenceInput]:
    """Parse all uploaded files into evidence inputs."""

    return [await parse_uploaded_file(item) for item in files]
