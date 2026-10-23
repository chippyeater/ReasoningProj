import type { AgentResult, InfoUnit, Relation } from "../api";
import { displayText, groupSources, presentQuestion, sourceKinds } from "./investigationPresentation";

const statuses = { completed: "本轮调查已结束", evidence_insufficient: "仍有材料待核实", budget_exhausted: "本轮核查暂告一段落", unsupported: "当前不支持此操作" };

export function InvestigationResultPanel({ result, infoUnits, relations, onLocate, onClose }: {
  result: AgentResult; infoUnits: InfoUnit[]; relations: Relation[];
  onLocate: (id: string) => void; onClose: () => void;
}) {
  const sources = groupSources(result.evidence);
  const title = (id: string) => infoUnits.find(unit => unit.id === id)?.title ?? "相关信息（已不可定位）";
  const text = (value: string) => displayText(value, infoUnits, result.evidence, relations);
  const questions = [...new Map([...result.open_questions, ...(result.limitations ?? [])].map(question => {
    const presented = presentQuestion(question);
    return [text(presented.text), { ...presented, text: text(presented.text) }];
  })).values()];
  const limits = questions.filter(question => question.limit);
  const nextQuestions = questions.filter(question => !question.limit);
  const firstFinding = result.findings[0];
  const primarySource = sources.find(group => firstFinding?.evidence_refs.some(pointer =>
    group.refs.some(ref => ref.info_unit_id === pointer.info_unit_id && ref.source_ref_index === pointer.source_ref_index)));
  const nodeButton = (id: string) => <button type="button" key={id} onClick={() => onLocate(id)}>{title(id)}</button>;
  function openSource(index: number) {
    const target = document.getElementById(`investigation-source-${index}`);
    for (let parent = target?.parentElement; parent; parent = parent.parentElement)
      if (parent instanceof HTMLDetailsElement) parent.open = true;
    target?.scrollIntoView({ block: "nearest" });
  }
  function finding(item: AgentResult["findings"][number], index: number) {
    const sourceIndexes = sources.flatMap((group, i) => item.evidence_refs.some(pointer => group.refs.some(ref =>
      ref.info_unit_id === pointer.info_unit_id && ref.source_ref_index === pointer.source_ref_index)) ? [i] : []);
    return <article className="investigation-finding" key={index}>
      <p className={index === 0 ? "investigation-answer" : ""}>{text(item.statement)}</p>
      <details><summary>查看判断依据</summary>
        {sourceIndexes.map(i => <button type="button" key={i} onClick={() => openSource(i)}>查看《{sources[i].source.filename}》原文</button>)}
        {item.info_unit_ids.map(nodeButton)}
        {item.relation_ids.map(id => {
          const relation = relations.find(item => item.id === id);
          const kind = { about: "关联", support: "支持", contradict: "矛盾" };
          return <p key={id}>{relation ? `「${title(relation.source_id)}」与「${title(relation.target_id)}」的${kind[relation.relation_type]}关系` : "相关关系已不可定位"}</p>;
        })}
        {!sourceIndexes.length ? <p>此发现尚未取得对应的原文引用。</p> : null}
      </details>
    </article>;
  }
  function source(group: typeof sources[number]) {
    const index = sources.indexOf(group);
    const ids = [...new Set(group.refs.map(ref => ref.info_unit_id))];
    return <article id={`investigation-source-${index}`} key={index} className="investigation-source">
      <button className="investigation-source-title" type="button" onClick={() => onLocate(ids[0])}>{group.source.filename}</button>
      <span className="investigation-source-kind">{sourceKinds[group.source.source_kind] ?? "来源材料"}</span>
      <blockquote>{group.source.quote}</blockquote>
      <details><summary>关联信息（{ids.length}）</summary>{ids.map(nodeButton)}
        {ids.length > 1 ? <p>这些信息共用此段原文，不构成多份独立佐证。</p> : null}
      </details>
    </article>;
  }
  return <section className="agent-proposal investigation-result" aria-label="Agent 调查结果">
    <header><strong role="status">{result.summary_status === "failed" ? "本轮总结未完成" : statuses[result.completion_status]}</strong><button type="button" onClick={onClose}>关闭结果</button></header>
    {result.summary_status === "failed" ? <p role="alert">总结未完成，已核查的发现与待确认建议仍可查看。建议尚未采纳。</p>
      : <p className="investigation-answer">{text(result.answer ?? firstFinding?.statement ?? "本轮未形成有依据的调查发现。")}</p>}
    <aside className="investigation-limit" aria-label="判断范围与限制">
      <p>依据现有材料整理，材料记载或当事人陈述不等于已被独立核实。</p>
      {firstFinding && !primarySource ? <p>主要发现尚未取得对应的原文依据。</p> : null}
      {!result.evidence_refs.length ? <p>本轮尚未提交可定位的原文依据，请继续核查。</p> : null}
      {limits.map(limit => <p key={limit.text}>{limit.text}</p>)}
      {result.completion_status === "budget_exhausted" && !limits.length ? <p>部分材料尚未核查，可以继续缩小问题范围。</p> : null}
    </aside>
    <details className="investigation-full"><summary>查看调查依据</summary>
      <p>调查问题：{text(result.goal)}</p>
      {result.findings.length ? <div><h3>已核查的发现</h3>{result.findings.map(finding)}</div> : null}
      {sources.length ? <div><h3>来源原文</h3>{sources.map(source)}</div> : null}
      {nextQuestions.length ? <div><h3>下一步核实</h3><ul>{nextQuestions.map(question => <li key={question.text}>{question.text}</li>)}</ul></div> : null}
      <details><summary>相关信息</summary>{result.relevant_info_unit_ids.map(nodeButton)}</details>
      <p>共整理 {sources.length} 段来源原文；片段数量不代表独立证据数量。</p>
    </details>
  </section>;
}
