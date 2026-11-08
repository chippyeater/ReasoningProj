import { useRef, useState } from "react";
import type { AgentResult } from "./api";

export type WorkspaceAgentRun = {
  case_id: string;
  // The current Workspace API identifies an Issue by its issue string.
  issue_id: string;
  run_id: string | null;
  goal: string;
  status: "idle" | "running" | "completed" | "incomplete" | "failed" | "cancelled";
  result: AgentResult | null;
  error: string;
};

// UI request/result ownership only; reasoning stays in the backend Workspace.
export function useWorkspaceAgentRuns() {
  const [runs, setRuns] = useState<Record<string, WorkspaceAgentRun>>({});
  const current = useRef(runs);

  function update(caseId: string, issueId: string, patch: Partial<WorkspaceAgentRun>, expectedRunId?: string) {
    const key = JSON.stringify([caseId, issueId]);
    const previous = current.current[key];
    if (expectedRunId && previous?.run_id !== expectedRunId) return;
    const run: WorkspaceAgentRun = { ...(previous ?? { case_id: caseId, issue_id: issueId, run_id: null,
      goal: "", status: "idle", result: null, error: "" }), ...patch };
    current.current = { ...current.current, [key]: run };
    setRuns(current.current);
  }

  function start(caseId: string, issueId: string, goal: string) {
    if (current.current[JSON.stringify([caseId, issueId])]?.status === "running") return null;
    const runId = `investigation-${crypto.randomUUID().replace(/-/g, "")}`;
    update(caseId, issueId, { run_id: runId, goal, status: "running", result: null, error: "" });
    return runId;
  }

  function restore(caseId: string, issueId: string, result: AgentResult) {
    if (current.current[JSON.stringify([caseId, issueId])]) return false;
    update(caseId, issueId, { run_id: result.run_id, goal: result.goal,
      status: result.run_status ?? "completed", result });
    return true;
  }

  const get = (caseId: string, issueId: string) => current.current[JSON.stringify([caseId, issueId])];
  return { runs, start, update, restore, get };
}
