// frontend/js/render.js
// 拆分自原 index.js 420-450, 706-757, 759-809, 1008-1053 行
// 节点装配：文件面板 / 选择面板 / 卡片头部 / 图标 / renderNode / render 主函数 / select / queueSave
// bindNode 在 events.js，renderNode 末尾通过 window.__bindNode 调（避免循环依赖）

import { state, dom } from './state.js';
import { esc, setStatus, fail } from './http.js';
import { typeMeta, iconOf, recommendationText } from './labels.js';
import { cardMap, kindOf, semanticBaseLevel, positionOf, allCards, cardFootprint } from './cards.js';
import { conflictBody, renderTimelineBody, hypothesisBody, hasHypothesisBoard, normalBody, metaBody, missingBody, inferenceBadgeLabel } from './views.js';
import { patchCard, loadTimeline } from './api.js';

// 侧栏面板

export function renderFilePanel() {
  dom.fileList.innerHTML = '';
  const files = state.caseData?.files || [];
  if (!files.length) {
    dom.fileList.innerHTML = '<div class="muted">暂无材料。点击上方"上传材料"开始。</div>';
    return;
  }
  for (const file of files) {
    const element = document.createElement('div');
    element.className = 'item';
    element.innerHTML = `<div class="name">${esc(file.filename)}</div><div class="meta">${esc(file.file_type)} / ${esc(file.parse_status)}</div>`;
    dom.fileList.appendChild(element);
  }
}

export function renderSelectionPanel() {
  dom.selectionList.innerHTML = '';
  const map = cardMap();
  if (!state.selected.length) {
    dom.selectionList.innerHTML = '<div class="muted">还没有选中卡片。普通点击单选，Ctrl/Cmd + 点击多选。</div>';
    return;
  }
  for (const cardId of state.selected) {
    const card = map.get(cardId);
    if (!card) continue;
    const element = document.createElement('div');
    element.className = 'item';
    element.innerHTML = `<div class="name">${esc(card.title)}</div><div class="meta">${esc(typeMeta(card))}</div>`;
    dom.selectionList.appendChild(element);
  }
}

// 卡片头部与图标

export function renderCardHead(card, kind, level) {
  if (card.card_type === 'meta' && level >= 2) {
    return { title: card.title || '元信息', meta: '', badge: card.entity_kind || 'meta' };
  }
  if (card.card_type === 'inference' && card.inference_type === 'missing_evidence' && level >= 2) {
    return { title: '待补充', meta: '', badge: '待补充' };
  }
  if (card.card_type === 'inference' && card.inference_type !== 'conflict' && card.inference_type !== 'missing_evidence' && level >= 3) {
    return { title: '推论', meta: '', badge: inferenceBadgeLabel(card.inference_type) };
  }
  return { title: card.title, meta: typeMeta(card), badge: '' };
}

export function renderIconLabel(card, kind, level) {
  if (card.card_type === 'meta' && level >= 2) return '元';
  if (card.card_type === 'inference' && card.inference_type === 'missing_evidence' && level >= 2) return '补';
  return ({ person: '人', organization: '组', conflict: '冲', missing: '缺' })[kind] || '推';
}

// 节点装配

export function renderNode(card, index, map) {
  const kind = kindOf(card);
  const level = semanticBaseLevel(card);
  const renderLevel = level;
  const position = positionOf(card, index);
  const selected = state.selected.includes(card.id);
  const focused = state.focused === card.id;
  const rejected = card.card_type === 'inference' && card.decision === 'rejected';
  const viewType = card.detail?.view_payload?.view_type;
  const hasConflictLayout = card.card_type === 'inference' && card.inference_type === 'conflict' && renderLevel === 3 && Array.isArray(card.detail?.view_payload?.layout_plan?.sections) && card.detail.view_payload.layout_plan.sections.length > 0;
  const hasHypLayout = viewType === 'hypothesis' && renderLevel === 3 && hasHypothesisBoard(card);
  const hasWideLayout = hasConflictLayout || hasHypLayout;
  const recommendation = card.card_type === 'inference' ? recommendationText(card) : '';
  const node = document.createElement('div');
  const accepted = card.card_type === 'inference' && card.decision === 'accepted';
  const headContent = renderCardHead(card, kind, renderLevel);
  const iconLabel = renderIconLabel(card, kind, renderLevel);
  const bodyContent = card.card_type === 'inference' && (card.inference_type === 'conflict' || viewType === 'conflict')
    ? conflictBody(card, map, renderLevel)
    : card.card_type === 'meta'
      ? metaBody(card, renderLevel)
      : card.card_type === 'inference' && card.inference_type === 'missing_evidence'
        ? missingBody(card, renderLevel)
        : card.card_type === 'inference' && card.inference_type === 'hypothesis'
          ? hypothesisBody(card, map, renderLevel)
          : normalBody(card, renderLevel);
  node.className = `node type-${card.card_type} kind-${kind} level-${renderLevel}${selected ? ' selected' : ''}${focused ? ' focused' : ''}${rejected ? ' rejected' : ''}${accepted ? ' accepted' : ''}${hasWideLayout ? ' wide-layout' : ''}`;
  node.id = card.id;
  node.style.left = `${position.x}px`;
  node.style.top = `${position.y}px`;
  node.style.width = '';
  node.style.zIndex = rejected ? '2' : focused ? '56' : selected ? '48' : renderLevel === 3 ? '36' : '12';
  node.innerHTML = `<div class="card"><div class="shell"><div class="head"><div class="icon">${esc(iconLabel)}</div><div class="copy"><div class="head-row"><div class="card-title">${esc(headContent.title)}</div>${headContent.badge ? `<div class="head-badge">${esc(headContent.badge)}</div>` : ''}</div>${headContent.meta ? `<div class="card-meta">${esc(headContent.meta)}</div>` : ''}</div></div><div class="body">${bodyContent}</div></div></div>${recommendation ? `<div class="card-edge-note">${esc(recommendation)}</div>` : ''}`;
  // 节点事件绑定放在 events.js，通过 window.__bindNode 注入避免循环依赖
  if (typeof window.__bindNode === 'function') window.__bindNode(node, card);
  dom.nodes.appendChild(node);
}

// Timeline 面板（runtime projection，独立于 inference card）

export function renderTimelinePanel() {
  if (!dom.timelinePanel || !dom.timelineBody) return;
  if (!state.caseData) {
    dom.timelinePanel.classList.add('hidden');
    dom.timelineBody.innerHTML = '';
    return;
  }
  dom.timelinePanel.classList.remove('hidden');
  dom.timelineBody.innerHTML = renderTimelineBody(state.timeline);
}

// 主 render 入口

export function render() {
  dom.caseTitle.textContent = state.caseData
    ? `当前案件：${state.caseData.case_title || state.caseData.case_id}`
    : '当前案件：未加载';
  renderFilePanel();
  renderSelectionPanel();
  renderTimelinePanel();
  dom.nodes.innerHTML = '';
  dom.links.innerHTML = '';
  if (!state.caseData) {
    dom.empty.style.display = 'block';
    return;
  }
  dom.empty.style.display = 'none';
  const cards = allCards().filter((card) => card.status !== 'archived' && card.status !== 'hidden');
  const map = cardMap();
  cards.forEach((card, index) => renderNode(card, index, map));
  if (typeof window.__updateLines === 'function') window.__updateLines();
}

// 选中逻辑

export function select(cardId, additive) {
  if (!additive) {
    state.selected = [cardId];
  } else if (state.selected.includes(cardId)) {
    state.selected = state.selected.filter((value) => value !== cardId);
  } else {
    state.selected = [...state.selected, cardId];
  }
  state.focused = state.selected.length ? cardId : null;
  render();
}

// 位置 debounce 保存

export function queueSave(cardId, position) {
  const old = state.pending.get(cardId);
  if (old) clearTimeout(old);
  const timer = setTimeout(async () => {
    state.pending.delete(cardId);
    try {
      await patchCard(cardId, { position }, false);
    } catch (error) {
      fail('位置保存', error);
    }
  }, 220);
  state.pending.set(cardId, timer);
}
