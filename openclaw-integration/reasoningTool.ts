// OpenClaw reasoning tool: call local backend and return structured JSON.
export async function reasoningTool(case_text: string, question: string) {
  const response = await fetch("http://localhost:8000/api/reason", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ case_text, question }),
  });

  if (!response.ok) {
    throw new Error(`reasoningTool failed with status ${response.status}`);
  }

  return await response.json();
}
