"""FastAPI entrypoint for the v0.3 upload and cached-document contract."""
from contextlib import asynccontextmanager
import json
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from app.api_models import (
    AgentRunInput,
    CaseResponse, CasesResponse, ConfirmInfoUnitAgentProposalInput, CreateInfoUnitInput,
    CreateRelationInput, ExtractionRunInput, InfoUnitAgentProposalResponse, InfoUnitAgentRequest,
    RelationRunInput, RelationRunResponse, UpdateCaseInput, UpdateInfoUnitInput, UpdateRelationInput,
)
from app.case_service import (
    append_files_to_case, build_extraction_input, create_case_from_uploads, create_empty_case,
    create_info_unit, delete_info_unit, list_quote_units, now_iso, update_info_unit,
)
from app.case_store import (
    clear_case, clear_current_case, list_cases, load_case, load_case_snapshot, load_current_case,
    read_parsed_document, save_case, set_current_case,
)
from app.errors import AppError
from app.extraction_service import run_extraction
from app.info_unit_agent import confirm_info_unit_proposal, generate_info_unit_proposal
from app.investigation_agent import run_investigation
from app.issue_workspaces import IssueWorkspacePatch, get_issue_workspaces, update_issue_workspace, investigation_context
from app.observability import flush as flush_observability
from app.quote_units import QuoteUnit
from app.relation_service import create_relation, delete_relation, run_relation, update_relation
from app.schemas import (
    CaseData, ExtractionInput, ExtractionOutput, LawRetrieveInput, ParsedDocument, ReasoningInput,
    UploadRequest, WorkspaceState,
)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    flush_observability()


app = FastAPI(title="LogicWeaver API", version="0.3", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(AppError)
async def application_error(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": {"code": exc.code, "message": str(exc), **exc.details}})


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
    return {"status": "ok", "schema_version": "0.3"}


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


@app.post("/api/cases/{case_id}/agent/run")
async def agent_run(case_id: str, payload: AgentRunInput) -> dict:
    case = require_case(case_id)
    context = investigation_context(case, payload.current_issue) if payload.current_issue is not None else None
    result = await run_investigation(case, payload.goal, payload.selected_info_unit_ids, work_context=context)
    units = {unit.id: unit for unit in case.info_units}
    files = {file.file_id: file for file in case.files}
    # Hydrate only validated, actually used pointers for the UI. Never model input.
    evidence = []
    for pointer in result["evidence_refs"]:
        ref = units[pointer["info_unit_id"]].source_refs[pointer["source_ref_index"]]
        evidence.append({**pointer, **ref.model_dump(mode="json"), "filename": files[ref.file_id].filename})
    return {**result, "evidence": evidence}


def require_multipart(request: Request) -> None:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "multipart/form-data":
        raise AppError("unsupported_media_type", "Expected multipart/form-data", 415)


def source_kinds_from_form(form) -> dict:
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate source_kinds filename")
            result[key] = value
        return result
    values = form.getlist("source_kinds")
    if len(values) != 1 or not isinstance(values[0], str):
        raise AppError("invalid_source_metadata", "Provide one source_kinds JSON object keyed by exact filename")
    try:
        kinds = json.loads(values[0], object_pairs_hook=unique_keys)
        if not isinstance(kinds, dict):
            raise ValueError("Expected a filename-to-source-kind object")
    except ValueError as exc:
        raise AppError("invalid_source_metadata", "source_kinds must be a JSON object with unique filename keys") from exc
    return kinds


@app.post("/api/cases", response_model=CaseResponse, status_code=201)
async def upload_case(request: Request) -> CaseResponse:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type == "application/json":
        try:
            payload = UpdateCaseInput.model_validate(await request.json())
        except json.JSONDecodeError as exc:
            raise AppError("invalid_json", "Request body must be valid JSON", 422) from exc
        case = create_empty_case(payload.case_title)
        save_case(case)
        return CaseResponse(case=case)
    require_multipart(request)
    async with request.form() as form:
        if set(form) - {"case_title", "files", "source_kinds"} or len(form.getlist("case_title")) != 1:
            raise AppError("invalid_upload", "Expected one case_title and one or more files")
        payload = UploadRequest(case_title=form.get("case_title"), files=form.getlist("files"),
                                source_kinds=source_kinds_from_form(form))
        case = await create_case_from_uploads(payload.case_title, payload.files, payload.source_kinds)
    return CaseResponse(case=case)


@app.post("/api/cases/{case_id}/files", response_model=CaseResponse)
async def append_case_files(case_id: str, request: Request) -> CaseResponse:
    require_multipart(request)
    case = require_case(case_id)
    async with request.form() as form:
        files = form.getlist("files")
        if set(form) != {"files", "source_kinds"} or not files or any(not isinstance(f, UploadFile) for f in files):
            raise AppError("invalid_upload", "Expected one or more files fields")
        case = await append_files_to_case(case, files, source_kinds_from_form(form))
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


@app.get("/api/cases/{case_id}/quote-units", response_model=list[QuoteUnit])
async def get_quote_units(case_id: str) -> list[QuoteUnit]:
    return list_quote_units(require_case(case_id))


@app.post("/api/cases/{case_id}/info-units", response_model=CaseResponse, status_code=201)
async def add_info_unit(case_id: str, payload: CreateInfoUnitInput) -> CaseResponse:
    return CaseResponse(case=create_info_unit(require_case(case_id), payload))


@app.patch("/api/cases/{case_id}/info-units/{info_unit_id}", response_model=CaseResponse)
async def edit_info_unit(case_id: str, info_unit_id: str, payload: UpdateInfoUnitInput) -> CaseResponse:
    return CaseResponse(case=update_info_unit(require_case(case_id), info_unit_id, payload))


@app.delete("/api/cases/{case_id}/info-units/{info_unit_id}", response_model=CaseResponse)
async def remove_info_unit(case_id: str, info_unit_id: str) -> CaseResponse:
    return CaseResponse(case=delete_info_unit(require_case(case_id), info_unit_id))


@app.post("/api/cases/{case_id}/relations", response_model=CaseResponse, status_code=201)
async def add_relation(case_id: str, payload: CreateRelationInput) -> CaseResponse:
    return CaseResponse(case=create_relation(require_case(case_id), payload))


@app.patch("/api/cases/{case_id}/relations/{relation_id}", response_model=CaseResponse)
async def edit_relation(case_id: str, relation_id: str, payload: UpdateRelationInput) -> CaseResponse:
    return CaseResponse(case=update_relation(require_case(case_id), relation_id, payload))


@app.delete("/api/cases/{case_id}/relations/{relation_id}", response_model=CaseResponse)
async def remove_relation(case_id: str, relation_id: str) -> CaseResponse:
    return CaseResponse(case=delete_relation(require_case(case_id), relation_id))


@app.post(
    "/api/cases/{case_id}/info-unit-agent/proposals",
    response_model=InfoUnitAgentProposalResponse,
)
async def propose_info_unit(case_id: str, payload: InfoUnitAgentRequest) -> InfoUnitAgentProposalResponse:
    return await generate_info_unit_proposal(require_case(case_id), payload)


@app.post(
    "/api/cases/{case_id}/info-unit-agent/proposals/confirm",
    response_model=CaseResponse,
)
async def confirm_proposed_info_unit(
        case_id: str, payload: ConfirmInfoUnitAgentProposalInput) -> CaseResponse:
    return CaseResponse(case=confirm_info_unit_proposal(require_case(case_id), payload))


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


@app.get("/api/cases/{case_id}/issue-workspaces")
async def read_issue_workspaces(case_id: str):
    return get_issue_workspaces(require_case(case_id))


@app.patch("/api/cases/{case_id}/issue-workspaces")
async def patch_issue_workspace(case_id: str, payload: IssueWorkspacePatch):
    return update_issue_workspace(require_case(case_id), payload)


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
    raise AppError("pipeline_not_implemented", f"v0.3 {stage} execution is not connected", 501)


@app.post("/api/extraction/run", response_model=ExtractionOutput)
async def extraction_run(payload: ExtractionRunInput) -> ExtractionOutput:
    case = load_case_snapshot(payload.case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return await run_extraction(case, workflow_id=payload.workflow_id)


@app.post("/api/relation/run", response_model=RelationRunResponse)
async def relation_run(payload: RelationRunInput) -> RelationRunResponse:
    case = load_case_snapshot(payload.case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return await run_relation(case, payload.selected_info_unit_ids, workflow_id=payload.workflow_id)


@app.post("/api/reasoning/run", status_code=501)
async def reasoning_run(payload: ReasoningInput) -> None:
    require_case(payload.case_id)
    pipeline_unavailable("view selection and composition")


@app.post("/api/law/retrieve", status_code=501)
async def law_retrieve(payload: LawRetrieveInput) -> None:
    require_case(payload.case_id)
    pipeline_unavailable("law retrieval")
