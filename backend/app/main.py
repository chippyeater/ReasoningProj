"""FastAPI entrypoint for the v0.2 upload and cached-document contract."""
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from app.api_models import (
    CaseResponse, CasesResponse, ExtractionRunInput, RelationRunInput, UpdateCaseInput,
)
from app.case_service import (
    append_files_to_case, build_extraction_input, create_case_from_uploads, now_iso,
)
from app.case_store import (
    clear_case, clear_current_case, list_cases, load_case, load_current_case,
    read_parsed_document, save_case, set_current_case,
)
from app.errors import AppError
from app.schemas import (
    CaseData, ExtractionInput, LawRetrieveInput, ParsedDocument, ReasoningInput, UploadRequest, WorkspaceState,
)

app = FastAPI(title="LogicWeaver API", version="0.2")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(AppError)
async def application_error(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": {"code": exc.code, "message": str(exc)}})


@app.exception_handler(OSError)
async def storage_error(request: Request, exc: OSError) -> JSONResponse:
    return JSONResponse(status_code=500, content={"detail": {
        "code": "storage_error", "message": "Unable to read or persist case artifacts"}})


@app.exception_handler(ValidationError)
async def contract_error(request: Request, exc: ValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": {
        "code": "invalid_contract", "message": str(exc)}})


def require_case(case_id: str) -> CaseData:
    case = load_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return case


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "schema_version": "0.2"}


@app.get("/api/cases", response_model=CasesResponse)
async def get_cases() -> CasesResponse:
    current = load_current_case()
    return CasesResponse(cases=list_cases(), current_case_id=current.case_id if current else None)


@app.get("/api/case", response_model=CaseResponse)
async def get_current_case() -> CaseResponse:
    return CaseResponse(case=load_current_case())


@app.get("/api/cases/{case_id}", response_model=CaseResponse)
async def get_case(case_id: str) -> CaseResponse:
    return CaseResponse(case=require_case(case_id))


def require_multipart(request: Request) -> None:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "multipart/form-data":
        raise AppError("unsupported_media_type", "Expected multipart/form-data", 415)


@app.post("/api/cases", response_model=CaseResponse, status_code=201)
async def upload_case(request: Request) -> CaseResponse:
    require_multipart(request)
    async with request.form() as form:
        if set(form) - {"case_title", "files"} or len(form.getlist("case_title")) != 1:
            raise AppError("invalid_upload", "Expected one case_title and one or more files")
        payload = UploadRequest(case_title=form.get("case_title"), files=form.getlist("files"))
        case = await create_case_from_uploads(payload.case_title, payload.files)
    return CaseResponse(case=case)


@app.post("/api/cases/{case_id}/files", response_model=CaseResponse)
async def append_case_files(case_id: str, request: Request) -> CaseResponse:
    require_multipart(request)
    case = require_case(case_id)
    async with request.form() as form:
        files = form.getlist("files")
        if set(form) != {"files"} or not files or any(not isinstance(f, UploadFile) for f in files):
            raise AppError("invalid_upload", "Expected one or more files fields")
        case = await append_files_to_case(case, files)
    return CaseResponse(case=case)


@app.get("/api/cases/{case_id}/files/{file_id}/text", response_model=ParsedDocument)
async def get_parsed_text(case_id: str, file_id: str) -> ParsedDocument:
    case = require_case(case_id)
    file = next((f for f in case.files if f.file_id == file_id), None)
    if file is None:
        raise HTTPException(status_code=404, detail="File not found")
    return read_parsed_document(case_id, file)


@app.get("/api/cases/{case_id}/extraction-input", response_model=ExtractionInput)
async def get_extraction_input(case_id: str, file_id: Annotated[list[str] | None, Query()] = None) -> ExtractionInput:
    return build_extraction_input(require_case(case_id), file_id)


@app.post("/api/cases/{case_id}/select", response_model=CaseResponse)
async def select_case(case_id: str) -> CaseResponse:
    if not set_current_case(case_id):
        raise HTTPException(status_code=404, detail="Case not found")
    return CaseResponse(case=require_case(case_id))


@app.patch("/api/cases/{case_id}", response_model=CaseResponse)
async def update_case(case_id: str, payload: UpdateCaseInput) -> CaseResponse:
    case = require_case(case_id)
    case.case_title = payload.case_title
    case.updated_at = now_iso()
    save_case(case)
    return CaseResponse(case=case)


@app.patch("/api/cases/{case_id}/workspace", response_model=CaseResponse)
async def update_workspace(case_id: str, payload: WorkspaceState) -> CaseResponse:
    case = require_case(case_id)
    case.workspace_state = payload
    case.updated_at = now_iso()
    save_case(case)
    return CaseResponse(case=case)


@app.delete("/api/cases/{case_id}")
async def delete_case(case_id: str) -> dict:
    require_case(case_id)
    clear_case(case_id)
    return {"status": "deleted", "case_id": case_id}


@app.delete("/api/case")
async def delete_current_case() -> dict:
    clear_current_case()
    return {"status": "cleared"}


def pipeline_unavailable(stage: str) -> None:
    raise AppError("pipeline_not_implemented", f"v0.2 {stage} execution is not connected", 501)


@app.post("/api/extraction/run", status_code=501)
async def extraction_run(payload: ExtractionRunInput) -> None:
    build_extraction_input(require_case(payload.case_id), payload.file_ids)
    pipeline_unavailable("extraction")


@app.post("/api/relation/run", status_code=501)
async def relation_run(payload: RelationRunInput) -> None:
    require_case(payload.case_id)
    pipeline_unavailable("relation")


@app.post("/api/reasoning/run", status_code=501)
async def reasoning_run(payload: ReasoningInput) -> None:
    require_case(payload.case_id)
    pipeline_unavailable("view selection and composition")


@app.post("/api/law/retrieve", status_code=501)
async def law_retrieve(payload: LawRetrieveInput) -> None:
    require_case(payload.case_id)
    pipeline_unavailable("law retrieval")
