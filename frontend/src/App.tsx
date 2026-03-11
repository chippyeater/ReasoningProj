import { useMemo, useState } from "react";
import { postReason, type ReasonResponse } from "./api";
import ConflictCompare from "./components/ConflictCompare";
import TimelineReasoning from "./components/TimelineReasoning";
import HypothesisBoard from "./components/HypothesisBoard";

const DEMO_CASE = `证词A：A 在 22:00 出现在仓库
证词B：A 在 22:00 在公司加班
监控：A 在 21:45 进入仓库
银行记录：A 在 22:10 收到转账`;

const DEMO_QUESTION = "A 在 22:00 更可能在哪里？请给出证据链和冲突点。";

export default function App() {
  const [caseText, setCaseText] = useState(DEMO_CASE);
  const [question, setQuestion] = useState(DEMO_QUESTION);
  const [result, setResult] = useState<ReasonResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  // Pick UI block by recommended_view.
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

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError("");
    try {
      const data = await postReason({ case_text: caseText, question });
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
              <textarea value={caseText} onChange={(e) => setCaseText(e.target.value)} rows={10} />
            </label>
            <label>
              推理问题
              <textarea value={question} onChange={(e) => setQuestion(e.target.value)} rows={4} />
            </label>
            <button disabled={loading} type="submit">
              {loading ? "分析中..." : "提交推理"}
            </button>
          </form>
          {error ? <p className="error">请求失败: {error}</p> : null}
        </section>

        <section className="panel right">
          <h2>Output</h2>
          {!result ? <p>提交后将显示结构化推理结果。</p> : null}
          {result ? (
            <>
              <p><strong>Summary:</strong> {result.summary}</p>
              <h3>Entities</h3>
              <ul>
                {result.entities.map((entity, idx) => (
                  <li key={idx}>
                    <pre>{JSON.stringify(entity, null, 2)}</pre>
                  </li>
                ))}
              </ul>
              {view}
            </>
          ) : null}
        </section>
      </div>
    </main>
  );
}
