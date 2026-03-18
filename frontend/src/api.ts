export type EvidenceInput = {
  id?: string;
  type: "text" | "document" | "image" | "video" | "audio";
  name: string;
  content?: string;
  file_name?: string;
  mime_type?: string;
  notes?: string;
};

export type ParsedEvidence = {
  id: string;
  type: "text" | "document" | "image" | "video" | "audio";
  name: string;
  parser_tool: string;
  normalized_text: string;
  metadata?: {
    parse_status?: string;
    parser_detail?: string;
    file_name?: string | null;
    mime_type?: string | null;
    notes?: string;
  };
};

export type EvidenceItem = {
  id: string;
  type: "text" | "document" | "image" | "video" | "audio";
  original_content: string;
  source_file: string;
  page_or_paragraph: string;
  time: string;
  producer_or_speaker: string;
  is_original_evidence: boolean;
  notes: string;
};

export type Entity = {
  id: string;
  name: string;
  type: "person" | "location" | "organization" | "object" | "account" | "time";
  aliases: string[];
  source_evidence_ids: string[];
};

export type Relation = {
  id: string;
  subject_entity: string;
  object_entity: string;
  relation_type: string;
  time: string;
  evidence_sources: string[];
  confidence_status: "high" | "medium" | "low" | "unknown";
};

export type Event = {
  id: string;
  event_type: string;
  participant_entities: string[];
  time: string;
  location: string;
  description: string;
  source_evidence_ids: string[];
};

export type Claim = {
  id: string;
  content: string;
  source: string;
  target_ids: string[];
  stance: "support" | "oppose" | "neutral";
  credibility_status: "high" | "medium" | "low" | "unknown";
  quote: string;
};

export type StageLog = {
  stage_name: string;
  llm_used: boolean;
  fallback_used: boolean;
  fallback_reason: string;
  prompt_system: string;
  prompt_user: string;
  raw_response: Record<string, unknown>;
  raw_content: string;
  usage: Record<string, unknown>;
  limits: Record<string, unknown>;
  error: string;
};

export type PipelineLog = {
  provider: string;
  model: string;
  endpoint: string;
  pipeline_llm_used: boolean;
  fallback_reason: string;
  stages: StageLog[];
};

export type CurrentCase = {
  case_id: string;
  case_text: string;
  parsed_evidences: ParsedEvidence[];
  evidence_items: EvidenceItem[];
  entities: Entity[];
  relations: Relation[];
  events: Event[];
  claims: Claim[];
  extraction_log: PipelineLog;
};

export type CaseCreateResponse = {
  case: CurrentCase;
};

export type CurrentCaseEnvelope = {
  case: CurrentCase | null;
};

export type CaseQuestionResponse = {
  case_id: string;
  question: string;
  conflicts: Array<Record<string, unknown>>;
  evidence_paths: Array<Record<string, unknown>>;
  recommended_view: "conflict_compare" | "timeline_reasoning" | "hypothesis_board";
  summary: string;
  reasoning_log: PipelineLog;
};

export type QuestionViewData = {
  events: Event[];
  claims: Claim[];
  conflicts: Array<Record<string, unknown>>;
  evidence_paths: Array<Record<string, unknown>>;
  recommended_view: "conflict_compare" | "timeline_reasoning" | "hypothesis_board";
};

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

async function readJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `API request failed: ${response.status}`);
  }
  return (await response.json()) as T;
}

export async function createCase(caseText: string, evidences: EvidenceInput[], files: File[]): Promise<CaseCreateResponse> {
  const response =
    files.length > 0
      ? await fetch(`${API_BASE}/api/case`, {
          method: "POST",
          body: (() => {
            const formData = new FormData();
            formData.append("case_text", caseText);
            formData.append("manual_evidences", JSON.stringify(evidences));
            files.forEach((file) => {
              formData.append("files", file);
            });
            return formData;
          })(),
        })
      : await fetch(`${API_BASE}/api/case`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ case_text: caseText, evidences }),
        });

  return readJson<CaseCreateResponse>(response);
}

export async function getCase(): Promise<CurrentCaseEnvelope> {
  return readJson<CurrentCaseEnvelope>(await fetch(`${API_BASE}/api/case`));
}

export async function deleteCase(): Promise<void> {
  await readJson(await fetch(`${API_BASE}/api/case`, { method: "DELETE" }));
}

export async function askCaseQuestion(question: string): Promise<CaseQuestionResponse> {
  return readJson<CaseQuestionResponse>(
    await fetch(`${API_BASE}/api/case/question`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    })
  );
}
