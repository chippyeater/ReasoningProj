# ReasoningProj Backend Integration Doc

This document reflects the current backend implementation in:
- `backend/app/main.py`
- `backend/app/schemas.py`

## 1. Service Basics

- Framework: FastAPI
- Default local URL: `http://localhost:8000`
- Entry: `backend/app/main.py`
- Data models: `backend/app/schemas.py`
- Main data directory: `backend/data/<case_id>/`

## 2. General Conventions

- No auth is enabled right now.
- CORS is open (`allow_origins=["*"]`).
- Datetime fields are ISO8601 UTC strings.
- Default request body is JSON, except file upload endpoints (`multipart/form-data`).

## 3. API Endpoints

## 3.1 Health

### `GET /health`
- Purpose: service health check
- Response:

```json
{ "status": "ok" }
```

## 3.2 Case APIs

### `GET /api/cases`
- Purpose: list cases
- Response model: `ListCasesResponse`

### `GET /api/case`
- Purpose: get current selected case
- Response model: `GetCaseResponse` (`case` may be `null`)

### `GET /api/cases/{case_id}`
- Purpose: get case by ID
- Response model: `GetCaseResponse`
- Errors: `404 Case not found`

### `POST /api/cases`
- Purpose: create or upsert case
- Response model: `UpsertCaseResponse`
- Supports three input modes:

1. `multipart/form-data`
- Fields: `case_title` (optional)
- Files: one or many file parts
- Behavior: create new case, save uploaded files, run extraction.

2. JSON payload with `case`
- Body shape: `{ "case": CaseData }`
- Behavior: upsert full case object.

3. JSON payload without `case`
- Body shape: `{ "case_title"?: string, "case_id"?: string }`
- Behavior: create empty case.

### `POST /api/cases/{case_id}/files`
- Purpose: append uploaded files to existing case
- Request: `multipart/form-data`
- Response model: `UpsertCaseResponse`
- Current implementation note: `append_files_to_case` is currently a placeholder (no file append, no extraction rerun).

### `POST /api/cases/{case_id}/select`
- Purpose: set current case
- Response model: `GetCaseResponse`
- Errors: `404 Case not found`

### `PATCH /api/cases/{case_id}`
- Purpose: update case metadata
- Request model: `UpdateCaseRequest`
- Response model: `UpsertCaseResponse`

### `DELETE /api/case`
- Purpose: clear current case pointer
- Response:

```json
{ "status": "cleared" }
```

### `DELETE /api/cases/{case_id}`
- Purpose: delete a specific case
- Response:

```json
{ "status": "deleted", "case_id": "case-xxxx" }
```

## 3.3 Card and Workspace APIs

### `PATCH /api/cases/{case_id}/cards/{card_id}`
- Purpose: patch one card (`meta`, `inference`, or `law`)
- Request model: `UpdateCardRequest`
- Response model: `UpsertCaseResponse`
- Errors: `404 Case not found`, `404 Card not found`

### `PATCH /api/cases/{case_id}/workspace`
- Purpose: patch workspace state
- Request model: `UpdateWorkspaceRequest`
- Response model: `UpsertCaseResponse`

## 3.4 Agent and Reasoning APIs

### `POST /api/inference/generate`
- Purpose: generate inference cards/edges from selected cards
- Request model: `GenerateInferenceRequest`
- Response model: `GenerateInferenceResponse`

### `POST /api/router/route`
- Purpose: route incoming user intent to a task
- Request model: `RouteRequest`
- Response model: `RouteResponse`

### `POST /api/extraction/run`
- Purpose: run extraction and generate `info_units`
- Request model: `ExtractionRequest`
- Response model: `ExtractionResponse`

### `POST /api/relation/run`
- Purpose: run relation extraction and generate `relations` and `edges`
- Request model: `RelationRequest`
- Response model: `RelationResponse`

### `POST /api/reasoning/run`
- Purpose: run reasoning agent
- Request model: `ReasoningRequest`
- Response model: `ReasoningResponse`

### `POST /api/qa/ask`
- Purpose: QA over current case
- Request model: `QARequest`
- Response model: `QAResponse`

### `POST /api/interaction/act`
- Purpose: parse user intent into UI actions
- Request model: `InteractionRequest`
- Response model: `InteractionResponse`

### `POST /api/interaction/track`
- Purpose: track frontend interaction events
- Request model: `InteractionTrackRequest`
- Response model: `InteractionTrackResponse`

## 4. Core Type Definitions

## 4.1 Main Enums

- `FileType`: `pdf | docx | image | txt | markdown | other`
- `FileParseStatus`: `pending | parsing | parsed | failed`
- `CardType`: `meta | inference | law`
- `DisplayLevel`: `1 | 2 | 3`
- `CardStatus`: `active | hidden | archived`
- `InferenceType`: `hypothesis | conclusion | conflict | missing_evidence | reasoning_step | evidence_chain | risk | other`
- `InferenceDecision`: `undecided | accepted | rejected | pending`
- `EdgeType`: `relates_to | supports | opposes | derives | mentions | conflicts_with | missing_for | cites | belongs_to`
- `ViewMode`: `panorama | reasoning | detail`
- `ConfidenceLevel`: `high | medium | low`
- `CertaintyLevel`: `explicit | inferred`
- `RouterTask`: `extraction | relation | reasoning | qa | interaction`
- `RecommendedView`: `timeline | conflict | hypothesis | evidence_chain`
- `IntentType`: `qa | search | reasoning | canvas_edit | view_switch`
- `ResponseMode`: `text | canvas | mixed`
- `UIActionType`: `highlight | open_view | rearrange | create_node`
- `InfoUnitType`: `subject | event | state | claim`
- `SubjectSubtype`: `person | organization`
- `EventSubtype`: `legal_act | factual_act | transaction | communication | violation`
- `StateSubtype`: `temporal | physical_state | usage_state`
- `ClaimSubtype`: `fact_assertion | legal_assertion | defense`
- `InfoUnitSubtype`: union of all subtypes above

Note:
- `InfoUnitType` is the source of truth for extraction-level card typing.
- `MetaCard` now stores `info_type` and `info_subtype` directly.

## 4.2 Info Unit Types

- Validation rule exists in `InfoUnit`: `type` and `subtype` must match.

## 4.3 Core Models

### `CaseFile`
- File metadata and parsing state.
- Fields include:
- `file_id`, `filename`, `file_type`, `file_size`, `storage_path`
- `uploaded_at`, `parse_status`, `preview_text`, `page_count`, `error_message`

### `InfoUnit`
- Minimal extracted unit for analysis.
- Fields include:
- `id`, `type`, `subtype`, `legal_type`
- `title`, `summary`, `detail`
- `source_refs[]`, `confidence`
- `extraction_reason`, `evidence_quote`
- `created_at`, `updated_at`

### `RelationRecord`
- Fields include:
- `id`, `source_id`, `target_id`, `relation_type`
- `confidence`, `evidence_basis`, `rationale`, `certainty_level`
- `created_at`

### Card Models
- `MetaCard` (`card_type=meta`) + `MetaDetail`
- `InferenceCard` (`card_type=inference`) + `InferenceDetail`
- `LawCard` (`card_type=law`) + `LawDetail`
- All inherit `CardBase`:
- `title`, `summary`, `display_level`, `status`, `position`, `ui_state`, `source_file_ids`, timestamps

### `GraphEdge`
- Fields:
- `id`, `source`, `target`, `edge_type`, `label`, `weight`, `created_at`

### `WorkspaceState`
- Fields:
- `current_view`
- `selected_card_ids[]`, `focused_card_id`
- `expanded_card_ids[]`, `pinned_card_ids[]`
- `viewport` (`zoom`, `offset_x`, `offset_y`)

### `CaseData`
- Top-level case object:
- `case_id`, `case_title`, `created_at`, `updated_at`
- `files[]`
- `info_units[]`
- `relations[]`
- `meta_cards[]`
- `inference_cards[]`
- `law_cards[]`
- `edges[]`
- `workspace_state`
- `agent_runs[]`

## 4.4 API Request/Response Models

- Case:
- `GetCaseResponse`
- `UpsertCaseResponse`
- `ListCasesResponse`
- `CaseListItem`
- `UpdateCaseRequest`

- Editing:
- `UpdateCardRequest`
- `UpdateWorkspaceRequest`

- Agent:
- `GenerateInferenceRequest`, `GenerateInferenceResponse`
- `RouteRequest`, `RouteResponse`
- `ExtractionRequest`, `ExtractionResponse`
- `RelationRequest`, `RelationResponse`
- `ReasoningRequest`, `ReasoningResponse`
- `QARequest`, `QAResponse`
- `InteractionRequest`, `InteractionResponse`
- `InteractionTrackRequest`, `InteractionTrackResponse`
- `UIAction`

## 5. Local Run

## 5.1 Backend

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

## 5.2 Frontend

```bash
cd frontend
npm install
npm run dev
```

Frontend default URL: `http://localhost:5173`

## 6. Integration Notes

- For full-case upsert, use `POST /api/cases` with body `{ "case": CaseData }`.
- For uploads, always use `multipart/form-data`.
- When schema changes, update backend `schemas.py` first, then frontend API types.



