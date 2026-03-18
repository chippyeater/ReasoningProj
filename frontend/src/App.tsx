import { useEffect, useMemo, useState } from "react";
import {
  askCaseQuestion,
  createCase,
  deleteCase,
  getCase,
  type CaseQuestionResponse,
  type Claim,
  type CurrentCase,
  type Entity,
  type EvidenceInput,
  type EvidenceItem,
  type Event,
  type ParsedEvidence,
  type PipelineLog,
  type QuestionViewData,
  type Relation,
} from "./api";
import ConflictCompare from "./components/ConflictCompare";
import TimelineReasoning from "./components/TimelineReasoning";
import HypothesisBoard from "./components/HypothesisBoard";

const DEMO_CASE = `请输入案件介绍，包括案件背景、涉及人员、关键时间线等基本信息。你也可以上传相关证据材料。提交后，系统会先做证据解析和结构化抽取。`;
const DEMO_QUESTION = "例如：案件中有哪些关键事件？哪些证据彼此冲突？";
const EVIDENCE_TYPES: EvidenceInput["type"][] = ["text", "document", "image", "video", "audio"];

const DEFAULT_EVIDENCE: EvidenceInput = {
  type: "text",
  name: "",
  content: "",
  notes: "",
};

type EvidenceStatus = "pending" | "submitted" | "success";

type AddedEvidence =
  | {
      id: string;
      kind: "text";
      status: EvidenceStatus;
      name: string;
      type: "text";
      content: string;
      notes?: string;
    }
  | {
      id: string;
      kind: "file";
      status: EvidenceStatus;
      name: string;
      type: "document" | "image" | "video" | "audio";
      file: File;
      notes?: string;
    };

function firstLine(text?: string) {
  return (text ?? "").split(/\r?\n/, 1)[0] ?? "";
}

function parseStatusLabel(status?: string) {
  if (status === "success") return "解析成功";
  if (status === "partial") return "部分解析";
  if (status === "unsupported") return "暂不支持";
  return "状态未知";
}

function evidenceStatusLabel(status: EvidenceStatus) {
  if (status === "submitted") return "submitted";
  if (status === "success") return "success";
  return "pending";
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

function PipelineLogView({ title, log }: { title: string; log?: PipelineLog }) {
  if (!log) return null;
  return (
    <div className="evidence-list">
      <h3>{title}</h3>
      {renderKeyValueRows([
        ["provider", log.provider],
        ["model", log.model],
        ["endpoint", log.endpoint],
        ["pipeline_llm_used", log.pipeline_llm_used],
        ["fallback_reason", log.fallback_reason],
      ])}
      {log.stages.map((stage) => (
        <article className="evidence-card" key={stage.stage_name}>
          <div className="evidence-card-header">
            <strong>{stage.stage_name}</strong>
            <span className={`status-badge status-${stage.fallback_used ? "unknown" : "success"}`}>
              {stage.fallback_used ? "fallback" : "llm"}
            </span>
          </div>
          {renderKeyValueRows([
            ["llm_used", stage.llm_used],
            ["fallback_used", stage.fallback_used],
            ["fallback_reason", stage.fallback_reason],
            ["error", stage.error],
          ])}
          {stage.prompt_system ? <pre>{stage.prompt_system}</pre> : null}
          {stage.prompt_user ? <pre>{stage.prompt_user}</pre> : null}
          {stage.raw_content ? <pre>{stage.raw_content}</pre> : null}
        </article>
      ))}
    </div>
  );
}

export default function App() {
  const [caseText, setCaseText] = useState(DEMO_CASE);
  const [question, setQuestion] = useState(DEMO_QUESTION);
  const [draftEvidence, setDraftEvidence] = useState<EvidenceInput>(DEFAULT_EVIDENCE);
  const [draftFile, setDraftFile] = useState<File | null>(null);
  const [evidences, setEvidences] = useState<AddedEvidence[]>([]);
  const [currentCase, setCurrentCase] = useState<CurrentCase | null>(null);
  const [questionResult, setQuestionResult] = useState<CaseQuestionResponse | null>(null);
  const [caseLoading, setCaseLoading] = useState(false);
  const [questionLoading, setQuestionLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    void (async () => {
      try {
        const data = await getCase();
        setCurrentCase(data.case);
      } catch {
        // Ignore initial load failures.
      }
    })();
  }, []);

  const entities: Entity[] = currentCase?.entities ?? [];
  const relations: Relation[] = currentCase?.relations ?? [];
  const events: Event[] = currentCase?.events ?? [];
  const claims: Claim[] = currentCase?.claims ?? [];
  const evidenceItems: EvidenceItem[] = currentCase?.evidence_items ?? [];
  const parsedEvidences: ParsedEvidence[] = currentCase?.parsed_evidences ?? [];

  const questionViewData = useMemo<QuestionViewData | null>(() => {
    if (!currentCase || !questionResult) return null;
    return {
      events: currentCase.events,
      claims: currentCase.claims,
      conflicts: questionResult.conflicts,
      evidence_paths: questionResult.evidence_paths,
      recommended_view: questionResult.recommended_view,
    };
  }, [currentCase, questionResult]);

  const recommendedView = useMemo(() => {
    if (!questionViewData) return null;
    if (questionViewData.recommended_view === "timeline_reasoning") {
      return <TimelineReasoning data={questionViewData} />;
    }
    if (questionViewData.recommended_view === "hypothesis_board") {
      return <HypothesisBoard data={questionViewData} />;
    }
    return <ConflictCompare data={questionViewData} />;
  }, [questionViewData]);

  function updateDraft<K extends keyof EvidenceInput>(key: K, value: EvidenceInput[K]) {
    setDraftEvidence((current) => ({ ...current, [key]: value }));
    if (key === "type") {
      setDraftFile(null);
    }
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
        setError("文本证据必须填写内容。");
        return;
      }
      setEvidences((current) => [
        ...current,
        {
          id: `evidence-${Date.now()}`,
          kind: "text",
          status: "pending",
          type: "text",
          name,
          content,
          notes: draftEvidence.notes?.trim() ?? "",
        },
      ]);
    } else {
      if (!draftFile) {
        setError("当前证据类型需要选择文件。");
        return;
      }
      setEvidences((current) => [
        ...current,
        {
          id: `evidence-${Date.now()}`,
          kind: "file",
          status: "pending",
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

  function buildDraftEvidenceForSubmit(): AddedEvidence | null {
    const name = draftEvidence.name?.trim() ?? "";
    const notes = draftEvidence.notes?.trim() ?? "";

    if (draftEvidence.type === "text") {
      const content = draftEvidence.content?.trim() ?? "";
      if (!name && !content && !notes) return null;
      if (!name || !content) return null;
      return {
        id: `draft-evidence-${Date.now()}`,
        kind: "text",
        status: "pending",
        type: "text",
        name,
        content,
        notes,
      };
    }

    if (!name && !draftFile && !notes) return null;
    if (!name || !draftFile) return null;
    return {
      id: `draft-evidence-${Date.now()}`,
      kind: "file",
      status: "pending",
      type: draftEvidence.type,
      name,
      file: draftFile,
      notes,
    };
  }

  async function onSubmitCase(event: React.FormEvent) {
    event.preventDefault();
    setCaseLoading(true);
    setError("");

    const draftEvidenceForSubmit = buildDraftEvidenceForSubmit();
    const hasIncompleteDraft = !draftEvidenceForSubmit && Boolean(
      draftEvidence.name?.trim() || draftEvidence.content?.trim() || draftEvidence.notes?.trim() || draftFile
    );
    if (hasIncompleteDraft) {
      setError("当前草稿证据尚未完整添加，请先点击“添加证据”或补全后再提交。");
      setCaseLoading(false);
      return;
    }

    const submissionEvidences = draftEvidenceForSubmit ? [...evidences, draftEvidenceForSubmit] : evidences;
    if (!caseText.trim() && submissionEvidences.length === 0) {
      setError("请至少填写案件介绍或添加一条证据。");
      setCaseLoading(false);
      return;
    }

    setEvidences(submissionEvidences.map((item) => ({ ...item, status: "submitted" as const })));

    const manualEvidences: EvidenceInput[] = submissionEvidences
      .filter((item): item is Extract<AddedEvidence, { kind: "text" }> => item.kind === "text")
      .map((item) => ({
        id: item.id,
        type: "text",
        name: item.name,
        content: item.content,
        notes: item.notes,
      }));

    const files = submissionEvidences
      .filter((item): item is Extract<AddedEvidence, { kind: "file" }> => item.kind === "file")
      .map((item) => item.file);

    try {
      const response = await createCase(caseText, manualEvidences, files);
      setCurrentCase(response.case);
      setQuestionResult(null);
      setEvidences(submissionEvidences.map((item) => ({ ...item, status: "success" as const })));
      if (draftEvidenceForSubmit) {
        setDraftEvidence(DEFAULT_EVIDENCE);
        setDraftFile(null);
      }
    } catch (err) {
      setEvidences(submissionEvidences.map((item) => ({ ...item, status: "pending" as const })));
      setError(err instanceof Error ? err.message : "Unknown error");
    } finally {
      setCaseLoading(false);
    }
  }

  async function onSubmitQuestion(event: React.FormEvent) {
    event.preventDefault();
    setQuestionLoading(true);
    setError("");

    try {
      const response = await askCaseQuestion(question);
      setQuestionResult(response);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unknown error");
    } finally {
      setQuestionLoading(false);
    }
  }

  async function onClearCase() {
    setError("");
    await deleteCase();
    setCurrentCase(null);
    setQuestionResult(null);
    setEvidences([]);
  }

  return (
    <main className="page">
      <h1>Reasoning Interface Generator</h1>
      <div className="layout">
        <section className="panel left">
          <h2>Case Input</h2>
          <form onSubmit={onSubmitCase}>
            <label>
              案件介绍
              <textarea rows={10} value={caseText} onChange={(e) => setCaseText(e.target.value)} />
            </label>

            <div className="evidence-editor">
              <h3>提交证据</h3>
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
              {draftEvidence.type === "text" ? (
                <label>
                  证据内容
                  <textarea rows={5} value={draftEvidence.content ?? ""} onChange={(e) => updateDraft("content", e.target.value)} />
                </label>
              ) : (
                <label>
                  上传文件
                  <input
                    accept={getFileAccept(draftEvidence.type)}
                    type="file"
                    onChange={(e) => setDraftFile(e.target.files?.[0] ?? null)}
                  />
                </label>
              )}
              <label>
                备注
                <textarea rows={2} value={draftEvidence.notes ?? ""} onChange={(e) => updateDraft("notes", e.target.value)} />
              </label>
              <button className="secondary" type="button" onClick={addEvidence}>
                添加证据
              </button>
            </div>

            <div className="evidence-list">
              <h3>当前案件证据</h3>
              {evidences.length === 0 ? <p>当前没有案件证据。</p> : null}
              {evidences.map((item) => (
                <article className="evidence-card" key={item.id}>
                  <div className="evidence-card-header">
                    <strong>{item.name}</strong>
                    <div className="evidence-actions">
                      <span
                        className={`status-badge status-${
                          item.status === "success" ? "success" : item.status === "submitted" ? "partial" : "unknown"
                        }`}
                      >
                        {evidenceStatusLabel(item.status)}
                      </span>
                      {item.status === "pending" ? (
                        <button className="danger" type="button" onClick={() => removeEvidence(item.id)}>
                          删除
                        </button>
                      ) : null}
                    </div>
                  </div>
                  <p>类型: {item.type}</p>
                  {item.kind === "text" ? <pre>{firstLine(item.content)}</pre> : <p>文件: {item.file.name}</p>}
                </article>
              ))}
            </div>

            <button disabled={caseLoading} type="submit">
              {caseLoading ? "提交证据中..." : "提交证据"}
            </button>
            <button className="secondary" type="button" onClick={() => void onClearCase()}>
              清空当前案件
            </button>
          </form>
          {error ? <p className="error">请求失败: {error}</p> : null}
        </section>

        <section className="panel right">
          <h2>Question & Output</h2>
          <form onSubmit={onSubmitQuestion}>
            <label>
              用户问题
              <textarea rows={4} value={question} onChange={(e) => setQuestion(e.target.value)} />
            </label>
            <button disabled={questionLoading || !currentCase} type="submit">
              {questionLoading ? "提交问题中..." : "提交问题"}
            </button>
          </form>

          {!currentCase ? <p>先在左侧提交案件介绍和证据。</p> : null}

          {currentCase ? (
            <>
              <p>
                <strong>Current Case:</strong> {currentCase.case_id}
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
                      <span className="status-badge status-success">{relation.confidence_status}</span>
                    </div>
                    {renderKeyValueRows([
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
                      <span className="status-badge status-success">{item.stance}</span>
                    </div>
                    {renderKeyValueRows([
                      ["可信状态", item.credibility_status],
                      ["指向对象", item.target_ids],
                    ])}
                    <pre>{item.content}</pre>
                    {item.quote ? <pre>{item.quote}</pre> : null}
                  </article>
                ))}
              </div>

              <PipelineLogView title="Extraction Log" log={currentCase.extraction_log} />

              <div className="evidence-list">
                <h3>解析结果</h3>
                {parsedEvidences.length === 0 ? <p>没有可展示的解析结果。</p> : null}
                {parsedEvidences.map((item) => (
                  <article className="evidence-card" key={item.id}>
                    <div className="evidence-card-header">
                      <strong>{item.name}</strong>
                      <span className={`status-badge status-${item.metadata?.parse_status ?? "unknown"}`}>
                        {parseStatusLabel(item.metadata?.parse_status)}
                      </span>
                    </div>
                    {renderKeyValueRows([
                      ["类型", item.type],
                      ["解析工具", item.parser_tool],
                      ["文件名", item.metadata?.file_name ?? "-"],
                      ["说明", item.metadata?.parser_detail ?? item.metadata?.notes ?? "-"],
                    ])}
                    <pre>{firstLine(item.normalized_text)}</pre>
                  </article>
                ))}
              </div>

              {questionResult ? (
                <>
                  <p>
                    <strong>Summary:</strong> {questionResult.summary}
                  </p>
                  <PipelineLogView title="Question Reasoning Log" log={questionResult.reasoning_log} />
                  {recommendedView}
                </>
              ) : (
                <p>提交问题后，这里会显示 conflicts、evidence_paths 和推荐视图。</p>
              )}
            </>
          ) : null}
        </section>
      </div>
    </main>
  );
}
