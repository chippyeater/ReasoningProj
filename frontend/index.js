const API = 'http://localhost:8000';
const OFF = 5000;
const MIN_LEVEL = 1;
const MAX_LEVEL = 3;

const state = {
  caseData: null,
  selected: [],
  focused: null,
  transform: { x: 360, y: 80, scale: 1 },
  draggingCanvas: false,
  pending: new Map(),
  pendingLevels: new Map(),
};

const wrap = document.getElementById('wrap');
const content = document.getElementById('content');
const links = document.getElementById('links');
const nodes = document.getElementById('nodes');
const fileList = document.getElementById('fileList');
const selectionList = document.getElementById('selectionList');
const empty = document.getElementById('empty');
const caseTitle = document.getElementById('caseTitle');
const statusEl = document.getElementById('status');
const promptEl = document.getElementById('prompt');
const uploadZone = document.getElementById('uploadZone');
const fileInput = document.getElementById('fileInput');
const reloadBtn = document.getElementById('reloadBtn');
const relationBtn = document.getElementById('relationBtn');
const fitBtn = document.getElementById('fitBtn');
const sendBtn = document.getElementById('sendBtn');

const EDGE_ICON_SRC = {
  danger: new URL('./src/assets/Familiar.png', import.meta.url).href,
  warn: new URL('./src/assets/Warn.png', import.meta.url).href,
  success: new URL('./src/assets/Success.png', import.meta.url).href,
};

const esc = (value) => String(value ?? '')
  .replace(/&/g, '&amp;')
  .replace(/</g, '&lt;')
  .replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;')
  .replace(/'/g, '&#39;');

const setStatus = (message) => {
  statusEl.textContent = message;
};

const json = async (response) => {
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `HTTP ${response.status}`);
  }
  return response.json();
};

const allCards = () => state.caseData
  ? [...state.caseData.meta_cards, ...state.caseData.inference_cards, ...state.caseData.law_cards]
  : [];

const cardMap = () => new Map(allCards().map((card) => [card.id, card]));

const generationScope = () => state.selected.length
  ? state.selected.slice()
  : (state.caseData?.meta_cards || []).slice(0, 4).map((card) => card.id);

const iconOf = (kind) => ({
  person: '人',
  organization: '组',
  legal: '法',
  conflict: '冲',
  missing: '缺',
})[kind] || '推';

const decisionLabel = (decision) => {
  if (decision === 'accepted') return '已确认';
  if (decision === 'rejected') return '已驳回';
  if (decision === 'pending') return '暂存';
  if (decision === 'undecided') return '待确认';
  return '未处理';
};

const levelLabel = (level) => {
  if (level === 3) return '详情';
  if (level === 2) return '概要';
  return '胶囊';
};

const edgeLabel = (type) => ({
  relates_to: '相关',
  supports: '支持',
  opposes: '不支持',
  derives: '推导',
  mentions: '提及',
  conflicts_with: '冲突',
  missing_for: '待补',
  cites: '引用',
  belongs_to: '属于',
  grounded_in: '依据',
  applies_to: '适用',
  retrieved_for: '检索',
})[type] || type;

const edgeVisual = (edge) => {
  if (['supports', 'grounded_in', 'retrieved_for', 'applies_to'].includes(edge.edge_type)) {
    return { color: '#36FF36', icon: edge.edge_type === 'supports' ? EDGE_ICON_SRC.success : null, iconOnly: edge.edge_type === 'supports', tone: 'success' };
  }
  if (['opposes', 'conflicts_with'].includes(edge.edge_type)) {
    return { color: '#FF0000', icon: edge.edge_type === 'conflicts_with' ? EDGE_ICON_SRC.danger : null, iconOnly: edge.edge_type === 'conflicts_with', tone: 'danger' };
  }
  if (['missing_for', 'mentions', 'derives'].includes(edge.edge_type)) {
    return { color: '#FF8800', icon: null, iconOnly: false, tone: 'warn' };
  }
  return { color: '#FFFFFF', icon: null, iconOnly: false, tone: 'neutral' };
};

const lineColor = (edge) => edgeVisual(edge).color;

const preferredSideLabel = (side) => {
  if (side === 'landlord') return '偏向房东';
  if (side === 'tenant') return '偏向租客';
  return '中性';
};

const recommendationDecisionLabel = (decision) => {
  if (decision === 'accepted') return '建议确认';
  if (decision === 'rejected') return '建议驳回';
  return '建议暂存';
};

const recommendationText = (card) => {
  const resolution = card.detail?.recommended_resolution;
  if (!resolution || typeof resolution !== 'object') return '';
  const reason = String(resolution.reason || '').trim();
  if (!reason) return '';
  const confidence = typeof resolution.confidence === 'number' ? ` ? ${Math.round(resolution.confidence * 100)}%` : '';
  return `系统建议：${recommendationDecisionLabel(resolution.recommended_decision)}，${preferredSideLabel(resolution.preferred_side)}${confidence}。${reason}`;
};

const typeMeta = (card) => {
  if (card.card_type === 'meta') return `${card.info_type} / ${card.info_subtype}`;
  if (card.card_type === 'law') {
    const article = card.detail?.article_no ? ` / ${card.detail.article_no}` : '';
    return `法条 / ${card.source_type} / ${card.norm_level}${article}`;
  }
  if (card.inference_type === 'conflict') return `冲突 / ${decisionLabel(card.decision)}`;
  if (card.inference_type === 'missing_evidence') return '待验证点';
  return '推理卡';
};

const kindOf = (card) => {
  if (card.card_type === 'meta') return card.info_subtype || card.info_type;
  if (card.card_type === 'law') return 'legal';
  const viewType = cardViewType(card);
  if (viewType === 'timeline') return 'timeline';
  if (card.inference_type === 'conflict' || viewType === 'conflict') return 'conflict';
  if (card.inference_type === 'missing_evidence') return 'missing';
  return 'hypothesis';
};

const clampLevel = (value) => Math.max(MIN_LEVEL, Math.min(MAX_LEVEL, value));

function semanticBaseLevel(card) {
  return clampLevel(card.display_level || 2);
}

function cardViewType(card) {
  if (!card || card.card_type !== 'inference') return null;
  const payload = card.detail?.view_payload;
  if (payload && typeof payload === 'object' && typeof payload.view_type === 'string') return payload.view_type;
  return typeof card.detail?.view_type === 'string' ? card.detail.view_type : null;
}

function isTimelineViewCard(card) {
  return cardViewType(card) === 'timeline';
}

function timelinePayload(card) {
  if (!isTimelineViewCard(card)) return null;
  const payload = card.detail?.view_payload;
  return payload && typeof payload === 'object' ? payload : null;
}

function timelinePointCount(payload) {
  const timepoints = Array.isArray(payload?.timepoints) ? payload.timepoints.length : 0;
  const preferred = Number(payload?.visible_point_count || 0);
  return Math.max(timepoints, preferred, 1);
}

function timelineCardWidth(card) {
  const payload = timelinePayload(card);
  const pointCount = timelinePointCount(payload);
  return Math.max(760, Math.min(1320, 220 + pointCount * 132));
}

function cardFootprint(card, index = 0) {
  const level = card.card_type === 'law' ? Math.min(semanticBaseLevel(card), 2) : semanticBaseLevel(card);
  const viewType = cardViewType(card);
  if (card.card_type === 'inference' && viewType === 'timeline' && level >= 3) {
    return { width: timelineCardWidth(card), height: 420 };
  }
  if (card.card_type === 'inference' && card.inference_type === 'conflict' && level >= 3) {
    const hasPlan = Array.isArray(card.detail?.view_payload?.layout_plan?.sections) && card.detail.view_payload.layout_plan.sections.length > 0;
    return { width: hasPlan ? 860 : 420, height: hasPlan ? 560 : 320 };
  }
  if (card.card_type === 'law' && level >= 2) return { width: 432, height: 220 };
  if (card.card_type === 'meta' && level >= 2) return { width: 432, height: 220 };
  if (card.card_type === 'inference' && card.inference_type === 'missing_evidence' && level >= 2) return { width: 432, height: 220 };
  if (card.card_type === 'inference' && level >= 3) return { width: 360, height: 260 };
  if (level === 2) return { width: 320, height: 180 };
  return { width: 220, height: 96 };
}

function findOpenCanvasPosition(card) {
  const cards = allCards().filter((item) => item.id !== card.id && item.status !== 'archived' && item.status !== 'hidden');
  if (!cards.length) return { x: 120, y: 120 };
  const rects = cards.map((item, index) => {
    const pos = positionOf(item, index);
    const size = cardFootprint(item, index);
    return { x: pos.x, y: pos.y, width: size.width, height: size.height };
  });
  const maxRight = Math.max(...rects.map((rect) => rect.x + rect.width));
  const minTop = Math.min(...rects.map((rect) => rect.y));
  return { x: Math.round(maxRight + 180), y: Math.max(80, Math.round(minTop + 20)) };
}

function hoveredSelectedCardFromEvent(event) {
  const node = event.target instanceof Element ? event.target.closest('.node') : null;
  if (!node) return null;
  if (!state.selected.includes(node.id)) return null;
  return cardMap().get(node.id) || null;
}

function selectedCards() {
  const map = cardMap();
  return state.selected.map((id) => map.get(id)).filter(Boolean);
}

function fallbackPosition(card, index) {
  const metaIndex = state.caseData.meta_cards.findIndex((item) => item.id === card.id);
  const inferenceIndex = state.caseData.inference_cards.findIndex((item) => item.id === card.id);
  const lawIndex = state.caseData.law_cards.findIndex((item) => item.id === card.id);
  if (card.card_type === 'meta') return { x: 80 + (metaIndex % 2) * 360, y: 80 + Math.floor(metaIndex / 2) * 240 };
  if (card.card_type === 'law') return { x: 820 + (lawIndex % 2) * 340, y: 60 + Math.floor(lawIndex / 2) * 180 };
  return { x: 900 + (inferenceIndex % 2) * 420, y: 240 + Math.floor(inferenceIndex / 2) * 280 + index * 4 };
}

function positionOf(card, index) {
  const position = card.position || { x: 0, y: 0 };
  if (typeof position.x === 'number' && typeof position.y === 'number' && (position.x !== 0 || position.y !== 0)) {
    return position;
  }
  return fallbackPosition(card, index);
}

function localPatch(cardId, patch) {
  if (!state.caseData) return;
  for (const group of [state.caseData.meta_cards, state.caseData.inference_cards, state.caseData.law_cards]) {
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

async function patchCard(cardId, patch, rerender = true) {
  if (!state.caseData?.case_id) return;
  localPatch(cardId, patch);
  if (rerender) render();
  await json(await fetch(`${API}/api/cases/${state.caseData.case_id}/cards/${cardId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  }));
}

function queueLevelSave(cardId, displayLevel) {
  const old = state.pendingLevels.get(cardId);
  if (old) clearTimeout(old);
  const timer = setTimeout(async () => {
    state.pendingLevels.delete(cardId);
    try {
      await patchCard(cardId, { display_level: displayLevel }, false);
    } catch (error) {
      setStatus(`层级保存失败：${error.message}`);
    }
  }, 180);
  state.pendingLevels.set(cardId, timer);
}

function semanticZoomSelected(delta, event) {
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

async function loadCase() {
  const data = await json(await fetch(`${API}/api/case`));
  state.caseData = data.case;
  if (!state.caseData) {
    state.selected = [];
    state.focused = null;
  }
  render();
}

async function runRelation() {
  if (!state.caseData?.case_id) {
    setStatus('没有可生成关系的案件');
    return;
  }
  await json(await fetch(`${API}/api/relation/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ case_id: state.caseData.case_id }),
  }));
  await loadCase();
  setStatus('关系已更新');
}

async function upload(files) {
  if (!files.length) return;
  setStatus('正在上传并抽取材料...');
  const formData = new FormData();
  files.forEach((file) => formData.append('files', file));
  let response;
  if (state.caseData?.case_id) {
    response = await fetch(`${API}/api/cases/${state.caseData.case_id}/files`, { method: 'POST', body: formData });
  } else {
    formData.append('case_title', 'MVP案件');
    response = await fetch(`${API}/api/cases`, { method: 'POST', body: formData });
  }
  const data = await json(response);
  state.caseData = data.case;
  setStatus('材料已上传，正在生成关系...');
  await runRelation();
}

async function generate(prompt, forcedMode = null, forcedSelection = null) {
  if (!state.caseData?.case_id) {
    setStatus('请先上传或加载案件');
    return;
  }
  const mode = forcedMode || 'auto';
  const selected = forcedSelection || generationScope();
  setStatus(`正在生成：${mode}`);
  const result = await json(await fetch(`${API}/api/inference/generate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      case_id: state.caseData.case_id,
      selected_card_ids: selected,
      mode,
      user_prompt: prompt,
    }),
  }));
  const cards = result.new_inference_cards || [];
  if (mode === 'conflict_check') {
    for (const card of cards) {
      await patchCard(card.id, { decision: 'pending', display_level: 3 }, false);
    }
  }
  await loadCase();
  const focusRef = cards.find((card) => card.detail?.view_payload?.view_type === 'timeline') || cards.find((card) => card.inference_type === 'conflict') || cards[0] || null;
  const focus = focusRef ? allCards().find((card) => card.id === focusRef.id) || null : null;
  if (focus) {
    if (isTimelineViewCard(focus)) {
      await patchCard(focus.id, { position: findOpenCanvasPosition(focus), display_level: 3 }, false);
    }
    state.selected = [focus.id];
    state.focused = focus.id;
    render();
    focusNode(focus.id);
  }
  setStatus(result.message || `已生成 ${cards.length} 张推理卡`);
}

async function retrieveLaw(conflictCardId) {
  if (!state.caseData?.case_id) {
    setStatus('请先加载案件');
    return;
  }
  setStatus('正在检索法条...');
  const result = await json(await fetch(`${API}/api/law/retrieve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      case_id: state.caseData.case_id,
      conflict_card_id: conflictCardId,
      selected_card_ids: [conflictCardId],
      intent: 'support_conflict_resolution',
      max_results: 3,
    }),
  }));
  await loadCase();
  state.selected = [conflictCardId, ...(result.new_law_cards || []).map((card) => card.id)];
  state.focused = conflictCardId;
  render();
  focusNode(conflictCardId);
  setStatus(result.message || `已检索 ${result.new_law_cards?.length || 0} 条法条`);
}

function renderFilePanel() {
  fileList.innerHTML = '';
  const files = state.caseData?.files || [];
  if (!files.length) {
    fileList.innerHTML = '<div class="muted">暂无材料。点击上方“上传材料”开始。</div>';
    return;
  }
  for (const file of files) {
    const element = document.createElement('div');
    element.className = 'item';
    element.innerHTML = `<div class="name">${esc(file.filename)}</div><div class="meta">${esc(file.file_type)} / ${esc(file.parse_status)}</div>`;
    fileList.appendChild(element);
  }
}

function renderSelectionPanel() {
  selectionList.innerHTML = '';
  const map = cardMap();
  if (!state.selected.length) {
    selectionList.innerHTML = '<div class="muted">还没有选中卡片。普通点击单选，Ctrl/Cmd + 点击多选。</div>';
    return;
  }
  for (const cardId of state.selected) {
    const card = map.get(cardId);
    if (!card) continue;
    const element = document.createElement('div');
    element.className = 'item';
    element.innerHTML = `<div class="name">${esc(card.title)}</div><div class="meta">${esc(typeMeta(card))}</div>`;
    selectionList.appendChild(element);
  }
}

function listBlock(title, items) {
  if (!items.length) {
    return '';
  }
  return `<div class="block detail"><h4>${title}</h4><ul>${items.map((item) => `<li>${esc(item)}</li>`).join('')}</ul></div>`;
}

function titles(ids, map) {
  return (ids || []).map((id) => map.get(id)?.title || id).filter(Boolean);
}

function infoUnitMap() {
  return new Map((state.caseData?.info_units || []).map((unit) => [unit.id, unit]));
}

function emphasisLabel(value) {
  if (value === 'high') return '高相关';
  if (value === 'medium') return '相关';
  if (value === 'low') return '补充';
  return '相关';
}

function roleLabel(value) {
  if (value === 'claim') return '主张';
  if (value === 'evidence') return '证据';
  if (value === 'event') return '事件';
  if (value === 'state') return '状态';
  if (value === 'summary') return '摘要';
  return value || '内容';
}

function renderPlanItem(item, unitLookup) {
  const unit = unitLookup.get(item.card_id);
  if (!unit) return '';
  const summary = unit.summary || unit.detail || '';
  const meta = [
    roleLabel(item.card_role),
    emphasisLabel(item.emphasis),
    unit.side === 'landlord' ? '房东侧' : unit.side === 'tenant' ? '租客侧' : '',
  ].filter(Boolean).join(' · ');
  return `<div class="view-item${item.default_expanded ? ' expanded' : ''}">
    <div class="view-item-title">${esc(unit.title || item.card_id)}</div>
    ${meta ? `<div class="view-item-meta">${esc(meta)}</div>` : ''}
    ${summary ? `<div class="view-item-summary">${esc(summary)}</div>` : ''}
  </div>`;
}

function renderPlanSection(section, unitLookup) {
  const cards = Array.isArray(section.cards) ? section.cards : [];
  const items = cards
    .filter((item) => item && typeof item === 'object')
    .map((item) => renderPlanItem(item, unitLookup))
    .join('');
  if (!items && section.section_type !== 'question_header') return '';
  return `<section class="view-section placement-${esc(section.placement || 'center')} section-${esc(section.section_type || 'generic')}">
    ${section.title ? `<h4>${esc(section.title)}</h4>` : ''}
    ${section.purpose ? `<div class="view-section-purpose">${esc(section.purpose)}</div>` : ''}
    ${items ? `<div class="view-items">${items}</div>` : ''}
  </section>`;
}

function renderConflictViewPlan(card) {
  const payload = card.detail?.view_payload;
  const layoutPlan = payload?.layout_plan;
  if (!layoutPlan || !Array.isArray(layoutPlan.sections) || !layoutPlan.sections.length) return '';
  const unitLookup = infoUnitMap();
  const sections = layoutPlan.sections.filter((section) => section && typeof section === 'object');
  const top = sections.filter((section) => section.placement === 'top' || section.section_type === 'question_header');
  const left = sections.filter((section) => section.placement === 'left');
  const right = sections.filter((section) => section.placement === 'right');
  const center = sections.filter((section) => !['top', 'left', 'right', 'bottom'].includes(section.placement));
  const bottom = sections.filter((section) => section.placement === 'bottom');
  const topHtml = top.map((section) => renderPlanSection(section, unitLookup)).join('');
  const centerHtml = center.map((section) => renderPlanSection(section, unitLookup)).join('');
  const bottomHtml = bottom.map((section) => renderPlanSection(section, unitLookup)).join('');
  const leftHtml = left.map((section) => renderPlanSection(section, unitLookup)).join('');
  const rightHtml = right.map((section) => renderPlanSection(section, unitLookup)).join('');
  return `<div class="view-plan conflict-plan">
    ${topHtml}
    ${leftHtml || rightHtml ? `<div class="view-columns">${leftHtml ? `<div class="view-column">${leftHtml}</div>` : ''}${rightHtml ? `<div class="view-column">${rightHtml}</div>` : ''}</div>` : ''}
    ${centerHtml}
    ${bottomHtml}
  </div>`;
}


function conflictLevelOne(card) {
  return `<div class="chips compact-only">
    <span class="chip">冲突卡</span>
    <span class="chip ${esc(card.decision || 'pending')}">${esc(decisionLabel(card.decision))}</span>
  </div>`;
}

function conflictLevelTwo(card, support, oppose, missing, legal, level) {
  const overview = [
    `支持 ${support.length}`,
    `反向 ${oppose.length}`,
    `待补 ${missing.length}`,
    `法条 ${legal.length}`,
  ];
  return `<div class="summary compact-overview">${overview.map((item) => `<span>${esc(item)}</span>`).join('')}</div>
    <div class="actions">
      ${card.decision === 'accepted' ? `<button class="action" data-act="law" data-id="${card.id}">检索法条</button>
      <button class="action" data-act="reasoning" data-id="${card.id}">生成推论</button>
      <button class="action" data-act="missing" data-id="${card.id}">找待验证点</button>` : ''}
    </div>`;
}


function conflictLevelThree(card, support, oppose, missing, legal, steps, level) {
  const plan = renderConflictViewPlan(card);
  const fallbackBlocks = !plan
    ? `${listBlock('支持证据', support)}
    ${listBlock('反向证据', oppose)}
    ${listBlock('待补证据', missing)}
    ${listBlock('相关法条', legal)}
    ${listBlock('推理链', steps)}`
    : `${legal.length ? listBlock('相关法条', legal) : ''}${steps.length ? listBlock('推理链', steps) : ''}`;
  return `${plan || ''}
    ${fallbackBlocks}
    <div class="actions detail">
      <button class="action" data-act="decision" data-decision="pending" data-id="${card.id}">暂存</button>
      <button class="action" data-act="decision" data-decision="rejected" data-id="${card.id}">驳回</button>
      <button class="action" data-act="decision" data-decision="accepted" data-id="${card.id}">确认</button>
    </div>`;
}


function conflictBody(card, map, level) {
  const support = titles(card.detail?.supporting_card_ids, map);
  const oppose = titles(card.detail?.opposing_card_ids, map);
  const missing = titles(card.detail?.missing_card_ids, map);
  const legal = titles(card.detail?.legal_basis_ids, map);
  const steps = card.detail?.reasoning_steps || [];
  if (level === 1) return conflictLevelOne(card);
  if (level === 2) return conflictLevelTwo(card, support, oppose, missing, legal, level);
  return conflictLevelThree(card, support, oppose, missing, legal, steps, level);
}

function renderTimelineBody(card, level) {
  if (level === 1) return '';
  const payload = timelinePayload(card);
  if (!payload) return '';
  const timepoints = Array.isArray(payload.timepoints) ? payload.timepoints : [];
  const events = Array.isArray(payload.events) ? payload.events.filter((item) => item && typeof item === 'object') : [];
  const eventMap = new Map(events.map((item) => [item.id, item]));
  const pointCount = timelinePointCount(payload);
  const focusedTimepointId = payload.focused_timepoint_id || timepoints[0]?.id || null;
  const detailCard = payload.detail_card && typeof payload.detail_card === 'object' ? payload.detail_card : null;
  const expandedEvent = eventMap.get(payload.expanded_event_id) || eventMap.get(detailCard?.event_id) || events[0] || null;
  const detailTitle = detailCard?.title || expandedEvent?.title || '时间事件';
  const detailText = detailCard?.extraction_reason || expandedEvent?.detail || expandedEvent?.summary || '暂无详情';
  const detailExcerpt = detailCard?.source_excerpt || expandedEvent?.source_excerpt || '';
  const detailLabel = detailCard?.card_label || '元信息';
  const pointHtml = timepoints.map((point, index) => {
    const pointEventId = Array.isArray(point.event_ids) && point.event_ids.length ? point.event_ids[0] : null;
    const pointEvent = eventMap.get(pointEventId) || null;
    const focused = point.id === focusedTimepointId;
    const topLabel = point.event_title || pointEvent?.title || `事件 ${index + 1}`;
    const bottomLabel = point.time_label || `时间点 ${index + 1}`;
    const brief = point.event_brief || pointEvent?.summary || '';
    return `<div class="timeline-point${focused ? ' is-focused' : ''}${point.emphasis === 'normal' ? '' : ' is-emphasis'}">
      <div class="timeline-point-top">${esc(topLabel)}</div>
      <div class="timeline-point-dot" aria-hidden="true"></div>
      <div class="timeline-point-bottom">${esc(bottomLabel)}</div>
      ${brief ? `<div class="timeline-point-brief">${esc(brief)}</div>` : ''}
    </div>`;
  }).join('');
  const relatedInfo = (detailCard?.related_info_unit_ids || expandedEvent?.related_info_unit_ids || []).filter(Boolean);
  return `<div class="timeline-view" style="--timeline-count:${pointCount}">
    <div class="timeline-track">${pointHtml}</div>
    <div class="timeline-meta-card type-meta level-2">
      <div class="card">
        <div class="shell">
          <div class="head">
            <div class="icon">元</div>
            <div class="copy">
              <div class="head-row">
                <div class="card-title">元信息</div>
                <div class="head-badge">${esc(detailLabel)}</div>
              </div>
              <div class="card-meta">${esc(detailTitle)}</div>
            </div>
          </div>
          <div class="body">
            <div class="meta-body">
              <div class="meta-text">${esc(detailText)}</div>
            </div>
            ${detailExcerpt ? `<div class="timeline-detail-excerpt">${esc(detailExcerpt)}</div>` : ''}
            ${relatedInfo.length ? `<div class="timeline-detail-refs">${relatedInfo.map((item) => `<span>${esc(item)}</span>`).join('')}</div>` : ''}
          </div>
        </div>
      </div>
    </div>
  </div>`;
}

function normalBody(card, level) {
  if (level === 1) return '';
  const summary = card.summary || card.detail?.claim || card.detail?.text || card.detail?.description || '暂无摘要';
  if (card.card_type === 'inference' && card.inference_type !== 'conflict' && card.inference_type !== 'missing_evidence' && level >= 3) {
    const steps = Array.isArray(card.detail?.reasoning_steps) ? card.detail.reasoning_steps.filter(Boolean).slice(0, 3) : [];
    const stepHtml = steps.length ? `<div class="inference-steps">${steps.map(step => `<span>${esc(step)}</span>`).join('')}</div>` : '';
    return `<div class="inference-body"><div class="inference-detail">${esc(summary)}</div>${stepHtml}</div>`;
  }
  return `<div class="summary">${esc(summary)}</div>`;
}

function metaBody(card, level) {
  if (level === 1) return '';
  const metaText = card.detail?.description || '';
  return `<div class="meta-body"><div class="meta-text">${esc(metaText)}</div></div>`;
}

function lawBody(card, level) {
  if (level === 1) return '';
  const lawText = card.detail?.text || card.summary || card.detail?.article_no || '';
  return `<div class="law-body"><div class="law-text">${esc(lawText)}</div></div>`;
}

function missingBody(card, level) {
  if (level === 1) return '';
  const missingText = card.summary || card.detail?.claim || card.title || '';
  return `<div class="missing-body"><div class="missing-text">${esc(missingText)}</div></div>`;
}

function renderCardHead(card, kind, level) {
  if (card.card_type === 'law' && level >= 2) {
    return {
      title: '法条',
      meta: card.detail?.title_full || card.title || '',
      badge: '',
    };
  }
  if (card.card_type === 'meta' && level >= 2) {
    return {
      title: card.title || '元信息',
      meta: '',
      badge: '已提取',
    };
  }
  if (card.card_type === 'inference' && card.inference_type === 'missing_evidence' && level >= 2) {
    return {
      title: '待补充',
      meta: '',
      badge: '待补充',
    };
  }
  if (card.card_type === 'inference' && cardViewType(card) === 'timeline' && level >= 3) {
    return {
      title: '时间事件',
      meta: '',
      badge: '时间线',
    };
  }
  if (card.card_type === 'inference' && card.inference_type !== 'conflict' && card.inference_type !== 'missing_evidence' && level >= 3) {
    return {
      title: '推论',
      meta: '',
      badge: inferenceBadgeLabel(card.inference_type),
    };
  }
  return {
    title: card.title,
    meta: typeMeta(card),
    badge: '',
  };
}

function inferenceBadgeLabel(inferenceType) {
  return ({
    hypothesis: '假设',
    conclusion: '结论',
    timeline: '时序',
    risk: '风险',
    other: '推论',
  })[inferenceType] || '推论';
}

function renderIconLabel(card, kind, level) {
  if (card.card_type === 'law' && level >= 2) return '法';
  if (card.card_type === 'meta' && level >= 2) return '元';
  if (card.card_type === 'inference' && cardViewType(card) === 'timeline' && level >= 2) return '时';
  if (card.card_type === 'inference' && card.inference_type === 'missing_evidence' && level >= 2) return '补';
  return iconOf(kind);
}

function renderNode(card, index, map) {
  const kind = kindOf(card);
  const level = semanticBaseLevel(card);
  const renderLevel = card.card_type === 'law' ? Math.min(level, 2) : level;
  const position = positionOf(card, index);
  const selected = state.selected.includes(card.id);
  const focused = state.focused === card.id;
  const rejected = card.card_type === 'inference' && card.decision === 'rejected';
  const viewType = cardViewType(card);
  const hasConflictLayout = card.card_type === 'inference' && card.inference_type === 'conflict' && renderLevel === 3 && Array.isArray(card.detail?.view_payload?.layout_plan?.sections) && card.detail.view_payload.layout_plan.sections.length > 0;
  const hasWideLayout = hasConflictLayout || (viewType === 'timeline' && renderLevel === 3);
  const recommendation = card.card_type === 'inference' ? recommendationText(card) : '';
  const node = document.createElement('div');
  const accepted = card.card_type === 'inference' && card.decision === 'accepted';
  const headContent = renderCardHead(card, kind, renderLevel);
  const iconLabel = renderIconLabel(card, kind, renderLevel);
  const bodyContent = card.card_type === 'inference' && (card.inference_type === 'conflict' || viewType === 'conflict')
    ? conflictBody(card, map, renderLevel)
    : card.card_type === 'inference' && viewType === 'timeline'
      ? renderTimelineBody(card, renderLevel)
      : card.card_type === 'law'
        ? lawBody(card, renderLevel)
        : card.card_type === 'meta'
          ? metaBody(card, renderLevel)
          : card.card_type === 'inference' && card.inference_type === 'missing_evidence'
            ? missingBody(card, renderLevel)
            : normalBody(card, renderLevel);
  node.className = `node type-${card.card_type} kind-${kind} level-${renderLevel}${selected ? ' selected' : ''}${focused ? ' focused' : ''}${rejected ? ' rejected' : ''}${accepted ? ' accepted' : ''}${hasWideLayout ? ' wide-layout' : ''}`;
  node.id = card.id;
  node.style.left = `${position.x}px`;
  node.style.top = `${position.y}px`;
  if (viewType === 'timeline' && renderLevel === 3) {
    node.style.width = `${timelineCardWidth(card)}px`;
  } else {
    node.style.width = '';
  }
  node.style.zIndex = rejected ? '2' : focused ? '56' : selected ? '48' : renderLevel === 3 ? '36' : '12';
  node.innerHTML = `<div class="card"><div class="shell"><div class="head"><div class="icon">${esc(iconLabel)}</div><div class="copy"><div class="head-row"><div class="card-title">${esc(headContent.title)}</div>${headContent.badge ? `<div class="head-badge">${esc(headContent.badge)}</div>` : ''}</div>${headContent.meta ? `<div class="card-meta">${esc(headContent.meta)}</div>` : ''}</div></div><div class="body">${bodyContent}</div></div></div>${recommendation ? `<div class="card-edge-note">${esc(recommendation)}</div>` : ''}`;
  bindNode(node, card);
  nodes.appendChild(node);
}


function buildEdgeArrow(endPoint, previousPoint, color) {
  const angle = Math.atan2(endPoint.y - previousPoint.y, endPoint.x - previousPoint.x) * 180 / Math.PI;
  const arrow = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
  arrow.setAttribute('points', '-10,-5 0,0 -10,5');
  arrow.setAttribute('fill', color);
  arrow.setAttribute('transform', `translate(${endPoint.x},${endPoint.y}) rotate(${angle})`);
  return arrow;
}

function buildEdgePill(edge, path) {
  const label = edgeLabel(edge.edge_type);
  if (!label) return null;
  const visual = edgeVisual(edge);
  const totalLength = path.getTotalLength();
  const midpoint = path.getPointAtLength(totalLength / 2);
  const group = document.createElementNS('http://www.w3.org/2000/svg', 'g');
  group.setAttribute('transform', `translate(${midpoint.x},${midpoint.y})`);

  if (visual.iconOnly && visual.icon) {
    const image = document.createElementNS('http://www.w3.org/2000/svg', 'image');
    image.setAttribute('href', visual.icon);
    image.setAttribute('x', '-9');
    image.setAttribute('y', '-9');
    image.setAttribute('width', '18');
    image.setAttribute('height', '18');
    group.appendChild(image);
    return group;
  }

  const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
  rect.setAttribute('rx', '12');
  rect.setAttribute('ry', '12');
  rect.setAttribute('fill', '#2B1146');
  rect.setAttribute('stroke', 'rgba(255,255,255,.92)');
  rect.setAttribute('stroke-width', '1');

  const textNode = document.createElementNS('http://www.w3.org/2000/svg', 'text');
  textNode.textContent = label;
  textNode.setAttribute('fill', '#FFFFFF');
  textNode.setAttribute('font-size', '12');
  textNode.setAttribute('font-weight', '600');
  textNode.setAttribute('font-family', '"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif');
  textNode.setAttribute('x', '-9999');
  textNode.setAttribute('y', '-9999');
  group.appendChild(textNode);
  links.appendChild(group);

  const textBox = textNode.getBBox();
  const width = textBox.width + 28;
  const height = 28;
  rect.setAttribute('x', String(-width / 2));
  rect.setAttribute('y', String(-height / 2));
  rect.setAttribute('width', String(width));
  rect.setAttribute('height', String(height));
  group.insertBefore(rect, group.firstChild);
  textNode.setAttribute('x', String(-width / 2 + 14));
  textNode.setAttribute('y', '4');
  return group;
}

function updateLines() {
  links.innerHTML = '';
  if (!state.caseData) return;
  for (const edge of state.caseData.edges || []) {
    const source = document.getElementById(edge.source);
    const target = document.getElementById(edge.target);
    if (!source || !target) continue;
    const sourceRect = source.getBoundingClientRect();
    const targetRect = target.getBoundingClientRect();
    const contentRect = content.getBoundingClientRect();
    const sx = (sourceRect.left - contentRect.left) / state.transform.scale + source.offsetWidth / 2;
    const sy = (sourceRect.top - contentRect.top) / state.transform.scale + source.offsetHeight / 2;
    const tx = (targetRect.left - contentRect.left) / state.transform.scale + target.offsetWidth / 2;
    const ty = (targetRect.top - contentRect.top) / state.transform.scale + target.offsetHeight / 2;
    const dx = tx - sx;
    const dy = ty - sy;
    const dr = Math.max(120, Math.sqrt(dx * dx + dy * dy) * 1.25);
    const visual = edgeVisual(edge);
    const group = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('class', 'link');
    path.setAttribute('d', `M ${sx + OFF},${sy + OFF} A ${dr},${dr} 0 0,1 ${tx + OFF},${ty + OFF}`);
    path.style.stroke = visual.color;
    path.style.strokeWidth = String(Math.max(1.8, edge.weight || 2.2));
    group.appendChild(path);
    links.appendChild(group);

    const totalLength = path.getTotalLength();
    const endPoint = path.getPointAtLength(totalLength);
    const previousPoint = path.getPointAtLength(Math.max(0, totalLength - 12));
    group.appendChild(buildEdgeArrow(endPoint, previousPoint, visual.color));

    const pill = buildEdgePill(edge, path);
    if (pill) {
      group.appendChild(pill);
    }
  }
}

function transform() {
  content.style.transform = `translate(${state.transform.x}px, ${state.transform.y}px) scale(${state.transform.scale})`;
  updateLines();
}

function fitView() {
  const cards = allCards().filter((card) => card.status !== 'archived' && card.status !== 'hidden');
  if (!cards.length) return;
  const positions = cards.map((card, index) => positionOf(card, index));
  const minX = Math.min(...positions.map((point) => point.x));
  const minY = Math.min(...positions.map((point) => point.y));
  const maxX = Math.max(...positions.map((point) => point.x + 360));
  const maxY = Math.max(...positions.map((point) => point.y + 260));
  const boxWidth = maxX - minX;
  const boxHeight = maxY - minY;
  const sx = (wrap.clientWidth - 120) / Math.max(boxWidth, 1);
  const sy = (wrap.clientHeight - 120) / Math.max(boxHeight, 1);
  state.transform.scale = Math.max(0.45, Math.min(1, Math.min(sx, sy)));
  state.transform.x = 60 - minX * state.transform.scale;
  state.transform.y = 60 - minY * state.transform.scale;
  transform();
}

function focusNode(id) {
  const node = document.getElementById(id);
  if (!node) return;
  const x = parseFloat(node.style.left || '0');
  const y = parseFloat(node.style.top || '0');
  state.transform.x = wrap.clientWidth * 0.5 - (x + node.offsetWidth / 2) * state.transform.scale;
  state.transform.y = wrap.clientHeight * 0.38 - (y + node.offsetHeight / 2) * state.transform.scale;
  transform();
}

function render() {
  caseTitle.textContent = state.caseData
    ? `当前案件：${state.caseData.case_title || state.caseData.case_id}`
    : '当前案件：未加载';
  renderFilePanel();
  renderSelectionPanel();
  nodes.innerHTML = '';
  links.innerHTML = '';
  if (!state.caseData) {
    empty.style.display = 'block';
    return;
  }
  empty.style.display = 'none';
  const cards = allCards().filter((card) => card.status !== 'archived' && card.status !== 'hidden');
  const map = cardMap();
  cards.forEach((card, index) => renderNode(card, index, map));
  updateLines();
}

function select(cardId, additive) {
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

function queueSave(cardId, position) {
  const old = state.pending.get(cardId);
  if (old) clearTimeout(old);
  const timer = setTimeout(async () => {
    state.pending.delete(cardId);
    try {
      await patchCard(cardId, { position }, false);
    } catch (error) {
      setStatus(`位置保存失败：${error.message}`);
    }
  }, 220);
  state.pending.set(cardId, timer);
}

function bindNode(node, card) {
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
      setStatus("冲突状态已更新为：" + decisionLabel(decision));
      return;
    }

    if (act === 'expand') {
      await patchCard(id, { display_level: 3 });
      setStatus('已展开冲突详情');
      return;
    }

    if (act === 'law') {
      state.selected = [id];
      state.focused = id;
      render();
      await retrieveLaw(id);
      return;
    }

    if (act === 'reasoning') {
      state.selected = [id];
      state.focused = id;
      render();
      await generate('åºäºå·²ç¡®è®¤å²çªçæ推论', 'hypothesis', [id]);
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

wrap.addEventListener('mousedown', (event) => {
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

wrap.addEventListener('wheel', (event) => {
  event.preventDefault();
  const semanticDelta = event.deltaY < 0 ? 1 : -1;
  if (semanticZoomSelected(semanticDelta, event)) return;
  const delta = -event.deltaY * 0.001;
  const scale = Math.max(0.18, Math.min(2.2, state.transform.scale * Math.exp(delta)));
  const rect = wrap.getBoundingClientRect();
  const mx = event.clientX - rect.left;
  const my = event.clientY - rect.top;
  state.transform.x = mx - (mx - state.transform.x) * (scale / state.transform.scale);
  state.transform.y = my - (my - state.transform.y) * (scale / state.transform.scale);
  state.transform.scale = scale;
  transform();
}, { passive: false });

reloadBtn.addEventListener('click', async () => {
  try {
    setStatus('正在刷新案件...');
    await loadCase();
    setStatus('案件已刷新');
  } catch (error) {
    setStatus(`刷新失败：${error.message}`);
  }
});

relationBtn.addEventListener('click', async () => {
  try {
    await runRelation();
  } catch (error) {
    setStatus(`关系生成失败：${error.message}`);
  }
});

fitBtn.addEventListener('click', fitView);

fileInput.addEventListener('change', async (event) => {
  const files = [...(event.target.files || [])];
  try {
    await upload(files);
    setStatus('上传、抽取、关系生成完成');
    fitView();
  } catch (error) {
    setStatus(`上传失败：${error.message}`);
  } finally {
    event.target.value = '';
    uploadZone?.classList.remove('active');
  }
});

uploadZone?.addEventListener('click', () => fileInput.click());
uploadZone?.addEventListener('dragenter', (event) => {
  event.preventDefault();
  uploadZone.classList.add('active');
});
uploadZone?.addEventListener('dragover', (event) => {
  event.preventDefault();
  uploadZone.classList.add('active');
});
uploadZone?.addEventListener('dragleave', (event) => {
  event.preventDefault();
  uploadZone.classList.remove('active');
});
uploadZone?.addEventListener('drop', async (event) => {
  event.preventDefault();
  uploadZone.classList.remove('active');
  const files = [...(event.dataTransfer?.files || [])];
  if (!files.length) return;
  try {
    await upload(files);
    setStatus('上传、抽取、关系生成完成');
    fitView();
  } catch (error) {
    setStatus(`上传失败：${error.message}`);
  }
});

sendBtn.addEventListener('click', async () => {
  const text = promptEl.value.trim();
  if (!text) return;
  try {
    await generate(text);
    promptEl.value = '';
  } catch (error) {
    setStatus(`生成失败：${error.message}`);
  }
});

promptEl.addEventListener('keydown', (event) => {
  if (event.key !== 'Enter') return;
  event.preventDefault();
  sendBtn.click();
});

window.addEventListener('resize', updateLines);

(async function init() {
  try {
    setStatus('正在加载当前案件...');
    await loadCase();
    transform();
    if (state.caseData) {
      fitView();
      setStatus('案件已加载');
    } else {
      setStatus('当前没有案件，请先上传材料');
    }
  } catch (error) {
    setStatus(`初始化失败：${error.message}`);
  }
}());




