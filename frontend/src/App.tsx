import { useMemo, useState } from "react";
import {
  postReason,
  type Claim,
  type Entity,
  type EvidenceInput,
  type EvidenceItem,
  type Event,
  type ReasonResponse,
  type Relation,
} from "./api";
import ConflictCompare from "./components/ConflictCompare";
import TimelineReasoning from "./components/TimelineReasoning";
import HypothesisBoard from "./components/HypothesisBoard";

const DEMO_CASE = `证词A：A 在 22:00 出现在仓库
证词B：A 在 22:00 在公司加班
监控：A 在 21:45 进入仓库
银行记录：A 在 22:10 收到转账`;

const DEMO_QUESTION = "A 在 22:00 更可能在哪里？请给出证据链和冲突点。";

const EVIDENCE_TYPES: EvidenceInput["type"][] = ["text", "document", "image", "video", "audio"];

const DEFAULT_EVIDENCE: EvidenceInput = {
  type: "text",
  name: "补充证据",
  content: "",
  notes: "",
};

type ParsedEvidenceView = {
  id?: string;
  name?: string;
  type?: string;
  parser_tool?: string;
  normalized_text?: string;
  metadata?: {
    parse_status?: string;
    parser_detail?: string;
    file_name?: string | null;
    mime_type?: string | null;
    notes?: string;
  };
};

type AddedEvidence =
  | {
      id: string;
      kind: "text";
      name: string;
      type: "text";
      content: string;
      notes?: string;
    }
  | {
      id: string;
      kind: "file";
      name: string;
      type: "document" | "image" | "video" | "audio";
      file: File;
      notes?: string;
    };

function statusLabel(status?: string) {
  if (status === "success") return "解析成功";
  if (status === "partial") return "部分解析";
  if (status === "unsupported") return "暂不支持";
  return "状态未知";
}

function getFileAccept(type: EvidenceInput["type"]) {
  if (type === "document") return ".txt,.md,.pdf,.docx";
  if (type === "image") return "image/*";
  if (type === "video") return "video/*";
  if (type === "audio") return "audio/*";
  return "";
}

function renderKeyValueRows(rows: Array<[string, string | boolean | string[]]>) {
  return rows.map(([label, value]) => {
    const text = Array.isArray(value) ? value.join(", ") : String(value || "-");
    return (
      <p key={label}>
        {label}: {text || "-"}
      </p>
    );
  });
}

function firstLine(text?: string) {
  return (text ?? "").split(/\r?\n/, 1)[0] ?? "";
}

export default function App() {
  const [caseText, setCaseText] = useState(DEMO_CASE);
  const [question, setQuestion] = useState(DEMO_QUESTION);
  const [draftEvidence, setDraftEvidence] = useState<EvidenceInput>(DEFAULT_EVIDENCE);
  const [draftFile, setDraftFile] = useState<File | null>(null);
  const [evidences, setEvidences] = useState<AddedEvidence[]>([
    {
      id: "demo-text-1",
      kind: "text",
      type: "text",
      name: "监控补充说明",
      content: "监控截图文字：A 于 21:45 进入仓库入口。",
      notes: "手工录入的演示证据",
    },
  ]);
  const [result, setResult] = useState<ReasonResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const view = useMemo(() => {
    if (!result) return null;
    if (result.recommended_view === "timeline_reasoning") {
      return <TimelineReasoning data={result} />;
    }
    if (result.recommended_view === "hypothesis_board") {
      return <HypothesisBoard data={result} />;
    }
    return <ConflictCompare data={result} />;
  }, [result]);

  const parsedEvidences = (result?.parsed_evidences ?? []) as ParsedEvidenceView[];
  const evidenceItems: EvidenceItem[] = result?.evidence_items ?? [];
  const entities: Entity[] = result?.entities ?? [];
  const relations: Relation[] = result?.relations ?? [];
  const events: Event[] = result?.events ?? [];
  const claims: Claim[] = result?.claims ?? [];
  const fallbackReason = result?.fallback_reason?.trim() ?? "";
  const fallbackDetail = result?.llm_log?.error?.trim() ?? "";
  const promptSystem = result?.llm_log?.prompt_system?.trim() ?? "";
  const promptUser = result?.llm_log?.prompt_user?.trim() ?? "";
  const rawContent = result?.llm_log?.raw_content?.trim() ?? "";
  const isFallback = Boolean(result && !result.llm_used);
  const isTextDraft = draftEvidence.type === "text";

  function updateDraft<K extends keyof EvidenceInput>(key: K, value: EvidenceInput[K]) {
    setDraftEvidence((current) => ({ ...current, [key]: value }));
    if (key === "type") {
      setDraftFile(null);
    }
  }

  function onDraftFileChange(event: React.ChangeEvent<HTMLInputElement>) {
    setDraftFile(event.target.files?.[0] ?? null);
  }

  function addEvidence() {
    const name = draftEvidence.name?.trim() ?? "";
    const draftType = draftEvidence.type;

    if (!name) {
      setError("证据名称不能为空。");
      return;
    }

    if (draftType === "text") {
      const content = draftEvidence.content?.trim() ?? "";
      if (!content) {
        setError("text 类型必须填写证据内容。");
        return;
      }

      setEvidences((current) => [
        ...current,
        {
          id: `evidence-${Date.now()}`,
          kind: "text",
          type: "text",
          name,
          content,
          notes: draftEvidence.notes?.trim() ?? "",
        },
      ]);
    } else {
      if (!draftFile) {
        setError("当前证据类型需要选择一个文件。");
        return;
      }

      setEvidences((current) => [
        ...current,
        {
          id: `evidence-${Date.now()}`,
          kind: "file",
          type: draftType,
          name,
          file: draftFile,
          notes: draftEvidence.notes?.trim() ?? "",
        },
      ]);
    }

    setDraftEvidence(DEFAULT_EVIDENCE);
    setDraftFile(null);
    setError("");
  }

  function removeEvidence(id: string) {
    setEvidences((current) => current.filter((item) => item.id !== id));
  }

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setLoading(true);
    setError("");

    const manualEvidences: EvidenceInput[] = evidences
      .filter((item): item is Extract<AddedEvidence, { kind: "text" }> => item.kind === "text")
      .map((item) => ({
        id: item.id,
        type: "text",
        name: item.name,
        content: item.content,
        notes: item.notes,
      }));

    const files = evidences
      .filter((item): item is Extract<AddedEvidence, { kind: "file" }> => item.kind === "file")
      .map((item) => item.file);

    try {
      const data = await postReason(
        {
          case_text: caseText,
          question,
          evidences: manualEvidences,
        },
        files
      );
      setResult(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unknown error");
      setResult(null);
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="page">
      <h1>Reasoning Interface Generator</h1>
      <div className="layout">
        <section className="panel left">
          <h2>Input</h2>
          <form onSubmit={onSubmit}>
            <label>
              案件材料
              <textarea rows={10} value={caseText} onChange={(e) => setCaseText(e.target.value)} />
            </label>
            <label>
              推理问题
              <textarea rows={4} value={question} onChange={(e) => setQuestion(e.target.value)} />
            </label>

            <div className="evidence-editor">
              <h3>添加证据</h3>
              <label>
                证据类型
                <select value={draftEvidence.type} onChange={(e) => updateDraft("type", e.target.value as EvidenceInput["type"])}>
                  {EVIDENCE_TYPES.map((type) => (
                    <option key={type} value={type}>
                      {type}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                证据名称
                <input value={draftEvidence.name ?? ""} onChange={(e) => updateDraft("name", e.target.value)} />
              </label>
              {isTextDraft ? (
                <label>
                  证据内容
                  <textarea rows={5} value={draftEvidence.content ?? ""} onChange={(e) => updateDraft("content", e.target.value)} />
                </label>
              ) : (
                <label>
                  上传文件
                  <input accept={getFileAccept(draftEvidence.type)} type="file" onChange={onDraftFileChange} />
                </label>
              )}
              {!isTextDraft && draftFile ? <p className="hint">已选择文件: {draftFile.name}</p> : null}
              {!isTextDraft ? <p className="hint">文件会在提交推理时上传，并由后端做解析。</p> : null}
              <label>
                备注
                <textarea rows={2} value={draftEvidence.notes ?? ""} onChange={(e) => updateDraft("notes", e.target.value)} />
              </label>
              <button className="secondary" type="button" onClick={addEvidence}>
                添加证据
              </button>
            </div>

            <div className="evidence-list">
              <h3>已添加证据</h3>
              {evidences.length === 0 ? <p>当前没有证据。</p> : null}
              {evidences.map((item) => (
                <article className="evidence-card" key={item.id}>
                  <div className="evidence-card-header">
                    <strong>{item.name}</strong>
                    <button className="danger" type="button" onClick={() => removeEvidence(item.id)}>
                      删除
                    </button>
                  </div>
                  <p>类型: {item.type}</p>
                  {item.kind === "text" ? <pre>{firstLine(item.content)}</pre> : <p>文件: {item.file.name}</p>}
                </article>
              ))}
            </div>

            <button disabled={loading} type="submit">
              {loading ? "分析中..." : "提交推理"}
            </button>
          </form>
          {error ? <p className="error">请求失败: {error}</p> : null}
        </section>

        <section className="panel right">
          <h2>Output</h2>
          {!result ? <p>提交后这里会显示结构化推理结果。</p> : null}
          {result ? (
            <>
              {isFallback ? (
                <div className="fallback-alert">
                  <strong>当前结果来自 fallback，不是 LLM 的正式推理结果。</strong>
                  <p>fallback_reason: {fallbackReason || "unknown"}</p>
                  <p>error_detail: {fallbackDetail || "none"}</p>
                </div>
              ) : null}

              <p>
                <strong>Summary:</strong> {result.summary}
              </p>

              <div className="evidence-list">
                <h3>EvidenceItem</h3>
                {evidenceItems.length === 0 ? <p>没有 EvidenceItem。</p> : null}
                {evidenceItems.map((item) => (
                  <article className="evidence-card" key={item.id}>
                    <div className="evidence-card-header">
                      <strong>{item.id}</strong>
                      <span className="status-badge status-success">{item.type}</span>
                    </div>
                    {renderKeyValueRows([
                      ["来源文件", item.source_file],
                      ["页码/段落", item.page_or_paragraph],
                      ["时间", item.time],
                      ["说话人/生产者", item.producer_or_speaker],
                      ["是否原始证据", item.is_original_evidence],
                      ["备注", item.notes],
                    ])}
                    <pre>{firstLine(item.original_content)}</pre>
                  </article>
                ))}
              </div>

              <div className="evidence-list">
                <h3>Entity</h3>
                {entities.length === 0 ? <p>没有 Entity。</p> : null}
                {entities.map((entity) => (
                  <article className="evidence-card" key={entity.id}>
                    <div className="evidence-card-header">
                      <strong>{entity.name}</strong>
                      <span className="status-badge status-success">{entity.type}</span>
                    </div>
                    {renderKeyValueRows([
                      ["ID", entity.id],
                      ["别名", entity.aliases],
                      ["来源证据", entity.source_evidence_ids],
                    ])}
                  </article>
                ))}
              </div>

              <div className="evidence-list">
                <h3>Relation</h3>
                {relations.length === 0 ? <p>没有 Relation。</p> : null}
                {relations.map((relation) => (
                  <article className="evidence-card" key={relation.id}>
                    <div className="evidence-card-header">
                      <strong>{relation.relation_type}</strong>
                      <span
                        className={`status-badge status-${
                          relation.confidence_status === "high"
                            ? "success"
                            : relation.confidence_status === "medium"
                              ? "partial"
                              : "unknown"
                        }`}
                      >
                        {relation.confidence_status}
                      </span>
                    </div>
                    {renderKeyValueRows([
                      ["ID", relation.id],
                      ["主体", relation.subject_entity],
                      ["客体", relation.object_entity],
                      ["时间", relation.time],
                      ["证据来源", relation.evidence_sources],
                    ])}
                  </article>
                ))}
              </div>

              <div className="evidence-list">
                <h3>Event</h3>
                {events.length === 0 ? <p>没有 Event。</p> : null}
                {events.map((item) => (
                  <article className="evidence-card" key={item.id}>
                    <div className="evidence-card-header">
                      <strong>{item.event_type}</strong>
                      <span className="status-badge status-success">{item.id}</span>
                    </div>
                    {renderKeyValueRows([
                      ["参与实体", item.participant_entities],
                      ["时间", item.time],
                      ["地点", item.location],
                      ["来源证据", item.source_evidence_ids],
                    ])}
                    <pre>{item.description}</pre>
                  </article>
                ))}
              </div>

              <div className="evidence-list">
                <h3>Claim</h3>
                {claims.length === 0 ? <p>没有 Claim。</p> : null}
                {claims.map((item) => (
                  <article className="evidence-card" key={item.id}>
                    <div className="evidence-card-header">
                      <strong>{item.source || item.id}</strong>
                      <span
                        className={`status-badge status-${
                          item.credibility_status === "high"
                            ? "success"
                            : item.credibility_status === "medium"
                              ? "partial"
                              : "unknown"
                        }`}
                      >
                        {item.stance}
                      </span>
                    </div>
                    {renderKeyValueRows([
                      ["ID", item.id],
                      ["可信状态", item.credibility_status],
                      ["指向对象", item.target_ids],
                    ])}
                    <pre>{item.content}</pre>
                    {item.quote ? <pre>{item.quote}</pre> : null}
                  </article>
                ))}
              </div>

              <div className="evidence-list">
                <h3>LLM Log</h3>
                {renderKeyValueRows([
                  ["provider", result.llm_log?.provider ?? "-"],
                  ["model", result.llm_log?.model ?? "-"],
                  ["endpoint", result.llm_log?.endpoint ?? "-"],
                  ["fallback_reason", result.llm_log?.fallback_reason ?? "-"],
                  ["error", result.llm_log?.error ?? "-"],
                ])}
                {promptSystem ? (
                  <>
                    <p>system prompt</p>
                    <pre>{promptSystem}</pre>
                  </>
                ) : null}
                {promptUser ? (
                  <>
                    <p>user prompt</p>
                    <pre>{promptUser}</pre>
                  </>
                ) : null}
                {rawContent ? (
                  <>
                    <p>model raw content</p>
                    <pre>{rawContent}</pre>
                  </>
                ) : null}
              </div>

              {view}

              <div className="evidence-list">
                <h3>解析结果</h3>
                {parsedEvidences.length === 0 ? <p>没有可展示的解析结果。</p> : null}
                {parsedEvidences.map((item, idx) => (
                  <article className="evidence-card" key={item.id ?? idx}>
                    <div className="evidence-card-header">
                      <strong>{item.name ?? "Unnamed evidence"}</strong>
                      <span className={`status-badge status-${item.metadata?.parse_status ?? "unknown"}`}>
                        {statusLabel(item.metadata?.parse_status)}
                      </span>
                    </div>
                    {renderKeyValueRows([
                      ["类型", item.type ?? "-"],
                      ["解析工具", item.parser_tool ?? "-"],
                      ["文件名", item.metadata?.file_name ?? "-"],
                      ["说明", item.metadata?.parser_detail ?? item.metadata?.notes ?? "-"],
                    ])}
                    <pre>{firstLine(item.normalized_text)}</pre>
                  </article>
                ))}
              </div>
            </>
          ) : null}
        </section>
      </div>
    </main>
  );
}
