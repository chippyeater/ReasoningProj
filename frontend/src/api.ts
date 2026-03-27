const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

export type FileType = "pdf" | "docx" | "image" | "txt" | "markdown" | "other";
export type FileParseStatus = "pending" | "parsing" | "parsed" | "failed";
export type EvidenceType = "text_span" | "image_region" | "screenshot" | "table" | "extracted_statement" | "other";
export type CardType = "meta" | "inference";
export type MetaType =
  | "person"
  | "organization"
  | "location"
  | "time"
  | "object"
  | "account"
  | "document"
  | "event"
  | "claim"
  | "law"
  | "other";
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
  | "belongs_to";
export type ViewMode = "panorama" | "reasoning" | "detail";

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

export type EvidenceAnchor = {
  file_id: string;
  page?: number | null;
  section?: string | null;
  paragraph_index?: number | null;
  char_start?: number | null;
  char_end?: number | null;
  bbox?: number[] | null;
};

export type EvidenceItem = {
  evidence_id: string;
  evidence_type: EvidenceType;
  label: string;
  content?: string | null;
  summary?: string | null;
  anchors: EvidenceAnchor[];
  source_file_ids: string[];
  created_at: string;
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
  source_evidence_ids: string[];
  source_file_ids: string[];
  created_at: string;
  updated_at: string;
  meta_type: MetaType;
  detail: {
    name?: string | null;
    aliases: string[];
    description?: string | null;
    attributes: Record<string, unknown>;
    image_urls: string[];
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
  source_evidence_ids: string[];
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
    notes?: string | null;
  };
};

export type GraphEdge = {
  id: string;
  source: string;
  target: string;
  edge_type: EdgeType;
  label?: string | null;
  weight?: number | null;
  source_evidence_ids: string[];
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
  evidences: EvidenceItem[];
  meta_cards: MetaCard[];
  inference_cards: InferenceCard[];
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

export async function updateWorkspaceState(
  caseId: string,
  viewport: { zoom: number; offset_x: number; offset_y: number }
): Promise<{ case: CaseData }> {
  return readJson<{ case: CaseData }>(
    await fetch(`${API_BASE}/api/cases/${caseId}/workspace`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ viewport }),
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
