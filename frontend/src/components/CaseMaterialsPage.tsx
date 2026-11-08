import { useEffect, useRef, useState } from "react";
import { getCaseMaterials } from "../api";
import "./CaseMaterialsPage.css";

export function CaseMaterialsPage({ caseId, caseTitle, onClose }: {
  caseId: string; caseTitle: string; onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const request = useRef<AbortController | null>(null);
  const [html, setHtml] = useState("");
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");

  async function generate() {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setError("");
    try {
      const artifact = await getCaseMaterials(caseId, controller.signal);
      if (!controller.signal.aborted) setHtml(artifact);
    } catch (err) {
      if (!controller.signal.aborted) setError(err instanceof Error ? err.message : "无法生成案件材料，请重试。");
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  useEffect(() => {
    dialog.current?.showModal();
    setHtml("");
    void generate();
    return () => { request.current?.abort(); };
  }, [caseId]);

  return <dialog ref={dialog} className="materials-page" aria-label="案件材料阅读器"
    onCancel={event => { event.preventDefault(); onClose(); }}>
    <header className="materials-toolbar">
      <button type="button" onClick={onClose}>← 返回工作台</button>
      <div><strong>案件材料</strong><span>{caseTitle}</span></div>
      <button type="button" disabled={busy} onClick={() => void generate()}>{busy ? "正在生成…" : "重新生成"}</button>
    </header>
    {busy ? <p className="materials-message" role="status">正在读取全部 Issue 的已保存状态…{html ? "当前仍显示上次材料包。" : ""}</p> : null}
    {error ? <div className="materials-message materials-error" role="alert">
      <p>生成失败：{error}{html ? " 当前仍显示上次材料包。" : ""}</p>
      <button type="button" onClick={() => void generate()}>重试生成</button>
    </div> : null}
    {html ? <iframe className="materials-frame" title="HTML 案件档案" srcDoc={html}
      sandbox="allow-scripts" referrerPolicy="no-referrer" /> : null}
  </dialog>;
}
