// frontend/js/api.js
// 拆分自原 index.js 257-298, 314-393, 395-418 行
// 纯 fetch + 客户端乐观更新。
// 业务编排（generate / semanticZoomSelected）放在 events.js 避免循环依赖。

import { state } from './state.js';
import { json, setStatus, fail } from './http.js';

// render/focusNode 是 render.js / layout.js 挂到 window 的全局函数。
// api.js 不 import 它们，避免循环依赖。
// render.js 文件末尾会 window.__render = render; layout.js 同理。
function tryRender() {
  if (typeof window.__render === 'function') window.__render();
}
function tryFocusNode(id) {
  if (typeof window.__focusNode === 'function') window.__focusNode(id);
}

// 客户端乐观更新：本地先改 state，再发请求。

export function localPatch(cardId, patch) {
  if (!state.caseData) return;
  for (const group of [state.caseData.meta_cards, state.caseData.inference_cards]) {
    const card = group.find((item) => item.id === cardId);
    if (!card) continue;
    if (patch.title !== undefined) card.title = patch.title;
    if (patch.summary !== undefined) card.summary = patch.summary;
    if (patch.display_level !== undefined) card.display_level = patch.display_level;
    if (patch.position !== undefined) card.position = patch.position;
    if (patch.ui_state !== undefined) card.ui_state = patch.ui_state;
    if (patch.decision !== undefined && card.card_type === 'inference') card.decision = patch.decision;
    if (patch.detail !== undefined && card.detail) card.detail = { ...card.detail, ...patch.detail };
    card.updated_at = new Date().toISOString();
    state.caseData.updated_at = card.updated_at;
    return;
  }
}

// PATCH 单卡：本地先改，发请求；rerender 由调用方控制（默认 true）
export async function patchCard(cardId, patch, rerender = true) {
  if (!state.caseData?.case_id) return;
  localPatch(cardId, patch);
  if (rerender) tryRender();
  await json(await fetch(`/api/cases/${state.caseData.case_id}/cards/${cardId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  }));
}

// 层级保存的 debounce（避免拖动时频繁 PATCH）
export function queueLevelSave(cardId, displayLevel) {
  const old = state.pendingLevels.get(cardId);
  if (old) clearTimeout(old);
  const timer = setTimeout(async () => {
    state.pendingLevels.delete(cardId);
    try {
      await patchCard(cardId, { display_level: displayLevel }, false);
    } catch (error) {
      fail('层级保存', error);
    }
  }, 180);
  state.pendingLevels.set(cardId, timer);
}

// 加载当前案件
export async function loadCase() {
  const data = await json(await fetch('/api/case'));
  state.caseData = data.case;
  if (!state.caseData) {
    state.selected = [];
    state.focused = null;
  }
  tryRender();
}

// 生成关系
export async function runRelation() {
  if (!state.caseData?.case_id) {
    setStatus('没有可生成关系的案件');
    return;
  }
  await json(await fetch('/api/relation/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ case_id: state.caseData.case_id }),
  }));
  await loadCase();
  setStatus('关系已更新');
}

// 上传文件
export async function upload(files) {
  if (!files.length) return;
  setStatus('正在上传并抽取材料...');
  const formData = new FormData();
  files.forEach((file) => formData.append('files', file));
  let response;
  if (state.caseData?.case_id) {
    response = await fetch(`/api/cases/${state.caseData.case_id}/files`, { method: 'POST', body: formData });
  } else {
    formData.append('case_title', 'MVP案件');
    response = await fetch('/api/cases', { method: 'POST', body: formData });
  }
  const data = await json(response);
  state.caseData = data.case;
  setStatus('材料已上传，正在生成关系...');
  await runRelation();
}

// 纯 fetch：返回原始 response，让 events.js 的 generate 业务编排决定后续 UI 行为
export async function apiGenerate(payload) {
  return json(await fetch('/api/inference/generate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }));
}

// Timeline：runtime projection（只读）。返回 timeline.items（已排序）。
export async function loadTimeline(caseId) {
  if (!caseId) return { items: [] };
  const data = await json(await fetch(`/api/cases/${caseId}/timeline`));
  return data.timeline || { items: [] };
}
