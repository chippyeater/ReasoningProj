import { useState } from "react";
import type { CaseData, EvidenceGap, EvidenceLink, Hypothesis, Judgment, LinkMeaning, ReasoningEdit, ReasoningState } from "../api";

export const judgments = { unverified: "未证实", verified: "已证实", refuted: "已证伪" };
export const meanings = { support: "支持", oppose: "反对", related: "相关" };
export type ReasoningEditorTarget = { kind: "hypothesis" | "gap" | "link"; id?: string; hypothesisId?: string; infoUnitId?: string };

export function ReasoningEditor({ target, state, caseData, issue, busy, error, onSave, onClose, onEdit, onLocate, onInvestigate }: {
  target: ReasoningEditorTarget; state: ReasoningState; caseData: CaseData; issue: string; busy: boolean; error: string;
  onSave: (edit: ReasoningEdit) => void; onClose: () => void; onEdit: (target: ReasoningEditorTarget) => void;
  onLocate: (id: string) => void; onInvestigate: (id: string, question: string) => void;
}) {
  const original = target.kind === "hypothesis" ? state.hypotheses.find(obj => obj.id === target.id)
    : target.kind === "gap" ? state.gaps.find(obj => obj.id === target.id) : state.links.find(obj => obj.id === target.id);
  const hypothesis = original as Hypothesis | undefined;
  const gap = original as EvidenceGap | undefined;
  const link = original as EvidenceLink | undefined;
  const [content, setContent] = useState(target.kind === "link" ? "" : (original as Hypothesis | EvidenceGap)?.content ?? "");
  const [judgment, setJudgment] = useState<Judgment>(hypothesis?.judgment ?? "unverified");
  const [workStatus, setWorkStatus] = useState<"active" | "deprecated">(hypothesis?.work_status ?? "active");
  const [gapStatus, setGapStatus] = useState<"open" | "resolved">(gap?.status ?? "open");
  const [hypothesisIds, setHypothesisIds] = useState<string[]>(gap?.hypothesis_ids ?? [target.hypothesisId ?? state.hypotheses[0]?.id].filter(Boolean) as string[]);
  const [infoIds, setInfoIds] = useState<string[]>(gap?.basis_info_unit_ids ?? []);
  const [addressingIds, setAddressingIds] = useState<string[]>(gap?.addressing_info_unit_ids ?? []);
  const [showAddressingPicker, setShowAddressingPicker] = useState(false);
  const [hypothesisId, setHypothesisId] = useState(link?.hypothesis_id ?? target.hypothesisId ?? state.hypotheses[0]?.id ?? "");
  const [infoId, setInfoId] = useState(link?.info_unit_id ?? target.infoUnitId ?? caseData.info_units[0]?.id ?? "");
  const [meaning, setMeaning] = useState<LinkMeaning>(link?.meaning ?? "related");
  const [rationale, setRationale] = useState(link?.rationale ?? "关联依据尚需进一步核实。");
  const label = target.kind === "hypothesis" ? "假设" : target.kind === "gap" ? "证据缺口" : "证据关联";
  const toggle = (ids: string[], id: string) => ids.includes(id) ? ids.filter(value => value !== id) : [...ids, id];
  const unitTitle = (id: string) => caseData.info_units.find(unit => unit.id === id)?.title ?? "材料已不可定位";
  const links = state.links.filter(item => item.hypothesis_id === target.id);
  function submit() {
    const edit: ReasoningEdit = { issue };
    if (target.kind === "hypothesis") edit.hypothesis = { id: target.id, content, judgment, work_status: workStatus };
    else if (target.kind === "gap") edit.gap = { id: target.id, content, hypothesis_ids: hypothesisIds, basis_info_unit_ids: infoIds, status: gapStatus,
      addressing_info_unit_ids: addressingIds.filter(id => !gap?.addressing_info_unit_ids.includes(id)),
      remove_addressing_info_unit_ids: (gap?.addressing_info_unit_ids ?? []).filter(id => !addressingIds.includes(id)) };
    else edit.link = { id: target.id, hypothesis_id: hypothesisId, info_unit_id: infoId, meaning, rationale };
    onSave(edit);
  }
  function remove() {
    if (!target.id || target.kind === "link") return;
    const relatedGaps = state.gaps.filter(item => item.hypothesis_ids.includes(target.id!));
    const exclusive = relatedGaps.filter(item => item.hypothesis_ids.length === 1).length;
    const warning = target.kind === "hypothesis"
      ? `同时移除 ${links.length} 条证据关联和 ${exclusive} 个仅关联此假设的证据缺口；${relatedGaps.length - exclusive} 个共享缺口会保留并解除与此假设的关联。`
      : "仅删除此缺口，关联假设会保留。";
    if (!window.confirm(`确认删除${label}“${target.kind === "hypothesis" ? hypothesis?.content : gap?.content}”？\n${warning}\n案件材料和原始来源不会删除。此操作不可撤销。`)) return;
    onSave({ issue, ...(target.kind === "hypothesis" ? { delete_hypothesis_id: target.id } : { delete_gap_id: target.id }) });
  }
  const canSave = target.kind === "link" ? Boolean(hypothesisId && infoId && rationale.trim())
    : Boolean(content.trim() && (target.kind !== "gap" || hypothesisIds.length));
  const dirty = target.kind === "hypothesis" ? content !== hypothesis?.content || judgment !== hypothesis?.judgment || workStatus !== hypothesis?.work_status
    : target.kind === "gap" ? content !== gap?.content || gapStatus !== gap?.status
      || JSON.stringify(hypothesisIds) !== JSON.stringify(gap?.hypothesis_ids) || JSON.stringify(infoIds) !== JSON.stringify(gap?.basis_info_unit_ids)
      || addressingIds.length !== (gap?.addressing_info_unit_ids.length ?? 0)
      || addressingIds.some(id => !gap?.addressing_info_unit_ids.includes(id)) : false;
  return <div className="info-unit-editor-backdrop" onMouseDown={event => event.stopPropagation()}>
    <section className="info-unit-editor reasoning-editor" role="dialog" aria-modal="true" aria-label={`${target.id ? "编辑" : "新建"}${label}`}>
      <header><h2>{target.id ? "编辑" : "新建"}{label}</h2><button type="button" disabled={busy} onClick={onClose}>关闭推理编辑器</button></header>
      <p>当前议题：{issue}</p>
      {target.kind !== "link" ? <label>{target.kind === "hypothesis" ? "假设内容" : "缺口问题"}
        <textarea aria-label={target.kind === "hypothesis" ? "假设内容" : "缺口问题"} value={content} maxLength={600} onChange={event => setContent(event.target.value)} /></label> : null}
      {target.kind === "hypothesis" ? <>
        <label>证据判断<select aria-label="证据判断" value={judgment} onChange={event => setJudgment(event.target.value as Judgment)}>
          {Object.entries(judgments).map(([value, text]) => <option key={value} value={value}>{text}</option>)}
        </select></label>
        <label>工作状态<select aria-label="假设工作状态" value={workStatus} onChange={event => setWorkStatus(event.target.value as typeof workStatus)}>
          <option value="active">活跃</option><option value="deprecated">已弃用（可恢复）</option>
        </select></label>
        <p className="reasoning-caution">缺少支持不等于证伪；找到陈述不等于已核实。判断由你决定。</p>
        {target.id ? <>
          <button type="button" disabled={busy} onClick={() => onEdit({ kind: "link", hypothesisId: target.id })}>关联已有证据</button>
          <button type="button" disabled={busy} onClick={() => onEdit({ kind: "gap", hypothesisId: target.id })}>添加证据缺口</button>
          <details><summary>查看证据与缺口</summary>
            {links.map(item => <article key={item.id}><button type="button" onClick={() => onLocate(item.info_unit_id)}>{unitTitle(item.info_unit_id)}</button>
              <p>{meanings[item.meaning]} · {item.origin === "agent" ? "Agent 建议已采纳" : "人工关联"}{item.last_edited_by === "user" && item.origin === "agent" ? " · 已由用户修正" : ""}</p>
              <p>{item.rationale}</p><button type="button" onClick={() => onEdit({ kind: "link", id: item.id })}>编辑关联</button></article>)}
            {state.gaps.filter(item => item.hypothesis_ids.includes(target.id!)).map(item => <button key={item.id} type="button" onClick={() => onEdit({ kind: "gap", id: item.id })}>{item.content} · {item.status === "open" ? "未解决" : "已解决"}</button>)}
          </details>
        </> : null}
      </> : null}
      {target.kind === "gap" ? <>
        <label>缺口状态<select aria-label="缺口状态" value={gapStatus} onChange={event => setGapStatus(event.target.value as typeof gapStatus)}><option value="open">未解决</option><option value="resolved">已解决</option></select></label>
        <fieldset><legend>关联假设（至少一个）</legend>{state.hypotheses.map(item => <label key={item.id}><input type="checkbox" checked={hypothesisIds.includes(item.id)} onChange={() => setHypothesisIds(toggle(hypothesisIds, item.id))} />{item.content}</label>)}</fieldset>
        <details><summary>缺口形成依据（可选）</summary>{caseData.info_units.map(item => <div key={item.id} className="gap-basis-item">
          <label><input type="checkbox" checked={infoIds.includes(item.id)} onChange={() => setInfoIds(toggle(infoIds, item.id))} />{item.title}</label>
          {infoIds.includes(item.id) ? <button type="button" onClick={() => onLocate(item.id)}>定位信息</button> : null}</div>)}</details>
        <section aria-label="用于回应缺口的信息"><h3>用于回应缺口的信息</h3>
          {addressingIds.length ? addressingIds.map(id => <div className="gap-basis-item" key={id}>
            <button type="button" onClick={() => onLocate(id)} disabled={busy}>{unitTitle(id)}</button>
            <button type="button" aria-label={`移除关联：${unitTitle(id)}`} disabled={busy}
              onClick={() => setAddressingIds(ids => ids.filter(value => value !== id))}>移除关联</button>
          </div>) : <p>尚未关联回应材料。</p>}
          {target.id ? <>
            <button type="button" disabled={busy} aria-expanded={showAddressingPicker}
              onClick={() => setShowAddressingPicker(value => !value)}>添加回应材料</button>
            {showAddressingPicker ? <fieldset><legend>选择回应材料</legend>
              {caseData.info_units.map(item =>
                <label key={item.id}><input type="checkbox"
                  disabled={busy || Boolean(gap?.addressing_info_unit_ids.includes(item.id) && addressingIds.includes(item.id))} checked={addressingIds.includes(item.id)}
                  onChange={() => setAddressingIds(ids => toggle(ids, item.id))} />{item.title}</label>)}
            </fieldset> : null}
          </> : <p>请先保存缺口，再添加回应材料。</p>}
          <p>关联材料不代表缺口已解决，状态由明确判断决定。</p>
        </section>
      </> : null}
      {target.kind === "link" ? <>
        <label>关联假设<select aria-label="关联假设" value={hypothesisId} onChange={event => setHypothesisId(event.target.value)}>{state.hypotheses.map(item => <option value={item.id} key={item.id}>{item.content}</option>)}</select></label>
        <label>案件信息<select aria-label="关联案件信息" value={infoId} onChange={event => setInfoId(event.target.value)}>{caseData.info_units.map(item => <option value={item.id} key={item.id}>{item.title}</option>)}</select></label>
        <label>关联语义<select aria-label="关联语义" value={meaning} onChange={event => setMeaning(event.target.value as LinkMeaning)}>{Object.entries(meanings).map(([value, text]) => <option key={value} value={value}>{text}</option>)}</select></label>
        <p className="reasoning-caution">相关：材料与假设有关，但不明确支持或反对。</p>
        <label>判断依据<textarea aria-label="关联判断依据" maxLength={600} value={rationale} onChange={event => setRationale(event.target.value)} /></label>
        <details><summary>核对来源原文</summary>{caseData.info_units.find(unit => unit.id === infoId)?.source_refs.map((ref, index) => <article key={index}>
          <button type="button" onClick={() => onLocate(infoId)}>{caseData.files.find(file => file.file_id === ref.file_id)?.filename ?? "来源材料"}</button><blockquote>{ref.quote}</blockquote>
        </article>)}</details>
        {target.id ? <button type="button" disabled={busy} onClick={() => onSave({ issue, unlink_id: target.id })}>解除关联</button> : null}
      </> : null}
      {error ? <p role="alert">{error}</p> : null}
      <footer><button type="button" disabled={busy || !canSave} onClick={submit}>{busy ? "保存中…" : "保存推理"}</button>
        {target.id && target.kind !== "link" ? <button type="button" disabled={busy || dirty} title={dirty ? "请先保存修改，再交给 Agent 检查" : undefined} onClick={() => onInvestigate(target.id!, `请检查${target.kind === "gap" ? "证据缺口" : "当前假设"}的支持、反对依据与尚需核实的问题。`)}>请 Agent 检查</button> : null}
        {target.id && target.kind !== "link" ? <button type="button" className="editor-delete" disabled={busy} onClick={remove}>删除{label}</button> : null}
      </footer>
    </section>
  </div>;
}
