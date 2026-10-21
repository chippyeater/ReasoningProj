const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

export type FileType = "pdf" | "docx" | "image" | "txt" | "markdown" | "other";
export type FileParseStatus = "pending" | "parsing" | "parsed" | "failed" | "unsupported";
export type CardType = "meta" | "inference" | "law";
export type InferenceType =
  | "hypothesis"
  | "timeline"
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


export type InfoUnitType = "entity" | "event" | "state" | "rule" | "position";
export type InfoUnitSubtype =
  | "person"
  | "organization"
  | "object"
  | "location"
  | "reported"
  | "documented"
  | "verified";

export type SubjectLegalType = "natural_person" | "legal_person" | "unincorporated_org";
export type NormSourceType = "statute" | "judicial_interpretation" | "local_regulation" | "guideline" | "case_rule" | "other";
export type NormLevel = "constitution" | "law" | "administrative_regulation" | "local_regulation" | "judicial_interpretation" | "normative_document" | "other";

export type InfoUnit = {
  id: string;
  type: InfoUnitType;
  subtype: InfoUnitSubtype | null;
  title: string;
  content: string;
  source_refs: Array<{
    file_id: string;
    source_kind: "document" | "communication" | "testimony" | "record" | "other";
    quote: string;
    start: number;
    end: number;
  }>;
  speaker: string | null;
  side: string | null;
  actor: string | null;
  target: string | null;
  related_issue: string | null;
  created_at: string;
};

export type QuoteUnit = {
  quote_id: string;
  file_id: string;
  start: number;
  end: number;
  text: string;
  role: "text" | "temporal_context" | "message" | "record" | "group_boundary";
  group_id: string | null;
  message_id: string | null;
  context_quote_ids: string[];
};

export type InfoUnitMutationInput = {
  type: InfoUnitType;
  subtype: InfoUnitSubtype | null;
  title: string;
  content: string;
  speaker: string | null;
  side: string | null;
  actor: string | null;
  target: string | null;
  related_issue: string | null;
};

export type CreateInfoUnitInput = InfoUnitMutationInput & { evidence_ids: string[] };
export type UpdateInfoUnitInput = InfoUnitMutationInput & { evidence_ids?: string[] };

export type CaseFile = {
  file_id: string;
  filename: string;
  file_type: FileType;
  file_size?: number | null;
  source_kind?: "document" | "communication" | "testimony" | "record" | "other";
  original_path?: string | null;
  parsed_text_path?: string | null;
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
  info_subtype: InfoUnitSubtype | null;
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

export type Relation = {
  id: string;
  source_id: string;
  target_id: string;
  relation_type: "about" | "support" | "contradict";
  evidence_unit_ids: string[];
  rationale: string;
  certainty: "explicit" | "inferred";
};

export type ConflictView = {
  view_type: "conflict";
  title: string;
  sections: Array<{
    id: string;
    title: string;
    placement: "left" | "right" | "center";
    items: Array<{
      info_unit_id: string;
      role: "claim" | "evidence" | "event" | "context";
      emphasis: boolean;
      default_expanded: boolean;
    }>;
  }>;
  missing_information: string[];
};

export type TimelineView = {
  view_type: "timeline";
  title: string;
  events: Array<{
    id: string;
    title: string;
    time_label: string;
    sort_date: string | null;
    time_precision: "day" | "month" | "year" | "unknown";
    summary: string;
    related_info_unit_ids: string[];
  }>;
};

export type HypothesisView = {
  view_type: "hypothesis";
  title: string;
  hypothesis: string;
  steps: Array<{
    id: string;
    claim: string;
    supporting_info_unit_ids: string[];
    opposing_info_unit_ids: string[];
    depends_on_step_ids: string[];
  }>;
  missing_information: string[];
};

export type ReasoningView = {
  id: string;
  question: string;
  view_type: "timeline" | "conflict" | "hypothesis";
  selection_reason: string;
  payload: ConflictView | TimelineView | HypothesisView;
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
  selected_node_ids?: string[];
  focused_node_id?: string | null;
  current_view_id?: string | null;
  node_positions?: Record<string, CardPosition>;
  display_levels?: Record<string, 1 | 2 | 3>;
};

export type CaseData = {
  case_id: string;
  case_title: string;
  created_at: string;
  updated_at: string;
  files: CaseFile[];
  core_issue: string | null;
  issue_set: string[];
  info_units: InfoUnit[];
  relations: Relation[];
  reasoning_views: ReasoningView[];
  law_matches: Array<Record<string, unknown>>;
  stage_runs: Array<Record<string, unknown>>;
  // The following collections are presentation-only compatibility projections.
  meta_cards: MetaCard[];
  inference_cards: InferenceCard[];
  law_cards: LawCard[];
  edges: GraphEdge[];
  workspace_state: WorkspaceState;
};

type V03WorkspaceState = {
  selected_node_ids: string[];
  focused_node_id: string | null;
  current_view_id: string | null;
  node_positions: Record<string, CardPosition>;
  display_levels: Record<string, 1 | 2 | 3>;
  viewport: { zoom: number; offset_x: number; offset_y: number };
};

function relationEdgeType(type: Relation["relation_type"]): EdgeType {
  if (type === "support") return "supports";
  if (type === "contradict") return "conflicts_with";
  return "relates_to";
}

function reasoningRefs(view: ReasoningView): { supporting: string[]; opposing: string[]; missing: string[]; claim: string } {
  if (view.payload.view_type === "hypothesis") {
    return {
      supporting: view.payload.steps.flatMap((step) => step.supporting_info_unit_ids),
      opposing: view.payload.steps.flatMap((step) => step.opposing_info_unit_ids),
      missing: view.payload.missing_information,
      claim: view.payload.hypothesis,
    };
  }
  if (view.payload.view_type === "conflict") {
    return {
      supporting: view.payload.sections.flatMap((section) => section.items.map((item) => item.info_unit_id)),
      opposing: [],
      missing: view.payload.missing_information,
      claim: view.payload.title,
    };
  }
  return {
    supporting: view.payload.events.flatMap((event) => event.related_info_unit_ids),
    opposing: [],
    missing: [],
    claim: view.payload.title,
  };
}

/** Build legacy canvas objects from v0.3 business data without creating a second authority. */
export function normalizeCaseData(raw: CaseData): CaseData {
  const source = raw as CaseData & { workspace_state: WorkspaceState | V03WorkspaceState };
  const workspace = source.workspace_state as Partial<WorkspaceState & V03WorkspaceState>;
  const positions = workspace.node_positions ?? {};
  const levels = workspace.display_levels ?? {};
  const selected = workspace.selected_card_ids ?? workspace.selected_node_ids ?? [];
  const focused = workspace.focused_card_id ?? workspace.focused_node_id ?? null;
  const metaCards = raw.meta_cards ?? raw.info_units.map((unit, index): MetaCard => ({
    id: unit.id,
    card_type: "meta",
    title: unit.title,
    summary: null,
    display_level: levels[unit.id] ?? 2,
    status: "active",
    position: positions[unit.id] ?? { x: 120 + (index % 4) * 280, y: 120 + Math.floor(index / 4) * 170 },
    ui_state: { selected: selected.includes(unit.id), highlighted: false, pinned: false, collapsed: false },
    source_file_ids: Array.from(new Set(unit.source_refs.map((ref) => ref.file_id))),
    created_at: unit.created_at,
    updated_at: raw.updated_at,
    info_type: unit.type,
    info_subtype: unit.subtype,
    detail: {
      name: unit.type === "entity" ? unit.title : null,
      description: unit.content,
      attributes: {
        speaker: unit.speaker,
        side: unit.side,
        actor: unit.actor,
        target: unit.target,
        related_issue: unit.related_issue,
      },
      tags: [unit.type, ...(unit.subtype ? [unit.subtype] : [])],
    },
  }));
  const inferenceCards = raw.inference_cards ?? raw.reasoning_views.map((view, index): InferenceCard => {
    const refs = reasoningRefs(view);
    return {
      id: view.id,
      card_type: "inference",
      title: view.payload.title,
      summary: null,
      display_level: levels[view.id] ?? 2,
      status: "active",
      position: positions[view.id] ?? { x: 1320 + (index % 2) * 300, y: 160 + Math.floor(index / 2) * 180 },
      ui_state: { selected: selected.includes(view.id), highlighted: false, pinned: false, collapsed: false },
      source_file_ids: [],
      created_at: view.created_at,
      updated_at: raw.updated_at,
      inference_type: view.view_type,
      decision: "undecided",
      detail: {
        claim: refs.claim,
        reasoning_steps: view.payload.view_type === "hypothesis" ? view.payload.steps.map((step) => step.claim) : [],
        supporting_card_ids: refs.supporting,
        opposing_card_ids: refs.opposing,
        missing_card_ids: refs.missing,
        confidence: null,
        legal_basis_ids: [],
      },
    };
  });
  const relationEdges = raw.relations.map((relation): GraphEdge => ({
    id: relation.id,
    source: relation.source_id,
    target: relation.target_id,
    edge_type: relationEdgeType(relation.relation_type),
    relation_type: relation.relation_type,
    label: relation.rationale,
    created_at: raw.updated_at,
  }));
  const derivedEdges = raw.info_units.flatMap((unit): GraphEdge[] => [
    ...(unit.actor ? [{ id: `derived-actor-${unit.id}`, source: unit.id, target: unit.actor,
      edge_type: "relates_to" as EdgeType, relation_type: "actor_of", label: "行为主体", created_at: raw.updated_at }] : []),
    ...(unit.target ? [{ id: `derived-target-${unit.id}`, source: unit.id, target: unit.target,
      edge_type: "relates_to" as EdgeType, relation_type: "target_of", label: "作用对象", created_at: raw.updated_at }] : []),
  ]);
  return {
    ...raw,
    core_issue: raw.core_issue ?? null,
    issue_set: raw.issue_set ?? [],
    relations: raw.relations ?? [],
    reasoning_views: raw.reasoning_views ?? [],
    law_matches: raw.law_matches ?? [],
    stage_runs: raw.stage_runs ?? [],
    meta_cards: metaCards,
    inference_cards: inferenceCards,
    law_cards: raw.law_cards ?? [],
    edges: raw.edges ?? [...relationEdges, ...derivedEdges],
    workspace_state: {
      current_view: workspace.current_view ?? "panorama",
      selected_card_ids: selected,
      focused_card_id: focused,
      expanded_card_ids: workspace.expanded_card_ids ?? [],
      pinned_card_ids: workspace.pinned_card_ids ?? [],
      viewport: workspace.viewport ?? { zoom: 1, offset_x: 0, offset_y: 0 },
      selected_node_ids: selected,
      focused_node_id: focused,
      current_view_id: workspace.current_view_id ?? null,
      node_positions: positions,
      display_levels: levels,
    },
  };
}

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
  const text = await response.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text) as unknown;
    } catch {
      if (response.ok) throw new Error("API returned invalid JSON");
    }
  }
  if (!response.ok) {
    const envelope = data && typeof data === "object" ? data as Record<string, unknown> : null;
    const detail = envelope?.detail;
    if (detail && typeof detail === "object" && !Array.isArray(detail)) {
      const value = detail as Record<string, unknown>;
      const code = typeof value.code === "string" ? value.code : `http_${response.status}`;
      const message = typeof value.message === "string" ? value.message : "Request validation failed";
      const references = Array.isArray(value.references)
        ? value.references.flatMap((item) => {
            if (!item || typeof item !== "object") return [];
            const ref = item as Record<string, unknown>;
            const kind = typeof ref.kind === "string" ? ref.kind : "reference";
            const id = typeof ref.id === "string" ? ref.id : "unknown";
            const field = typeof ref.field === "string" ? `.${ref.field}` : "";
            return [`${kind}:${id}${field}`];
          })
        : [];
      throw new Error(`[${code}] ${message}${references.length ? ` (${references.join(", ")})` : ""}`);
    }
    const detailText = typeof detail === "string" ? detail : (detail ? JSON.stringify(detail) : text);
    throw new Error(detailText || `API request failed: ${response.status}`);
  }
  return data as T;
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
            formData.append("source_kinds", JSON.stringify(Object.fromEntries(files.map((file) => [file.name, "other"]))));
            return formData;
          })(),
        })
      : await fetch(`${API_BASE}/api/cases`, {
          method: "POST",
          signal,
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ case_title: title }),
        });

  const data = await readJson<{ case: CaseData }>(response);
  return { case: normalizeCaseData(data.case) };
}

export async function appendFilesToCase(caseId: string, files: File[], signal?: AbortSignal): Promise<{ case: CaseData }> {
  const response = await fetch(`${API_BASE}/api/cases/${caseId}/files`, {
    method: "POST",
    signal,
    body: (() => {
      const formData = new FormData();
      files.forEach((file) => formData.append("files", file));
      formData.append("source_kinds", JSON.stringify(Object.fromEntries(files.map((file) => [file.name, "other"]))));
      return formData;
    })(),
  });
  const data = await readJson<{ case: CaseData }>(response);
  return { case: normalizeCaseData(data.case) };
}

export async function getCase(): Promise<CaseEnvelope> {
  const data = await readJson<CaseEnvelope>(await fetch(`${API_BASE}/api/case`));
  return { case: data.case ? normalizeCaseData(data.case) : null };
}

export async function getCaseById(caseId: string): Promise<{ case: CaseData }> {
  const data = await readJson<{ case: CaseData }>(await fetch(`${API_BASE}/api/cases/${encodeURIComponent(caseId)}`));
  return { case: normalizeCaseData(data.case) };
}

export async function listQuoteUnits(caseId: string): Promise<QuoteUnit[]> {
  return readJson<QuoteUnit[]>(await fetch(`${API_BASE}/api/cases/${encodeURIComponent(caseId)}/quote-units`));
}

export async function createInfoUnit(caseId: string, payload: CreateInfoUnitInput): Promise<{ case: CaseData }> {
  const data = await readJson<{ case: CaseData }>(await fetch(`${API_BASE}/api/cases/${encodeURIComponent(caseId)}/info-units`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }));
  return { case: normalizeCaseData(data.case) };
}

export async function updateInfoUnit(
  caseId: string,
  infoUnitId: string,
  payload: UpdateInfoUnitInput,
): Promise<{ case: CaseData }> {
  const data = await readJson<{ case: CaseData }>(await fetch(
    `${API_BASE}/api/cases/${encodeURIComponent(caseId)}/info-units/${encodeURIComponent(infoUnitId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  ));
  return { case: normalizeCaseData(data.case) };
}

export async function deleteInfoUnit(caseId: string, infoUnitId: string): Promise<{ case: CaseData }> {
  const data = await readJson<{ case: CaseData }>(await fetch(
    `${API_BASE}/api/cases/${encodeURIComponent(caseId)}/info-units/${encodeURIComponent(infoUnitId)}`,
    { method: "DELETE" },
  ));
  return { case: normalizeCaseData(data.case) };
}

export async function listCases(): Promise<CaseListResponse> {
  const data = await readJson<{
    cases: Array<{ case_id: string; case_title: string; updated_at: string }>;
    current_case_id: string | null;
  }>(await fetch(`${API_BASE}/api/cases`));

  return {
    cases: data.cases.map((item) => ({
      case_id: item.case_id,
      title: item.case_title,
      updated_at: Date.parse(item.updated_at) || 0,
      is_current: item.case_id === data.current_case_id,
    })),
  };
}

export async function selectCase(caseId: string): Promise<CaseEnvelope> {
  const data = await readJson<CaseEnvelope>(
    await fetch(`${API_BASE}/api/cases/${caseId}/select`, {
      method: "POST",
    })
  );
  return { case: data.case ? normalizeCaseData(data.case) : null };
}

export async function renameCase(caseId: string, caseTitle: string): Promise<CaseEnvelope> {
  const data = await readJson<CaseEnvelope>(
    await fetch(`${API_BASE}/api/cases/${caseId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ case_title: caseTitle }),
    })
  );
  return { case: data.case ? normalizeCaseData(data.case) : null };
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
  const current = (await getCase()).case;
  if (!current || current.case_id !== caseId) throw new Error("Case workspace is unavailable");
  const workspace = current.workspace_state;
  const data = await readJson<{ case: CaseData }>(
    await fetch(`${API_BASE}/api/cases/${caseId}/workspace`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        selected_node_ids: patch.selected_card_ids ?? workspace.selected_node_ids ?? workspace.selected_card_ids,
        focused_node_id: patch.focused_card_id !== undefined ? patch.focused_card_id : workspace.focused_node_id ?? null,
        current_view_id: workspace.current_view_id ?? null,
        node_positions: workspace.node_positions ?? {},
        display_levels: workspace.display_levels ?? {},
        viewport: patch.viewport ?? workspace.viewport,
      }),
    })
  );
  return { case: normalizeCaseData(data.case) };
}

export async function updateCardPosition(
  caseId: string,
  cardId: string,
  position: { x: number; y: number }
): Promise<{ case: CaseData }> {
  const current = (await getCase()).case;
  if (!current || current.case_id !== caseId) throw new Error("Case workspace is unavailable");
  current.workspace_state.node_positions = { ...(current.workspace_state.node_positions ?? {}), [cardId]: position };
  const data = await readJson<{ case: CaseData }>(await fetch(`${API_BASE}/api/cases/${caseId}/workspace`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      selected_node_ids: current.workspace_state.selected_node_ids ?? current.workspace_state.selected_card_ids,
      focused_node_id: current.workspace_state.focused_node_id ?? null,
      current_view_id: current.workspace_state.current_view_id ?? null,
      node_positions: current.workspace_state.node_positions,
      display_levels: current.workspace_state.display_levels ?? {},
      viewport: current.workspace_state.viewport,
    }),
  }));
  return { case: normalizeCaseData(data.case) };
}


export async function updateCardDisplayLevel(
  caseId: string,
  cardId: string,
  displayLevel: 1 | 2 | 3
): Promise<{ case: CaseData }> {
  const current = (await getCase()).case;
  if (!current || current.case_id !== caseId) throw new Error("Case workspace is unavailable");
  current.workspace_state.display_levels = { ...(current.workspace_state.display_levels ?? {}), [cardId]: displayLevel };
  const data = await readJson<{ case: CaseData }>(await fetch(`${API_BASE}/api/cases/${caseId}/workspace`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      selected_node_ids: current.workspace_state.selected_node_ids ?? current.workspace_state.selected_card_ids,
      focused_node_id: current.workspace_state.focused_node_id ?? null,
      current_view_id: current.workspace_state.current_view_id ?? null,
      node_positions: current.workspace_state.node_positions ?? {},
      display_levels: current.workspace_state.display_levels,
      viewport: current.workspace_state.viewport,
    }),
  }));
  return { case: normalizeCaseData(data.case) };
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





