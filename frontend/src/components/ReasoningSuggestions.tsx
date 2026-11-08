import type { AgentResult, CaseData, ReasoningState, ReasoningChange } from "../api";
import { displayText, proposalInfoUnitIds } from "./investigationPresentation";
import { judgments, meanings } from "./ReasoningEditor";

export function reasoningText(value: string, caseData: CaseData, state: ReasoningState, evidence: AgentResult["evidence"] = []) {
  let text = displayText(value, caseData.info_units, evidence, caseData.relations);
  for (const obj of [...state.hypotheses, ...state.gaps]) text = text.split(obj.id).join(obj.content);
  for (const file of caseData.files) text = text.split(file.file_id).join(file.filename);
  return text.replace(/\b(?:hyp|gap|elink)-[a-zA-Z0-9_-]+\b/g, "相关推理");
}

export function ReasoningSuggestions({ result, state, caseData, busy, error, onReverse, onRetry, onEdit, onLocate }: {
  result: AgentResult | null; state: ReasoningState; caseData: CaseData; busy: boolean; error: string;
  onReverse: (runId: string, restore: boolean) => void; onEdit: (change: ReasoningChange) => void; onLocate: (id: string) => void;
  onRetry: (runId: string) => void;
}) {
  const runs = Object.entries(state.investigation_runs ?? {}).reverse();
  const pending = result && !state.investigation_runs?.[result.run_id]
    && !["applied", "undone", "no_change"].includes(result.workspace_commit?.status ?? "pending") ? result : null;
  if (!runs.length && !pending?.proposals?.length && !error) return null;
  const text = (value: string) => reasoningText(value, caseData, state, result?.evidence);
  return <section className="reasoning-suggestions" aria-label="调查变更记录">
    {pending?.proposals?.length ? <details open><summary>本轮未写入（{pending.proposals.length} 项变更记录）</summary>
      <p>{pending.workspace_commit?.status === "conflict" ? "调查期间推理或材料已被修改。" : "调查未正常完成或提交失败。"}未写入任何推理变更，提案与运行记录已保留。</p>
      {pending.proposals.map((item, index) => <article key={index}><strong>{text(item.content ?? "更新已有推理")}</strong><p>{text(item.rationale)}</p>
        {proposalInfoUnitIds(item).map(id => <button type="button" key={id} onClick={() => onLocate(id)}>{caseData.info_units.find(u => u.id === id)?.title ?? "相关材料"}</button>)}
      </article>)}
      {pending.workspace_commit?.status === "failed" && pending.finish_status === "validated"
        ? <button type="button" disabled={busy} onClick={() => onRetry(pending.run_id)}>重试写入本轮变更</button> : null}
    </details> : null}
    {runs.length ? <details open><summary>调查变更记录（{runs.length} 轮）</summary><p>Agent 已统一写入内部推理对象；来源可定位不代表命题已被证实。你可以编辑、删除，或撤销本轮变更。</p>
      {runs.map(([runId, run], index) => <details key={runId} open={index === 0} data-run-id={runId}>
        <summary>调查 {runs.length - index} · {run.status === "undone" ? "已撤销" : "已写入"}</summary>
        {run.changes.filter(change => {
          const before = change.before, after = change.after;
          return !before || !after || !("judgment" in before && "judgment" in after) || before.content !== after.content || before.judgment !== after.judgment || before.work_status !== after.work_status;
        }).map(change => {
          const obj = change.after ?? change.before!;
          const current = state[change.collection].find(item => item.id === change.object_id);
          const basis = run.proposals.filter((item, i) => change.proposal_keys.includes(`${runId}:${item.proposal_id ?? i}`));
          const parent = "meaning" in obj ? run.changes.find(c => c.object_id === obj.hypothesis_id)?.after : null;
          return <article key={change.object_id} className="reasoning-suggestion">
            <strong>{change.before ? "更新" : "新增"} · {"content" in obj ? text(obj.content) : "证据关联"}</strong>
            {"meaning" in obj ? <p>「{caseData.info_units.find(u => u.id === obj.info_unit_id)?.title ?? "相关材料"}」→「{text(state.hypotheses.find(h => h.id === obj.hypothesis_id)?.content
              ?? (parent && "content" in parent ? parent.content : "关联命题"))}」 · {change.before && "meaning" in change.before ? `${meanings[change.before.meaning]} → ` : ""}{meanings[obj.meaning]}</p> : null}
            {"judgment" in obj ? <p>{change.before && "judgment" in change.before ? `${judgments[change.before.judgment]} → ` : ""}{judgments[obj.judgment]}</p> : null}
            {"addressing_info_unit_ids" in obj ? <p>{obj.status === "open" ? "仍待补证" : "缺口已回应"} · 回应材料：{obj.addressing_info_unit_ids.map(id => caseData.info_units.find(u => u.id === id)?.title ?? "相关材料").join("、") || "暂无"}</p> : null}
            <details><summary>查看变更依据</summary>{basis.map((item, i) => <div key={i}><p>{text(item.rationale)}</p>
              {proposalInfoUnitIds(item).map(id => <button type="button" key={id} onClick={() => onLocate(id)}>{caseData.info_units.find(u => u.id === id)?.title ?? "相关材料"}</button>)}
            </div>)}</details>
            {current ? <button type="button" disabled={busy} onClick={() => onEdit(change)}>查看与编辑</button> : <p>对象当前已删除或撤销</p>}
          </article>;
        })}
        <button type="button" disabled={busy} onClick={() => onReverse(runId, run.status === "undone")}>{run.status === "undone" ? "恢复本轮变更" : "撤销本轮变更"}</button>
      </details>)}
    </details> : null}
    {error ? <p role="alert">{error}</p> : null}
  </section>;
}
