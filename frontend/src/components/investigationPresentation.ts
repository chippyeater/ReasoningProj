import type { AgentResult, InfoUnit, Relation, ReasoningState } from "../api";

type Evidence = AgentResult["evidence"][number];

export function proposalKey(result: Pick<AgentResult, "run_id" | "proposals">, index: number) {
  return `${result.run_id}:${result.proposals?.[index]?.proposal_id ?? index}`;
}

export function candidateNodeId(result: Pick<AgentResult, "run_id" | "proposals">, index: number) {
  return `candidate:${proposalKey(result, index)}`;
}

/** Resolve a Link's one endpoint without guessing or crossing run boundaries. */
export function evidenceLinkPreview(result: Pick<AgentResult, "run_id" | "proposals">, state: ReasoningState, index: number) {
  const item = result.proposals?.[index];
  if (item?.kind !== "evidence_link" || item.info_unit_ids.length !== 1) return null;
  const formal = item.hypothesis_ids.length === 1 ? state.hypotheses.find(h => h.id === item.hypothesis_ids[0]) : null;
  if (formal && !item.hypothesis_proposal_ids?.length) return {
    sourceId: item.info_unit_ids[0], targetId: formal.id, title: formal.content, parentIndex: null, pending: false,
  };
  if (item.hypothesis_ids.length || item.hypothesis_proposal_ids?.length !== 1) return null;
  const parentIndex = result.proposals?.findIndex(p => p.proposal_id === item.hypothesis_proposal_ids![0]) ?? -1;
  const parent = result.proposals?.[parentIndex];
  if (parent?.kind !== "hypothesis") return null;
  const key = proposalKey(result, parentIndex);
  const adopted = state.hypotheses.find(h => h.id === state.accepted_proposals[key] && h.proposal_key === key);
  return { sourceId: item.info_unit_ids[0], targetId: adopted?.id ?? candidateNodeId(result, parentIndex),
    title: adopted?.content ?? parent.content ?? "关联假设", parentIndex, pending: !adopted };
}

/** Resolve returned IDs inside this run; indices are only for older audited results. */
export function hypothesisProposalTargets(result: Pick<AgentResult, "run_id" | "proposals">, state: ReasoningState, index: number) {
  const item = result.proposals?.[index];
  const targets = [...(item?.hypothesis_proposal_indices ?? []), ...(item?.hypothesis_proposal_ids ?? []).map(id =>
    result.proposals?.findIndex(proposal => proposal.proposal_id === id) ?? -1)];
  return targets.map(targetIndex => {
    const proposal = result.proposals?.[targetIndex];
    const key = proposalKey(result, targetIndex);
    const id = state.accepted_proposals[key];
    return { title: proposal?.kind === "hypothesis" ? proposal.content ?? "关联假设" : "关联假设不可用",
      accepted: proposal?.kind === "hypothesis" && state.hypotheses.some(h => h.id === id && h.proposal_key === key) };
  });
}

/** Exact source identity, never similarity, title or InfoUnit identity. */
export function groupSources(evidence: Evidence[]) {
  const groups = new Map<string, { source: Evidence; refs: Evidence[] }>();
  for (const ref of evidence) {
    const key = JSON.stringify([ref.file_id, ref.start, ref.end, ref.quote]);
    const group = groups.get(key);
    if (group) group.refs.push(ref);
    else groups.set(key, { source: ref, refs: [ref] });
  }
  return [...groups.values()];
}

export const sourceKinds: Record<string, string> = {
  testimony: "笔录陈述", communication: "沟通记录", document: "文书材料", record: "记录材料", other: "其他材料",
};

export function displayText(text: string, units: Pick<InfoUnit, "id" | "title">[], evidence: Evidence[], relations: Relation[]) {
  const labels = new Map(units.map(unit => [unit.id, unit.title]));
  for (const [key, value] of Object.entries({ search_info_units: "资料检索", get_relations: "关联核查", get_evidence: "原文核查",
    propose_hypothesis: "假设建议", propose_evidence_link: "证据关联建议", propose_evidence_gap: "缺口建议",
    propose_hypothesis_status: "判断调整建议", present_findings: "发现展示",
    truncated: "部分材料尚未核查", invalid_request: "查询条件需要调整", error: "查询未能完成" })) labels.set(key, value);
  for (const ref of evidence) labels.set(ref.file_id, ref.filename);
  for (const relation of relations) labels.set(relation.id, "相关关系");
  return text.replace(/[A-Za-z][A-Za-z0-9_-]*/g, token => labels.get(token) ?? (
    /^(?:info|file|relation|investigation|proposal|run)-/.test(token) || /^[UR]\d{3,}$/.test(token) ? "相关材料" : token));
}

/** Collapse runtime diagnostics without deduplicating distinct factual questions. */
export function presentQuestion(question: string) {
  if (/truncated|has_more|部分关系的依据 ID 列表被截断/.test(question)) return { limit: true, text: "部分材料尚未核查，当前判断可能不完整。" };
  if (/invalid_request/.test(question)) return { limit: true, text: "部分资料未能查询，需要调整调查范围后继续核查。" };
  if (/\berror\b|parsed_cache|无法读取/.test(question)) return { limit: true, text: "部分材料暂时无法读取，相关判断尚待核实。" };
  if (/工具预算|调用尝试上限|调查预算耗尽/.test(question)) return { limit: true, text: "本轮核查范围有限，仍可继续查阅材料。" };
  if (/等价重复查询|duplicate_query/.test(question)) return { limit: true, text: "本轮未再获得新线索，可以换一个角度继续核查。" };
  if (/未使用可精确回溯|未取得可精确回溯/.test(question)) return { limit: true, text: "本轮缺少可核对的原文依据。" };
  return { limit: false, text: question.replace(/search_info_units|get_relations|get_evidence/g, "资料查询")
    .replace(/SourceRefs?/g, "原文引用").replace(/InfoUnits?/g, "相关信息") };
}
