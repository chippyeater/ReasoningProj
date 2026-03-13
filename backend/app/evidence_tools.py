"""Evidence parsing tools used to normalize uploaded evidence for the LLM."""

from app.schemas import EvidenceInput, ParsedEvidence


LLM_EVIDENCE_TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "parse_text_evidence",
            "description": "Normalize direct text evidence entered by a user.",
        },
    },
    {
        "type": "function",
        "function": {
            "name": "parse_document_evidence",
            "description": "Normalize document evidence such as pdf, doc, docx, txt, md.",
        },
    },
    {
        "type": "function",
        "function": {
            "name": "parse_image_evidence",
            "description": "Normalize image evidence by extracting OCR text or visual observations.",
        },
    },
    {
        "type": "function",
        "function": {
            "name": "parse_video_evidence",
            "description": "Normalize video evidence into a timeline, transcript, and key scenes.",
        },
    },
    {
        "type": "function",
        "function": {
            "name": "parse_audio_evidence",
            "description": "Normalize audio evidence into transcript and speaker cues.",
        },
    },
]


def _base_metadata(evidence: EvidenceInput) -> dict:
    """Keep common metadata across all parsed evidence."""

    return {
        "file_name": evidence.file_name,
        "mime_type": evidence.mime_type,
        "notes": evidence.notes,
    }


def parse_text_evidence(evidence: EvidenceInput) -> ParsedEvidence:
    """Normalize plain text evidence."""

    normalized_text = evidence.content.strip()
    metadata = _base_metadata(evidence)
    metadata["parse_status"] = "success"
    metadata["parser_detail"] = "Plain text content is available."
    return ParsedEvidence(
        id=evidence.id or evidence.name,
        type="text",
        name=evidence.name,
        parser_tool="parse_text_evidence",
        normalized_text=normalized_text,
        metadata=metadata,
    )


def parse_document_evidence(evidence: EvidenceInput) -> ParsedEvidence:
    """Normalize document evidence from extracted text provided by the caller."""

    normalized_text = (
        f"Document: {evidence.name}\n"
        f"Source file: {evidence.file_name or 'unknown'}\n"
        f"Extracted text:\n{evidence.content.strip()}"
    ).strip()
    metadata = _base_metadata(evidence)
    if "No extractable" in evidence.content or "Unsupported uploaded file type" in evidence.content:
        metadata["parse_status"] = "partial" if "No extractable" in evidence.content else "unsupported"
        metadata["parser_detail"] = evidence.content.strip()
    else:
        metadata["parse_status"] = "success"
        metadata["parser_detail"] = "Document text extracted and normalized."
    return ParsedEvidence(
        id=evidence.id or evidence.name,
        type="document",
        name=evidence.name,
        parser_tool="parse_document_evidence",
        normalized_text=normalized_text,
        metadata=metadata,
    )


def parse_image_evidence(evidence: EvidenceInput) -> ParsedEvidence:
    """Normalize image evidence from OCR text or analyst notes."""

    normalized_text = (
        f"Image: {evidence.name}\n"
        f"Observed text or scene notes:\n{evidence.content.strip() or '[no OCR/description provided]'}"
    )
    metadata = _base_metadata(evidence)
    metadata["parse_status"] = "partial" if "not enabled" in normalized_text else "success"
    metadata["parser_detail"] = (
        "Image metadata parsed, OCR not enabled yet."
        if metadata["parse_status"] == "partial"
        else "Image text or notes are available."
    )
    return ParsedEvidence(
        id=evidence.id or evidence.name,
        type="image",
        name=evidence.name,
        parser_tool="parse_image_evidence",
        normalized_text=normalized_text,
        metadata=metadata,
    )


def parse_video_evidence(evidence: EvidenceInput) -> ParsedEvidence:
    """Normalize video evidence from transcript or scene descriptions."""

    normalized_text = (
        f"Video: {evidence.name}\n"
        f"Transcript or scene timeline:\n{evidence.content.strip() or '[no transcript/scene description provided]'}"
    )
    metadata = _base_metadata(evidence)
    metadata["parse_status"] = "partial" if "[no transcript/scene description provided]" in normalized_text else "success"
    metadata["parser_detail"] = (
        "No video transcript or timeline was provided."
        if metadata["parse_status"] == "partial"
        else "Video transcript or scene notes are available."
    )
    return ParsedEvidence(
        id=evidence.id or evidence.name,
        type="video",
        name=evidence.name,
        parser_tool="parse_video_evidence",
        normalized_text=normalized_text,
        metadata=metadata,
    )


def parse_audio_evidence(evidence: EvidenceInput) -> ParsedEvidence:
    """Normalize audio evidence from transcript or analyst notes."""

    normalized_text = (
        f"Audio: {evidence.name}\n"
        f"Transcript or speaker notes:\n{evidence.content.strip() or '[no transcript provided]'}"
    )
    metadata = _base_metadata(evidence)
    metadata["parse_status"] = "partial" if "[no transcript provided]" in normalized_text else "success"
    metadata["parser_detail"] = (
        "No audio transcript was provided."
        if metadata["parse_status"] == "partial"
        else "Audio transcript or speaker notes are available."
    )
    return ParsedEvidence(
        id=evidence.id or evidence.name,
        type="audio",
        name=evidence.name,
        parser_tool="parse_audio_evidence",
        normalized_text=normalized_text,
        metadata=metadata,
    )


def parse_evidence(evidence: EvidenceInput) -> ParsedEvidence:
    """Dispatch to the correct evidence parser by type."""

    if evidence.type == "text":
        return parse_text_evidence(evidence)
    if evidence.type == "document":
        return parse_document_evidence(evidence)
    if evidence.type == "image":
        return parse_image_evidence(evidence)
    if evidence.type == "video":
        return parse_video_evidence(evidence)
    return parse_audio_evidence(evidence)


def build_evidence_context(evidences: list[EvidenceInput]) -> list[ParsedEvidence]:
    """Normalize all uploaded evidences before sending them to the LLM."""

    return [parse_evidence(item) for item in evidences]
