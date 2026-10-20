// frontend/js/events.js
// 拆分自原 index.js 300-312, 356-393, 395-418, 1041-1053, 1055-1155, 1157-1273 行
// 业务编排（generate/semanticZoomSelected）+ bindNode 节点事件 + 所有 addEventListener

import { state, dom } from './state.js';
import { setStatus } from './http.js';
import { decisionLabel } from './labels.js';
import { hoveredSelectedCardFromEvent, selectedCards, semanticBaseLevel, allCards, generationScope, clampLevel, cardFootprint } from './cards.js';
import { patchCard, queueLevelSave, localPatch, loadCase, loadTimeline, runRelation, upload, apiGenerate } from './api.js';
import { render, select, queueSave } from './render.js';
import { fitView, focusNode, transform } from './layout.js';
import { updateLines } from './edges.js';

// 业务编排：generate（POST /api/inference/generate + 处理 conflict_check + 标 decision + 聚焦）
export async function generate(prompt, forcedMode = null, forcedSelection = null) {
  if (!state.caseData?.case_id) {
    setStatus('请先上传或加载案件');
    return;
  }
  const mode = forcedMode || 'auto';
  const selected = forcedSelection || generationScope();
  setStatus(`正在生成：${mode}`);
  const result = await apiGenerate({
    case_id: state.caseData.case_id,
    selected_card_ids: selected,
    mode,
    user_prompt: prompt,
  });
  const cards = result.new_inference_cards || [];
  if (mode === 'conflict_check') {
    for (const card of cards) {
      await patchCard(card.id, { decision: 'pending', display_level: 3 }, false);
    }
  }
  await loadCase();
  const focusRef = cards.find((card) => card.inference_type === 'conflict') || cards[0] || null;
  const focus = focusRef ? allCards().find((card) => card.id === focusRef.id) || null : null;
  if (focus) {
    state.selected = [focus.id];
    state.focused = focus.id;
    render();
    focusNode(focus.id);
  }
  setStatus(result.message || `已生成 ${cards.length} 张推理卡`);
}

// Timeline：加载 runtime projection 并渲染（只读，不生成 inference card）
export async function showTimeline() {
  if (!state.caseData?.case_id) {
    setStatus('请先上传或加载案件');
    return;
  }
  setStatus('正在加载时间线...');
  try {
    state.timeline = await loadTimeline(state.caseData.case_id);
    render();
    setStatus(`时间线：${(state.timeline.items || []).length} 项`);
  } catch (error) {
    setStatus('时间线加载失败');
  }
}

// 业务编排：semanticZoomSelected（wheel 缩放选中卡）
export function semanticZoomSelected(delta, event) {
  const hovered = hoveredSelectedCardFromEvent(event);
  const targets = selectedCards();
  if (!hovered || !targets.length) return false;
  const targetLevel = clampLevel(semanticBaseLevel(hovered) + delta);
  if (targetLevel === semanticBaseLevel(hovered)) return true;
  for (const card of targets) {
    localPatch(card.id, { display_level: targetLevel });
    queueLevelSave(card.id, targetLevel);
  }
  render();
  return true;
}

// bindNode：节点事件绑定（拖动 + 单击/双击 + data-act 按钮）
export function bindNode(node, card) {
  let dragging = false;
  let sx = 0;
  let sy = 0;
  let baseX = 0;
  let baseY = 0;
  node.addEventListener('mousedown', (event) => {
    if (event.target.closest('[data-act]')) return;
    event.stopPropagation();
    dragging = true;
    sx = event.clientX;
    sy = event.clientY;
    baseX = parseFloat(node.style.left || '0');
    baseY = parseFloat(node.style.top || '0');
    node.style.zIndex = '1000';
  });

  node.addEventListener('click', (event) => {
    const action = event.target.closest('[data-act]');
    if (action) return;
    select(card.id, event.metaKey || event.ctrlKey);
  });

  node.addEventListener('dblclick', async () => {
    if (card.card_type === 'inference' && card.inference_type === 'conflict' && semanticBaseLevel(card) !== 3) {
      await patchCard(card.id, { display_level: 3 });
      setStatus('已展开冲突详情');
    }
  });

  node.addEventListener('click', async (event) => {
    const action = event.target.closest('[data-act]');
    if (!action) return;
    event.stopPropagation();
    const act = action.dataset.act;
    const id = action.dataset.id;

    if (act === 'decision') {
      const decision = action.dataset.decision;
      await patchCard(id, { decision, display_level: 2 });
      setStatus('冲突状态已更新为：' + decisionLabel(decision));
      return;
    }
    if (act === 'expand') {
      await patchCard(id, { display_level: 3 });
      setStatus('已展开冲突详情');
      return;
    }
    if (act === 'reasoning') {
      state.selected = [id];
      state.focused = id;
      render();
      await generate('请针对当前卡片的主题生成一个能帮助用户推进案件或得出结论的推论', 'hypothesis', [id]);
      return;
    }
    if (act === 'missing') {
      state.selected = [id];
      state.focused = id;
      render();
      await generate('基于该冲突找待验证点', 'missing_evidence', [id]);
    }
  });

  window.addEventListener('mousemove', (event) => {
    if (!dragging) return;
    const dx = (event.clientX - sx) / state.transform.scale;
    const dy = (event.clientY - sy) / state.transform.scale;
    const nx = baseX + dx;
    const ny = baseY + dy;
    node.style.left = `${nx}px`;
    node.style.top = `${ny}px`;
    localPatch(card.id, { position: { x: nx, y: ny } });
    updateLines();
  });

  window.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    node.style.zIndex = card.card_type === 'inference' && card.decision === 'rejected'
      ? '2'
      : state.focused === card.id
        ? '56'
        : '12';
    queueSave(card.id, {
      x: parseFloat(node.style.left || '0'),
      y: parseFloat(node.style.top || '0'),
    });
  });
}

// 全局事件绑定：画布拖动、wheel 缩放、按钮、文件、prompt
export function attachGlobalEvents() {
  dom.wrap.addEventListener('mousedown', (event) => {
    if (event.target.closest('.node')) return;
    if (state.selected.length || state.focused) {
      state.selected = [];
      state.focused = null;
      render();
    }
    state.draggingCanvas = true;
    state.panX = event.clientX - state.transform.x;
    state.panY = event.clientY - state.transform.y;
  });

  window.addEventListener('mousemove', (event) => {
    if (!state.draggingCanvas) return;
    state.transform.x = event.clientX - state.panX;
    state.transform.y = event.clientY - state.panY;
    transform();
  });

  window.addEventListener('mouseup', () => {
    state.draggingCanvas = false;
  });

  dom.wrap.addEventListener('wheel', (event) => {
    event.preventDefault();
    const semanticDelta = event.deltaY < 0 ? 1 : -1;
    if (semanticZoomSelected(semanticDelta, event)) return;
    const delta = -event.deltaY * 0.001;
    const scale = Math.max(0.18, Math.min(2.2, state.transform.scale * Math.exp(delta)));
    const rect = dom.wrap.getBoundingClientRect();
    const mx = event.clientX - rect.left;
    const my = event.clientY - rect.top;
    state.transform.x = mx - (mx - state.transform.x) * (scale / state.transform.scale);
    state.transform.y = my - (my - state.transform.y) * (scale / state.transform.scale);
    state.transform.scale = scale;
    transform();
  }, { passive: false });

  dom.reloadBtn.addEventListener('click', async () => {
    try {
      setStatus('正在刷新案件...');
      await loadCase();
      setStatus('案件已刷新');
    } catch (error) {
      fail('刷新案件', error);
    }
  });

  dom.relationBtn.addEventListener('click', async () => {
    try {
      await runRelation();
    } catch (error) {
      fail('关系生成', error);
    }
  });

  dom.timelineBtn.addEventListener('click', async () => {
    try {
      await showTimeline();
    } catch (error) {
      fail('时间线', error);
    }
  });

  dom.timelineCloseBtn.addEventListener('click', () => {
    if (dom.timelinePanel) dom.timelinePanel.classList.add('hidden');
  });

  dom.fitBtn.addEventListener('click', () => fitView());

  dom.fileInput.addEventListener('change', async (event) => {
    const files = [...(event.target.files || [])];
    try {
      await upload(files);
      setStatus('上传、抽取、关系生成完成');
      fitView();
    } catch (error) {
      fail('上传', error);
    } finally {
      event.target.value = '';
      dom.uploadZone?.classList.remove('active');
    }
  });

  dom.uploadZone?.addEventListener('click', () => dom.fileInput.click());
  dom.uploadZone?.addEventListener('dragenter', (event) => {
    event.preventDefault();
    dom.uploadZone.classList.add('active');
  });
  dom.uploadZone?.addEventListener('dragover', (event) => {
    event.preventDefault();
    dom.uploadZone.classList.add('active');
  });
  dom.uploadZone?.addEventListener('dragleave', (event) => {
    event.preventDefault();
    dom.uploadZone.classList.remove('active');
  });
  dom.uploadZone?.addEventListener('drop', async (event) => {
    event.preventDefault();
    dom.uploadZone.classList.remove('active');
    const files = [...(event.dataTransfer?.files || [])];
    if (!files.length) return;
    try {
      await upload(files);
      setStatus('上传、抽取、关系生成完成');
      fitView();
    } catch (error) {
      fail('上传', error);
    }
  });

  dom.sendBtn.addEventListener('click', async () => {
    const text = dom.promptEl.value.trim();
    if (!text) return;
    try {
      await generate(text);
      dom.promptEl.value = '';
    } catch (error) {
      fail('生成', error);
    }
  });

  dom.promptEl.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    dom.sendBtn.click();
  });

  window.addEventListener('resize', updateLines);
}
