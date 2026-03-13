"""FastAPI application entrypoint."""

import json

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from app.file_parsers import parse_uploaded_files
from app.llm import run_reasoning
from app.schemas import EvidenceInput, ReasonRequest, ReasonResponse


app = FastAPI(title="Reasoning Interface Generator API")

# Keep CORS open for local frontend demo.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _parse_manual_evidences(raw: str) -> list[EvidenceInput]:
    """Deserialize manual evidences sent inside multipart form data."""

    if not raw.strip():
        return []
    parsed = json.loads(raw)
    return [EvidenceInput.model_validate(item) for item in parsed]


@app.get("/health")
async def health() -> dict:
    """Simple health endpoint."""

    return {"status": "ok"}


@app.post("/api/reason", response_model=ReasonResponse)
async def reason(payload: ReasonRequest) -> ReasonResponse:
    """Generate structured reasoning JSON from case text and question."""

    return await run_reasoning(payload.case_text, payload.question, payload.evidences)


@app.post("/api/reason-upload", response_model=ReasonResponse)
async def reason_upload(
    case_text: str = Form(...),
    question: str = Form(...),
    manual_evidences: str = Form("[]"),
    files: list[UploadFile] = File(default_factory=list),
) -> ReasonResponse:
    """Generate reasoning JSON from multipart uploads plus manual evidence."""

    manual_items = _parse_manual_evidences(manual_evidences)
    uploaded_items = await parse_uploaded_files(files)
    return await run_reasoning(case_text, question, manual_items + uploaded_items)
