import type { AgentResult, CaseData, ReasoningState } from "../api";
import { displayText, evidenceLinkPreview, hypothesisProposalTargets, proposalKey } from "./investigationPresentation";
import { judgments, meanings } from "./ReasoningEditor";

export function reasoningText(value: string, caseData: CaseData, state: ReasoningState, evidence: AgentResult["evidence"] = []) {
  let text = displayText(value, caseData.info_units, evidence, caseData.relations);
  for (const obj of [...state.hypotheses, ...state.gaps]) text = text.split(obj.id).join(obj.content);
  for (const file of caseData.files) text = text.split(file.file_id).join(file.filename);
  return text.replace(/\b(?:hyp|gap|elink)-[a-zA-Z0-9_-]+\b/g, "相关推理");
}

export function ReasoningSuggestions({ result, state, caseData, dismissed, busy, error, onAccept, onIgnore, onLocate }: {
  result: AgentResult; state: ReasoningState; caseData: CaseData; dismissed: number[]; busy: boolean; error: string;
  onAccept: (index: number) => void; onIgnore: (index: number) => void; onLocate: (id: string) => void;
}) {
  const kinds = { hypothesis: "新假设", evidence_link: "证据关联", evidence_gap: "证据缺口", hypothesis_status: "判断调整" };
  function text(value: string) {
    return reasoningText(value, caseData, state, result.evidence);
  }
  const proposals = (result.proposals ?? []).map((item, index) => ({ item, index }))
    .filter(({ index }) => !dismissed.includes(index) && !state.accepted_proposals[proposalKey(result, index)]);
  if (!proposals.length && !error) return null;
  return <section className="reasoning-suggestions" aria-label="待确认推理建议">
    <details><summary>查看待确认建议（{proposals.length}）</summary><p>采纳后才保存；建议不是已证实的事实。候选也可在画布中查看。</p>
    {proposals.map(({ item, index }) => {
      const targets = hypothesisProposalTargets(result, state, index);
      const link = evidenceLinkPreview(result, state, index);
      const pending = targets.some(target => !target.accepted);
      return <article key={index} className="reasoning-suggestion" data-proposal-index={index}>
      <strong>{kinds[item.kind]}{item.meaning ? ` · ${meanings[item.meaning]}` : item.judgment ? ` · ${judgments[item.judgment]}` : ""}</strong>
      <p>{link ? `「${caseData.info_units.find(u => u.id === link.sourceId)?.title ?? "相关材料"}」→「${text(link.title)}」`
        : text(item.content ?? state.hypotheses.find(h => h.id === item.hypothesis_ids[0])?.content ?? "检查现有推理")}</p>
      {pending ? <p>请先采纳关联假设，再采纳此{item.kind === "evidence_link" ? "证据关联" : "缺口"}。若已忽略该假设，请重新调查。</p> : null}
      {targets.length ? <details><summary>关联假设</summary>{targets.map((target, offset) => <p key={offset}>{target.accepted ? "已采纳" : "待采纳"} · {text(target.title)}</p>)}</details> : null}
      <details><summary>查看建议依据</summary><p>{text(item.rationale)}</p>{item.info_unit_ids.map(id => <button type="button" key={id} onClick={() => onLocate(id)}>{caseData.info_units.find(u => u.id === id)?.title ?? "相关材料"}</button>)}</details>
      <button type="button" disabled={busy || pending} onClick={() => onAccept(index)}>采纳建议</button><button type="button" disabled={busy} onClick={() => onIgnore(index)}>忽略建议</button>
    </article>; })}
    </details>{error ? <p role="alert">{error}</p> : null}
  </section>;
}
