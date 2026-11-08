import { useEffect, useRef, useState } from "react";
import { getCaseMaterials, getCaseMaterialsZip } from "../api";
import "./CaseMaterialsPage.css";

export function CaseMaterialsPage({ caseId, caseTitle, onClose }: {
  caseId: string; caseTitle: string; onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const request = useRef<AbortController | null>(null);
  const exportRequest = useRef<AbortController | null>(null);
  const [html, setHtml] = useState("");
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [exportBusy, setExportBusy] = useState(false);
  const [exportError, setExportError] = useState("");

  async function download() {
    exportRequest.current?.abort();
    const controller = new AbortController();
    exportRequest.current = controller;
    setExportBusy(true);
    setExportError("");
    try {
      const archive = await getCaseMaterialsZip(caseId, controller.signal);
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(archive);
      const link = document.createElement("a");
      link.href = url;
      link.download = "case-materials.zip";
      dialog.current?.append(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (err) {
      if (!controller.signal.aborted) setExportError(err instanceof Error ? err.message : "无法下载案件材料包，请重试。");
    } finally {
      if (!controller.signal.aborted) setExportBusy(false);
    }
  }

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
    return () => { request.current?.abort(); exportRequest.current?.abort(); };
  }, [caseId]);

  return <dialog ref={dialog} className="materials-page" aria-label="案件材料阅读器"
    onCancel={event => { event.preventDefault(); onClose(); }}>
    <header className="materials-toolbar">
      <button type="button" onClick={onClose}>← 返回工作台</button>
      <div><strong>案件材料</strong><span>{caseTitle}</span></div>
      <button type="button" disabled={busy || exportBusy} onClick={() => void generate()}>{busy ? "正在生成…" : "重新生成"}</button>
      <button type="button" disabled={busy || exportBusy} onClick={() => void download()}>{exportBusy ? "正在打包…" : "下载案件材料包"}</button>
    </header>
    {busy ? <p className="materials-message" role="status">正在读取全部 Issue 的已保存状态…{html ? "当前仍显示上次材料包。" : ""}</p> : null}
    {error ? <div className="materials-message materials-error" role="alert">
      <p>生成失败：{error}{html ? " 当前仍显示上次材料包。" : ""}</p>
      <button type="button" onClick={() => void generate()}>重试生成</button>
    </div> : null}
    {exportBusy ? <p className="materials-message" role="status">正在打包最新已保存档案及全部原始上传文件…</p> : null}
    {exportError ? <p className="materials-message materials-error" role="alert">下载失败：{exportError}</p> : null}
    {html ? <iframe className="materials-frame" title="HTML 案件档案" srcDoc={html}
      sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox allow-downloads" referrerPolicy="no-referrer" /> : null}
  </dialog>;
}
