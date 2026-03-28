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
    route_task,
    run_extraction_agent,
    run_interaction_agent,
    track_interaction_event,
    run_qa_agent,
    run_reasoning_agent,
    run_relation_agent,
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
    ExtractionRequest,
    ExtractionResponse,
    GenerateInferenceRequest,
    GenerateInferenceResponse,
    GetCaseResponse,
    InteractionRequest,
    InteractionResponse,
    InteractionTrackRequest,
    InteractionTrackResponse,
    ListCasesResponse,
    QARequest,
    QAResponse,
    ReasoningRequest,
    ReasoningResponse,
    RelationRequest,
    RelationResponse,
    RouteRequest,
    RouteResponse,
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
# 异步接口，FastAPI会await这个函数的执行结果，request是完整http请求对象，包含了请求的所有信息
# -> UpsertCaseResponse是这个接口的返回类型
async def create_or_upsert_case(request: Request) -> UpsertCaseResponse:
    content_type = request.headers.get("content-type", "").lower()

    # 分支 A：multipart/form-data 表单上传类型
    if "multipart/form-data" in content_type:
        form = await request.form()  # 解析表单数据，form是一个包含了表单字段和文件的对象，可以通过form.get()获取字段值，通过form.multi_items()获取所有字段和文件
        case_title = str(form.get("case_title", "未命名案件"))
        files = [item for _, item in form.multi_items() if isinstance(item, (UploadFile, StarletteUploadFile))]
        case = await create_case_from_uploads(case_title=case_title, files=files)  # 根据上传的文件创建案件，case是一个案件对象，包含了案件的所有信息
        save_case(case, set_current=True)  # 将案件保存到存储中，并设置为当前案件
        return UpsertCaseResponse(success=True, case=case)

    # 分支 B：application/json 类型的请求体，直接解析为JSON对象
    payload_raw = await request.json()
    if isinstance(payload_raw, dict) and "case" in payload_raw:
        payload = UpsertCasePayload.model_validate(payload_raw)
        existing = load_case(payload.case.case_id)
        case = upsert_case_from_payload(existing, payload.case)
        save_case(case, set_current=True)
        return UpsertCaseResponse(success=True, case=case)

    # 默认分支：如果不是上述两种情况，尝试从JSON中提取case_title和case_id来创建一个空案件
    case_title = str(payload_raw.get("case_title", "未命名案件")) if isinstance(payload_raw, dict) else "未命名案件"
    case_id = payload_raw.get("case_id") if isinstance(payload_raw, dict) else None
    case = create_empty_case(case_title=case_title, case_id=case_id)
    save_case(case, set_current=True)
    return UpsertCaseResponse(success=True, case=case)



@app.post("/api/cases/{case_id}/files", response_model=UpsertCaseResponse)
async def append_files(case_id: str, request: Request) -> UpsertCaseResponse:
    content_type = request.headers.get("content-type", "").lower()
    if "multipart/form-data" not in content_type:
        raise HTTPException(status_code=400, detail="Expected multipart/form-data")

    case = load_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")

    form = await request.form()
    files = [item for _, item in form.multi_items() if isinstance(item, (UploadFile, StarletteUploadFile))]
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    # Append new files to existing case and re-run extraction.
    from app.case_service import append_files_to_case

    case = await append_files_to_case(case, files)
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





@app.post("/api/router/route", response_model=RouteResponse)
async def post_route(payload: RouteRequest) -> RouteResponse:
    case = load_current_case()
    if case is None:
        raise HTTPException(status_code=404, detail="No active case")

    response = await route_task(case, payload)
    save_case(case, set_current=True)
    return response


@app.post("/api/extraction/run", response_model=ExtractionResponse)
async def post_extraction(payload: ExtractionRequest) -> ExtractionResponse:
    case = load_case(payload.case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")

    units = await run_extraction_agent(case, payload)
    save_case(case, set_current=True)
    return ExtractionResponse(success=True, info_units=units, message="extraction-complete")


@app.post("/api/relation/run", response_model=RelationResponse)
async def post_relation(payload: RelationRequest) -> RelationResponse:
    case = load_case(payload.case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")

    relations, edges = await run_relation_agent(case, payload)
    save_case(case, set_current=True)
    return RelationResponse(success=True, relations=relations, new_edges=edges, message="relation-complete")


@app.post("/api/reasoning/run", response_model=ReasoningResponse)
async def post_reasoning(payload: ReasoningRequest) -> ReasoningResponse:
    case = load_case(payload.case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")

    response = await run_reasoning_agent(case, payload)
    save_case(case, set_current=True)
    return response


@app.post("/api/qa/ask", response_model=QAResponse)
async def post_qa(payload: QARequest) -> QAResponse:
    case = load_case(payload.case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")

    response = await run_qa_agent(case, payload)
    save_case(case, set_current=True)
    return response


@app.post("/api/interaction/act", response_model=InteractionResponse)
async def post_interaction(payload: InteractionRequest) -> InteractionResponse:
    case = load_case(payload.case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")

    response = await run_interaction_agent(case, payload)
    save_case(case, set_current=True)
    return response



@app.post("/api/interaction/track", response_model=InteractionTrackResponse)
async def post_interaction_track(payload: InteractionTrackRequest) -> InteractionTrackResponse:
    case = load_case(payload.case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")

    run_id = await track_interaction_event(case, payload)
    save_case(case, set_current=True)
    return InteractionTrackResponse(success=True, run_id=run_id, message="tracked")


