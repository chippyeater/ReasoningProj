import type { AgentResult, InfoUnit, Relation } from "../api";
import { displayText, groupSources, presentQuestion, sourceKinds } from "./investigationPresentation";
import { SummaryMarkdown } from "./SummaryMarkdown";

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
  const nodeButton = (id: string) => <button type="button" key={id} onClick={() => onLocate(id)}>{title(id)}</button>;
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
    {result.summary_status === "failed" ? <p role="alert">总结未完成，已取得的材料与暂存提案仍可查看。本轮未写入推理工作区。</p>
      : <SummaryMarkdown text={text(result.answer ?? "本轮未形成调查总结。")} />}
    <aside className="investigation-limit" aria-label="判断范围与限制">
      {result.answer_basis !== "general" && result.answer_basis !== "case_metadata" ? <>
        <p>材料记载或当事人陈述不等于已被独立核实。</p>
        {!result.evidence_refs.length ? <p>本轮尚未取得可定位的原文依据。</p> : null}
      </> : null}
      {limits.map(limit => <p key={limit.text}>{limit.text}</p>)}
      {result.completion_status === "budget_exhausted" && !limits.length ? <p>部分材料尚未核查，可以继续缩小问题范围。</p> : null}
    </aside>
    <details className="investigation-full"><summary>查看调查依据</summary>
      <p>调查问题：{text(result.goal)}</p>
      {sources.length ? <div><h3>来源原文</h3>{sources.map(source)}</div> : null}
      {nextQuestions.length ? <div><h3>下一步核实</h3><ul>{nextQuestions.map(question => <li key={question.text}>{question.text}</li>)}</ul></div> : null}
      <details><summary>相关信息</summary>{result.relevant_info_unit_ids.map(nodeButton)}</details>
      <p>共整理 {sources.length} 段来源原文；片段数量不代表独立证据数量。</p>
    </details>
  </section>;
}
