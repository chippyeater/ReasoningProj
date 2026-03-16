export type EvidenceInput = {
  id?: string;
  type: "text" | "document" | "image" | "video" | "audio";
  name: string;
  content?: string;
  file_name?: string;
  mime_type?: string;
  notes?: string;
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

export type ReasonRequest = {
  case_text: string;
  question: string;
  evidences?: EvidenceInput[];
};

export type ReasonResponse = {
  llm_used?: boolean;
  fallback_reason?: string;
  llm_log?: {
    provider?: string;
    model?: string;
    endpoint?: string;
    llm_used?: boolean;
    fallback_reason?: string;
    prompt_system?: string;
    prompt_user?: string;
    raw_response?: Record<string, unknown>;
    raw_content?: string;
    usage?: Record<string, unknown>;
    limits?: Record<string, unknown>;
    error?: string;
  };
  parsed_evidences?: Array<Record<string, unknown>>;
  evidence_items: EvidenceItem[];
  entities: Entity[];
  relations: Relation[];
  events: Event[];
  claims: Claim[];
  conflicts: Array<Record<string, unknown>>;
  evidence_paths: Array<Record<string, unknown>>;
  recommended_view: "conflict_compare" | "timeline_reasoning" | "hypothesis_board";
  summary: string;
};

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

async function readReasonResponse(response: Response): Promise<ReasonResponse> {
  if (!response.ok) {
    throw new Error(`API request failed: ${response.status}`);
  }
  return (await response.json()) as ReasonResponse;
}

export async function postReason(
  payload: ReasonRequest,
  files: File[] = []
): Promise<ReasonResponse> {
  const response =
    files.length > 0
      ? await fetch(`${API_BASE}/api/reason`, {
          method: "POST",
          body: (() => {
            const formData = new FormData();
            formData.append("case_text", payload.case_text);
            formData.append("question", payload.question);
            formData.append("manual_evidences", JSON.stringify(payload.evidences ?? []));
            files.forEach((file) => {
              formData.append("files", file);
            });
            return formData;
          })(),
        })
      : await fetch(`${API_BASE}/api/reason`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });

  return readReasonResponse(response);
}
