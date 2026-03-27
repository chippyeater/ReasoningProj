"""FastAPI application entrypoint."""

import json
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.case_service import (
    create_case_from_uploads,
    create_empty_case,
    generate_inference_with_llm,
    update_card,
    update_workspace,
    upsert_case_from_payload,
)
from app.case_store import (
    clear_case,
    clear_current_case,
    list_cases,
    load_case,
    load_current_case,
    save_case,
    set_current_case,
)
from app.schemas import (
    CaseData,
    GenerateInferenceRequest,
    GenerateInferenceResponse,
    GetCaseResponse,
    ListCasesResponse,
    UpdateCardRequest,
    UpdateCaseRequest,
    UpdateWorkspaceRequest,
    UpsertCaseResponse,
)


class UpsertCasePayload(BaseModel):
    case: CaseData


app = FastAPI(title="Reasoning Interface Generator API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/api/cases", response_model=ListCasesResponse)
async def list_cases_api() -> ListCasesResponse:
    current_case = load_current_case()
    current_id = current_case.case_id if current_case is not None else None
    return ListCasesResponse(cases=list_cases(), current_case_id=current_id)


@app.get("/api/case", response_model=GetCaseResponse)
async def get_current_case() -> GetCaseResponse:
    return GetCaseResponse(case=load_current_case())


@app.get("/api/cases/{case_id}", response_model=GetCaseResponse)
async def get_case_by_id(case_id: str) -> GetCaseResponse:
    case = load_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return GetCaseResponse(case=case)


@app.post("/api/cases", response_model=UpsertCaseResponse)
async def create_or_upsert_case(request: Request) -> UpsertCaseResponse:
    content_type = request.headers.get("content-type", "").lower()

    if "multipart/form-data" in content_type:
        form = await request.form()
        case_title = str(form.get("case_title", "未命名案件"))
        files = [item for _, item in form.multi_items() if isinstance(item, (UploadFile, StarletteUploadFile))]
        case = await create_case_from_uploads(case_title=case_title, files=files)
        save_case(case, set_current=True)
        return UpsertCaseResponse(success=True, case=case)

    payload_raw = await request.json()
    if isinstance(payload_raw, dict) and "case" in payload_raw:
        payload = UpsertCasePayload.model_validate(payload_raw)
        existing = load_case(payload.case.case_id)
        case = upsert_case_from_payload(existing, payload.case)
        save_case(case, set_current=True)
        return UpsertCaseResponse(success=True, case=case)

    case_title = str(payload_raw.get("case_title", "未命名案件")) if isinstance(payload_raw, dict) else "未命名案件"
    case_id = payload_raw.get("case_id") if isinstance(payload_raw, dict) else None
    case = create_empty_case(case_title=case_title, case_id=case_id)
    save_case(case, set_current=True)
    return UpsertCaseResponse(success=True, case=case)


@app.post("/api/cases/{case_id}/select", response_model=GetCaseResponse)
async def select_case(case_id: str) -> GetCaseResponse:
    if not set_current_case(case_id):
        raise HTTPException(status_code=404, detail="Case not found")
    return GetCaseResponse(case=load_current_case())


@app.patch("/api/cases/{case_id}", response_model=UpsertCaseResponse)
async def patch_case(case_id: str, payload: UpdateCaseRequest) -> UpsertCaseResponse:
    case = load_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")

    title = payload.case_title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="case_title is required")

    case.case_title = title
    case.updated_at = datetime.utcnow()
    save_case(case, set_current=True)
    return UpsertCaseResponse(success=True, case=case)


@app.delete("/api/case")
async def delete_current_case() -> dict:
    clear_current_case()
    return {"status": "cleared"}


@app.delete("/api/cases/{case_id}")
async def delete_case(case_id: str) -> dict:
    clear_case(case_id)
    return {"status": "deleted", "case_id": case_id}


@app.patch("/api/cases/{case_id}/cards/{card_id}", response_model=UpsertCaseResponse)
async def patch_card(case_id: str, card_id: str, payload: UpdateCardRequest) -> UpsertCaseResponse:
    case = load_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")

    case, updated = update_card(case, card_id, payload)
    if not updated:
        raise HTTPException(status_code=404, detail="Card not found")

    save_case(case, set_current=True)
    return UpsertCaseResponse(success=True, case=case)


@app.patch("/api/cases/{case_id}/workspace", response_model=UpsertCaseResponse)
async def patch_workspace(case_id: str, payload: UpdateWorkspaceRequest) -> UpsertCaseResponse:
    case = load_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")

    case = update_workspace(case, payload)
    save_case(case, set_current=True)
    return UpsertCaseResponse(success=True, case=case)


@app.post("/api/inference/generate", response_model=GenerateInferenceResponse)
async def post_generate_inference(payload: GenerateInferenceRequest) -> GenerateInferenceResponse:
    case = load_case(payload.case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")

    new_cards, new_edges, workspace, gen_message = await generate_inference_with_llm(case, payload)
    case.inference_cards.extend(new_cards)
    case.edges.extend(new_edges)
    case.workspace_state = workspace
    save_case(case, set_current=True)

    return GenerateInferenceResponse(
        success=True,
        new_inference_cards=new_cards,
        new_edges=new_edges,
        updated_workspace_state=workspace,
        message=gen_message,
    )


