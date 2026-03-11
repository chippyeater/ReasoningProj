export type ReasonRequest = {
  case_text: string;
  question: string;
};

export type ReasonResponse = {
  entities: Array<Record<string, unknown>>;
  events: Array<Record<string, unknown>>;
  claims: Array<Record<string, unknown>>;
  conflicts: Array<Record<string, unknown>>;
  evidence_paths: Array<Record<string, unknown>>;
  recommended_view: "conflict_compare" | "timeline_reasoning" | "hypothesis_board";
  summary: string;
};

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

// Fetch reasoning JSON from backend API.
export async function postReason(payload: ReasonRequest): Promise<ReasonResponse> {
  const res = await fetch(`${API_BASE}/api/reason`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    throw new Error(`API request failed: ${res.status}`);
  }

  return (await res.json()) as ReasonResponse;
}
