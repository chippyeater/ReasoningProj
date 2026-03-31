const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

export type FileType = "pdf" | "docx" | "image" | "txt" | "markdown" | "other";
export type FileParseStatus = "pending" | "parsing" | "parsed" | "failed";
export type CardType = "meta" | "inference" | "law";
export type InferenceType =
  | "hypothesis"
  | "conclusion"
  | "conflict"
  | "missing_evidence"
  | "reasoning_step"
  | "evidence_chain"
  | "risk"
  | "other";
export type EdgeType =
  | "relates_to"
  | "supports"
  | "opposes"
  | "derives"
  | "mentions"
  | "conflicts_with"
  | "missing_for"
  | "cites"
  | "belongs_to"
  | "grounded_in"
  | "applies_to"
  | "retrieved_for";
export type ViewMode = "panorama" | "reasoning" | "detail";


export type InfoUnitType = "subject" | "event" | "state" | "claim";
export type InfoUnitSubtype =
  | "person"
  | "organization"
  | "legal_act"
  | "factual_act"
  | "transaction"
  | "communication"
  | "violation"
  | "temporal"
  | "physical_state"
  | "usage_state"
  | "fact_assertion"
  | "legal_assertion"
  | "defense";

export type SubjectLegalType = "natural_person" | "legal_person" | "unincorporated_org";
export type NormSourceType = "statute" | "judicial_interpretation" | "local_regulation" | "guideline" | "case_rule" | "other";
export type NormLevel = "constitution" | "law" | "administrative_regulation" | "local_regulation" | "judicial_interpretation" | "normative_document" | "other";

export type InfoUnit = {
  id: string;
  type: InfoUnitType;
  subtype: InfoUnitSubtype;
  subject_legal_type?: SubjectLegalType | null;
  title: string;
  summary: string;
  detail: string;
  source_refs: string[];
  confidence: "high" | "medium" | "low";
  extraction_reason: string;
  evidence_quote: string;
  created_at: string;
  updated_at: string;
};

export type CaseFile = {
  file_id: string;
  filename: string;
  file_type: FileType;
  file_size?: number | null;
  storage_path?: string | null;
  uploaded_at: string;
  parse_status: FileParseStatus;
  preview_text?: string | null;
  page_count?: number | null;
  error_message?: string | null;
};

export type CardPosition = { x: number; y: number };

export type CardUIState = {
  selected: boolean;
  highlighted: boolean;
  pinned: boolean;
  collapsed: boolean;
};

export type MetaCard = {
  id: string;
  card_type: "meta";
  title: string;
  summary?: string | null;
  display_level: 1 | 2 | 3;
  status: "active" | "hidden" | "archived";
  position: CardPosition;
  ui_state: CardUIState;
  source_file_ids: string[];
  created_at: string;
  updated_at: string;
  info_type: InfoUnitType;
  info_subtype: InfoUnitSubtype;
  detail: {
    name?: string | null;
    description?: string | null;
    attributes: Record<string, unknown>;
    tags: string[];
  };
};

export type InferenceCard = {
  id: string;
  card_type: "inference";
  title: string;
  summary?: string | null;
  display_level: 1 | 2 | 3;
  status: "active" | "hidden" | "archived";
  position: CardPosition;
  ui_state: CardUIState;
  source_file_ids: string[];
  created_at: string;
  updated_at: string;
  inference_type: InferenceType;
  decision: "undecided" | "accepted" | "rejected" | "pending";
  detail: {
    claim: string;
    reasoning_steps: string[];
    supporting_card_ids: string[];
    opposing_card_ids: string[];
    missing_card_ids: string[];
    confidence?: number | null;
    legal_basis_ids: string[];
  };
};


export type LawCard = {
  id: string;
  card_type: "law";
  title: string;
  summary?: string | null;
  display_level: 1 | 2 | 3;
  status: "active" | "hidden" | "archived";
  position: CardPosition;
  ui_state: CardUIState;
  source_file_ids: string[];
  created_at: string;
  updated_at: string;
  source_type: NormSourceType;
  norm_level: NormLevel;
  detail: {
    source_type: NormSourceType;
    norm_level: NormLevel;
    article_no?: string | null;
    title_full: string;
    effective_status?: string | null;
    source_url?: string | null;
    keywords: string[];
    text?: string | null;
    source_info_unit_ids: string[];
  };
};

export type GraphEdge = {
  id: string;
  source: string;
  target: string;
  edge_type: EdgeType;
  relation_type?: string | null;
  label?: string | null;
  weight?: number | null;
  created_at: string;
};

export type WorkspaceState = {
  current_view: ViewMode;
  selected_card_ids: string[];
  focused_card_id?: string | null;
  expanded_card_ids: string[];
  pinned_card_ids: string[];
  viewport: {
    zoom: number;
    offset_x: number;
    offset_y: number;
  };
};

export type CaseData = {
  case_id: string;
  case_title: string;
  created_at: string;
  updated_at: string;
  files: CaseFile[];
  info_units: InfoUnit[];
  meta_cards: MetaCard[];
  inference_cards: InferenceCard[];
  law_cards: LawCard[];
  edges: GraphEdge[];
  workspace_state: WorkspaceState;
};

export type CaseEnvelope = {
  case: CaseData | null;
};

export type CaseSummary = {
  case_id: string;
  title: string;
  updated_at: number;
  is_current: boolean;
};

export type CaseListResponse = {
  cases: CaseSummary[];
};

export type GenerateInferenceMode = "hypothesis" | "conclusion" | "conflict_check" | "missing_evidence";

export type GenerateInferenceResponse = {
  success: boolean;
  new_inference_cards: InferenceCard[];
  new_edges: GraphEdge[];
  updated_workspace_state?: WorkspaceState | null;
  message?: string | null;
};

export type LawRetrieveIntent =
  | "support_conflict_resolution"
  | "retrieve_for_conclusion"
  | "validate_conflict_basis";

export type LawRetrieveRequest = {
  case_id: string;
  conflict_card_id: string;
  selected_card_ids?: string[];
  intent?: LawRetrieveIntent;
  query_hint?: string | null;
  max_results?: number;
};

export type LawRetrieveResponse = {
  success: boolean;
  conflict_card_id: string;
  new_law_cards: LawCard[];
  new_edges: GraphEdge[];
  updated_workspace_state?: WorkspaceState | null;
  message?: string | null;
};

export type QuestionViewData = {
  events: Array<Record<string, unknown>>;
  claims: Array<Record<string, unknown>>;
  conflicts: Array<Record<string, unknown>>;
  evidence_paths: Array<Record<string, unknown>>;
  recommended_view: "conflict_compare" | "timeline_reasoning" | "hypothesis_board";
};

async function readJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `API request failed: ${response.status}`);
  }
  return (await response.json()) as T;
}

export async function createCase(
  caseTitle: string,
  files: File[],
  signal?: AbortSignal
): Promise<{ case: CaseData }> {
  const title = caseTitle.trim() || "未命名案件";
  const response =
    files.length > 0
      ? await fetch(`${API_BASE}/api/cases`, {
          method: "POST",
          signal,
          body: (() => {
            const formData = new FormData();
            formData.append("case_title", title);
            files.forEach((file) => formData.append("files", file));
            return formData;
          })(),
        })
      : await fetch(`${API_BASE}/api/cases`, {
          method: "POST",
          signal,
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ case_title: title }),
        });

  return readJson<{ case: CaseData }>(response);
}

export async function appendFilesToCase(caseId: string, files: File[], signal?: AbortSignal): Promise<{ case: CaseData }> {
  const response = await fetch(`${API_BASE}/api/cases/${caseId}/files`, {
    method: "POST",
    signal,
    body: (() => {
      const formData = new FormData();
      files.forEach((file) => formData.append("files", file));
      return formData;
    })(),
  });
  return readJson<{ case: CaseData }>(response);
}

export async function getCase(): Promise<CaseEnvelope> {
  return readJson<CaseEnvelope>(await fetch(`${API_BASE}/api/case`));
}

export async function listCases(): Promise<CaseListResponse> {
  const data = await readJson<{
    cases: Array<{ case_id: string; case_title: string; updated_at: string; is_current: boolean }>;
  }>(await fetch(`${API_BASE}/api/cases`));

  return {
    cases: data.cases.map((item) => ({
      case_id: item.case_id,
      title: item.case_title,
      updated_at: Date.parse(item.updated_at) || 0,
      is_current: item.is_current,
    })),
  };
}

export async function selectCase(caseId: string): Promise<CaseEnvelope> {
  return readJson<CaseEnvelope>(
    await fetch(`${API_BASE}/api/cases/${caseId}/select`, {
      method: "POST",
    })
  );
}

export async function renameCase(caseId: string, caseTitle: string): Promise<CaseEnvelope> {
  return readJson<CaseEnvelope>(
    await fetch(`${API_BASE}/api/cases/${caseId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ case_title: caseTitle }),
    })
  );
}

export async function deleteCase(): Promise<void> {
  await readJson(await fetch(`${API_BASE}/api/case`, { method: "DELETE" }));
}

export async function generateInference(
  caseId: string,
  userPrompt: string,
  mode: GenerateInferenceMode = "hypothesis",
  selectedCardIds: string[] = []
): Promise<GenerateInferenceResponse> {
  return readJson<GenerateInferenceResponse>(
    await fetch(`${API_BASE}/api/inference/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        case_id: caseId,
        selected_card_ids: selectedCardIds,
        mode,
        user_prompt: userPrompt,
      }),
    })
  );
}

export async function retrieveLaw(payload: LawRetrieveRequest): Promise<LawRetrieveResponse> {
  return readJson<LawRetrieveResponse>(
    await fetch(`${API_BASE}/api/law/retrieve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
  );
}

export type WorkspacePatch = {
  current_view?: ViewMode;
  selected_card_ids?: string[];
  focused_card_id?: string | null;
  expanded_card_ids?: string[];
  pinned_card_ids?: string[];
  viewport?: { zoom: number; offset_x: number; offset_y: number };
};

export async function updateWorkspaceState(
  caseId: string,
  patch: WorkspacePatch
): Promise<{ case: CaseData }> {
  return readJson<{ case: CaseData }>(
    await fetch(`${API_BASE}/api/cases/${caseId}/workspace`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    })
  );
}

export async function updateCardPosition(
  caseId: string,
  cardId: string,
  position: { x: number; y: number }
): Promise<{ case: CaseData }> {
  return readJson<{ case: CaseData }>(
    await fetch(`${API_BASE}/api/cases/${caseId}/cards/${cardId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ position }),
    })
  );
}


export async function updateCardDisplayLevel(
  caseId: string,
  cardId: string,
  displayLevel: 1 | 2 | 3
): Promise<{ case: CaseData }> {
  return readJson<{ case: CaseData }>(
    await fetch(`${API_BASE}/api/cases/${caseId}/cards/${cardId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ display_level: displayLevel }),
    })
  );
}
export type InteractionTrackPayload = {
  case_id: string;
  action: string;
  targets?: string[];
  params?: Record<string, unknown>;
  context?: Record<string, unknown>;
};

export type InteractionTrackResponse = {
  success: boolean;
  run_id: string;
  message?: string | null;
};

export async function trackInteraction(payload: InteractionTrackPayload): Promise<InteractionTrackResponse> {
  return readJson<InteractionTrackResponse>(
    await fetch(`${API_BASE}/api/interaction/track`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
  );
}

export type RouterTask = "extraction" | "relation" | "reasoning" | "qa" | "interaction";

export type RouteRequestPayload = {
  user_input: string;
  current_selection?: string[];
  system_state?: Record<string, unknown>;
};

export type RouteResponsePayload = {
  task: RouterTask;
  subtask: string;
  target_scope: string[];
  requires_tool: boolean;
};

export type ExtractionRunRequest = {
  case_id: string;
  file_ids?: string[];
  raw_text?: string;
  context?: string;
};

export type ExtractionRunResponse = {
  success: boolean;
  info_units: InfoUnit[];
  message?: string | null;
};

export type RelationRunRequest = {
  case_id: string;
  info_unit_ids?: string[];
};

export type RelationRunResponse = {
  success: boolean;
  relations: Array<Record<string, unknown>>;
  new_edges: GraphEdge[];
  message?: string | null;
};

export type ReasoningRunRequest = {
  case_id: string;
  selected_info_units: string[];
  selected_relations: string[];
  user_prompt: string;
  current_canvas_state?: Record<string, unknown>;
};

export type ReasoningRunResponse = {
  success: boolean;
  recommended_view: "timeline" | "conflict" | "hypothesis" | "evidence_chain";
  reasoning_structure: Record<string, unknown>;
  view_payload: Record<string, unknown>;
  missing_information: string[];
  reasoning_rationale: string;
  new_inference_cards: InferenceCard[];
  new_edges: GraphEdge[];
  message?: string | null;
};

export type QAAskRequest = {
  case_id: string;
  question: string;
};

export type QAAskResponse = {
  answer: string;
  matched_items: string[];
  highlight_targets: string[];
  confidence: "high" | "medium" | "low";
};

export type InteractionActRequest = {
  case_id: string;
  user_input: string;
  current_selection?: string[];
  system_state?: Record<string, unknown>;
};

export type InteractionActResponse = {
  intent_type: "qa" | "search" | "reasoning" | "canvas_edit" | "view_switch";
  response_mode: "text" | "canvas" | "mixed";
  ui_actions: Array<{ action: "highlight" | "open_view" | "rearrange" | "create_node"; targets: string[]; params: Record<string, unknown> }>;
  assistant_message: string;
};

export async function routeTask(payload: RouteRequestPayload): Promise<RouteResponsePayload> {
  return readJson<RouteResponsePayload>(
    await fetch(`${API_BASE}/api/router/route`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
  );
}

export async function runExtraction(payload: ExtractionRunRequest): Promise<ExtractionRunResponse> {
  return readJson<ExtractionRunResponse>(
    await fetch(`${API_BASE}/api/extraction/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
  );
}

export async function runRelation(payload: RelationRunRequest): Promise<RelationRunResponse> {
  return readJson<RelationRunResponse>(
    await fetch(`${API_BASE}/api/relation/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
  );
}

export async function runReasoning(payload: ReasoningRunRequest): Promise<ReasoningRunResponse> {
  return readJson<ReasoningRunResponse>(
    await fetch(`${API_BASE}/api/reasoning/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
  );
}

export async function askQa(payload: QAAskRequest): Promise<QAAskResponse> {
  return readJson<QAAskResponse>(
    await fetch(`${API_BASE}/api/qa/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
  );
}

export async function actInteraction(payload: InteractionActRequest): Promise<InteractionActResponse> {
  return readJson<InteractionActResponse>(
    await fetch(`${API_BASE}/api/interaction/act`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
  );
}





