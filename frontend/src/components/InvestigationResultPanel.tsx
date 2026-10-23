import type { AgentResult } from "../api";

const statuses = { completed: "调查完成", evidence_insufficient: "证据不足", budget_exhausted: "调查预算耗尽", unsupported: "暂不支持此操作" };

export function InvestigationResultPanel({ result, onLocate, onClose }: {
  result: AgentResult; onLocate: (id: string) => void; onClose: () => void;
}) {
  return <section className="agent-proposal investigation-result" aria-label="Agent 调查结果">
    <header><strong role="status">{statuses[result.completion_status]}</strong><button type="button" onClick={onClose}>关闭结果</button></header>
    <p>{result.goal}</p>
    <h3>调查发现</h3>
    {result.findings.length ? result.findings.map((finding, index) => <article key={index}>
      <p>{finding.statement}</p>
      {finding.info_unit_ids.map((id) => <button type="button" key={id} onClick={() => onLocate(id)}>定位 {id}</button>)}
      {finding.relation_ids.length ? <p>关系依据：{finding.relation_ids.join("、")}</p> : null}
      {finding.evidence_refs.map((ref) => <a key={`${ref.info_unit_id}:${ref.source_ref_index}`}
        href={`#agent-evidence-${ref.info_unit_id}-${ref.source_ref_index}`}>原文 {ref.info_unit_id} · {ref.source_ref_index + 1} </a>)}
    </article>) : <p>未形成有依据的调查发现。</p>}
    <h3>相关节点</h3>
    <div>{result.relevant_info_unit_ids.map((id) => <button key={id} type="button" onClick={() => onLocate(id)}>{id}</button>)}</div>
    <h3>来源原文</h3>
    <div className="agent-evidence-list">{result.evidence.map((ref) => <article
      id={`agent-evidence-${ref.info_unit_id}-${ref.source_ref_index}`} key={`${ref.info_unit_id}:${ref.source_ref_index}`}>
      <button type="button" onClick={() => onLocate(ref.info_unit_id)}>定位 {ref.info_unit_id}</button>
      <p>{ref.filename} · {ref.file_id} · [{ref.start}, {ref.end}) · {ref.source_kind}</p>
      <blockquote>{ref.quote}</blockquote>
    </article>)}</div>
    <h3>未解决问题与限制</h3>
    {result.open_questions.length ? <ul>{result.open_questions.map((question, i) => <li key={i}>{question}</li>)}</ul> : <p>本轮未报告其他问题；不代表全案已核实。</p>}
    <small>只读调查 · {result.tool_call_count} 次工具调用 · {result.run_id}</small>
  </section>;
}
