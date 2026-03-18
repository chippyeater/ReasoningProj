"""FastAPI application entrypoint."""

import json
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.case_store import clear_current_case, load_current_case, save_current_case
from app.file_parsers import parse_uploaded_files
from app.llm import answer_case_question, build_current_case
from app.log_store import save_log_payload
from app.schemas import (
    CaseCreateRequest,
    CaseCreateResponse,
    CaseQuestionResponse,
    CurrentCaseEnvelope,
    EvidenceInput,
    QuestionReasonRequest,
)


app = FastAPI(title="Reasoning Interface Generator API")

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


@app.post("/api/case", response_model=CaseCreateResponse)
async def create_case(request: Request) -> CaseCreateResponse:
    """Create or replace the current case from JSON or multipart form data."""

    content_type = request.headers.get("content-type", "").lower()

    if "multipart/form-data" in content_type:
        form = await request.form()
        case_text = str(form.get("case_text", ""))
        manual_items = _parse_manual_evidences(str(form.get("manual_evidences", "[]")))
        files = [item for _, item in form.multi_items() if isinstance(item, (UploadFile, StarletteUploadFile))]
        uploaded_items = await parse_uploaded_files(files)
        evidences = manual_items + uploaded_items
    else:
        payload = CaseCreateRequest.model_validate(await request.json())
        case_text = payload.case_text
        evidences = payload.evidences

    current_case = await build_current_case(case_text, evidences, case_id=f"case-{uuid4().hex[:8]}")
    save_current_case(current_case)
    response = CaseCreateResponse(case=current_case)
    save_log_payload("case-create", response)
    return response


@app.get("/api/case", response_model=CurrentCaseEnvelope)
async def get_case() -> CurrentCaseEnvelope:
    """Return the current case if one exists."""

    return CurrentCaseEnvelope(case=load_current_case())


@app.delete("/api/case")
async def delete_case() -> dict:
    """Clear the current case."""

    clear_current_case()
    return {"status": "cleared"}


@app.post("/api/case/question", response_model=CaseQuestionResponse)
async def ask_case_question(payload: QuestionReasonRequest) -> CaseQuestionResponse:
    """Answer a question against the current case."""

    current_case = load_current_case()
    if current_case is None:
        raise HTTPException(status_code=404, detail="No current case. Submit evidence first.")

    response = await answer_case_question(current_case, payload.question)
    save_log_payload("case-question", response)
    return response
