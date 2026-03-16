"""FastAPI application entrypoint."""

import json

from fastapi import FastAPI, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from app.file_parsers import parse_uploaded_files
from app.llm import run_reasoning
from app.log_store import save_reason_response
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
async def reason(request: Request) -> ReasonResponse:
    """Generate reasoning JSON from JSON or multipart form data."""

    content_type = request.headers.get("content-type", "").lower()

    if "multipart/form-data" in content_type:
        form = await request.form()
        case_text = str(form.get("case_text", ""))
        question = str(form.get("question", ""))
        manual_items = _parse_manual_evidences(str(form.get("manual_evidences", "[]")))
        files = [item for _, item in form.multi_items() if isinstance(item, UploadFile)]
        uploaded_items = await parse_uploaded_files(files)
        response = await run_reasoning(case_text, question, manual_items + uploaded_items)
    else:
        payload = ReasonRequest.model_validate(await request.json())
        response = await run_reasoning(payload.case_text, payload.question, payload.evidences)

    save_reason_response(response)
    return response
