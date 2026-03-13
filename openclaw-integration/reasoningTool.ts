type EvidenceInput = {
  id?: string;
  type: "text" | "document" | "image" | "video" | "audio";
  name: string;
  content?: string;
  file_name?: string;
  mime_type?: string;
  notes?: string;
};

// OpenClaw reasoning tool: call local backend and return structured JSON.
export async function reasoningTool(case_text: string, question: string, evidences: EvidenceInput[] = []) {
  const response = await fetch("http://localhost:8000/api/reason", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ case_text, question, evidences }),
  });

  if (!response.ok) {
    throw new Error(`reasoningTool failed with status ${response.status}`);
  }

  return await response.json();
}
