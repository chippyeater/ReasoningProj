// frontend/js/views.js
// 拆分自原 index.js 452-461, 463-465, 467-481, 483-535, 538-589, 591-704, 749-757 行
// 卡片 body 渲染：冲突视图、时间线、假设、normal、meta、missing
// 不含 renderNode / render 主函数（那些在 render.js）

import { esc } from './http.js';
import { decisionLabel } from './labels.js';
import { state } from './state.js';
import { timelineItems, coreEntityByRef, personByRef } from './cards.js';

// 通用 helper

export const listBlock = (title, items) => {
  if (!items.length) return '';
  return `<div class="block detail"><h4>${title}</h4><ul>${items.map((item) => `<li>${esc(item)}</li>`).join('')}</ul></div>`;
};

export const titles = (ids, map) => (ids || []).map((id) => map.get(id)?.title || id).filter(Boolean);

// 冲突视图 plan 的 item 解析：映射 Core Entity（6 类），不再依赖 legacy info_units。
export const infoUnitMap = () => {
  const map = new Map();
  if (!state.caseData) return map;
  const groups = [
    state.caseData.persons,
    state.caseData.events,
    state.caseData.locations,
    state.caseData.objects,
    state.caseData.claims,
    state.caseData.evidences,
    state.caseData.meta_cards,
    state.caseData.inference_cards,
  ];
  for (const group of groups) {
    for (const entity of group || []) {
      if (!entity || !entity.id) continue;
      map.set(entity.id, {
        title: entity.title || entity.name || entity.proposition || entity.description || entity.id,
        summary: entity.summary || entity.description || entity.proposition || '',
        detail: entity.detail || '',
      });
    }
  }
  return map;
};

export const emphasisLabel = (value) => {
  if (value === 'high') return '高相关';
  if (value === 'medium') return '相关';
  if (value === 'low') return '补充';
  return '相关';
};

export const roleLabel = (value) => {
  if (value === 'claim') return '主张';
  if (value === 'evidence') return '证据';
  if (value === 'event') return '事件';
  if (value === 'state') return '状态';
  if (value === 'summary') return '摘要';
  return value || '内容';
};

// 冲突视图 plan 渲染

export function renderPlanItem(item, unitLookup) {
  const unit = unitLookup.get(item.card_id);
  if (!unit) return '';
  const summary = unit.summary || unit.detail || '';
  const meta = [
    roleLabel(item.card_role),
    emphasisLabel(item.emphasis),
  ].filter(Boolean).join(' · ');
  return `<div class="view-item${item.default_expanded ? ' expanded' : ''}">
    <div class="view-item-title">${esc(unit.title || item.card_id)}</div>
    ${meta ? `<div class="view-item-meta">${esc(meta)}</div>` : ''}
    ${summary ? `<div class="view-item-summary">${esc(summary)}</div>` : ''}
  </div>`;
}

export function renderPlanSection(section, unitLookup) {
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

export function renderConflictViewPlan(card) {
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

function conflictLevelTwo(card, support, oppose, missing) {
  const overview = [
    `支持 ${support.length}`,
    `反向 ${oppose.length}`,
    `待补 ${missing.length}`,
  ];
  return `<div class="summary compact-overview">${overview.map((item) => `<span>${esc(item)}</span>`).join('')}</div>
    <div class="actions">
      ${card.decision === 'accepted' ? `<button class="action" data-act="reasoning" data-id="${card.id}">生成推论</button>
      <button class="action" data-act="missing" data-id="${card.id}">找待验证点</button>` : ''}
    </div>`;
}

function conflictLevelThree(card, support, oppose, missing, steps) {
  const plan = renderConflictViewPlan(card);
  const fallbackBlocks = !plan
    ? `${listBlock('支持证据', support)}
    ${listBlock('反向证据', oppose)}
    ${listBlock('待补证据', missing)}
    ${listBlock('推理链', steps)}`
    : `${steps.length ? listBlock('推理链', steps) : ''}`;
  return `${plan || ''}
    ${fallbackBlocks}
    <div class="actions detail">
      <button class="action" data-act="decision" data-decision="pending" data-id="${card.id}">暂存</button>
      <button class="action" data-act="decision" data-decision="rejected" data-id="${card.id}">驳回</button>
      <button class="action" data-act="decision" data-decision="accepted" data-id="${card.id}">确认</button>
    </div>`;
}

export function conflictBody(card, map, level) {
  const support = titles(card.detail?.supporting_card_ids, map);
  const oppose = titles(card.detail?.opposing_card_ids, map);
  const missing = titles(card.detail?.missing_card_ids, map);
  const steps = card.detail?.reasoning_steps || [];
  if (level === 1) return conflictLevelOne(card);
  if (level === 2) return conflictLevelTwo(card, support, oppose, missing);
  return conflictLevelThree(card, support, oppose, missing, steps);
}

// 时间线视图（runtime projection）
//
// 数据来自 GET /api/cases/{case_id}/timeline 返回的 timeline.items：
//   [{ item_type: 'event' | 'claim', item_ref, time: {label, sort_value, precision} }]
// 展示内容通过 item_ref 回查 Core Entity（Event / Claim），不复制事实字段。

const TIMELINE_TYPE_LABEL = { event: '事件', claim: '陈述' };

function timelineItemHtml(item, index) {
  const entity = coreEntityByRef(item.item_ref);
  const isClaim = item.item_type === 'claim';
  const typeLabel = TIMELINE_TYPE_LABEL[item.item_type] || item.item_type;

  const timeLabel = item.time?.label || item.time?.sort_value || '时间未知';
  const precision = item.time?.precision && item.time.precision !== 'exact'
    ? `<span class="timeline-point-precision">${esc(item.time.precision)}</span>`
    : '';

  let main = '';
  let sub = '';
  if (isClaim) {
    const claimant = personByRef(entity?.claimant_ref);
    const claimantName = claimant?.name || entity?.claimant_text || '未知陈述者';
    main = entity?.proposition || '（陈述内容缺失）';
    sub = `陈述者：${claimantName}`;
  } else {
    main = entity?.description || '（事件描述缺失）';
  }

  return `<div class="timeline-point timeline-point-${esc(item.item_type)}" data-item-type="${esc(item.item_type)}" data-item-ref="${esc(item.item_ref)}">
    <div class="timeline-point-top">
      <span class="timeline-point-type timeline-point-type-${esc(item.item_type)}">${esc(typeLabel)}</span>
      <span class="timeline-point-index">${index + 1}</span>
    </div>
    <div class="timeline-point-dot" aria-hidden="true"></div>
    <div class="timeline-point-bottom">${esc(timeLabel)}${precision}</div>
    <div class="timeline-point-brief">${esc(main)}</div>
    ${sub ? `<div class="timeline-point-sub">${esc(sub)}</div>` : ''}
  </div>`;
}

export function renderTimelineBody(timeline) {
  const items = timelineItems(timeline);
  if (!items.length) {
    return '<div class="timeline-view is-empty"><div class="muted">暂无可排序的时间信息（需要 Event / Claim 带有 time）。</div></div>';
  }
  const pointHtml = items.map((item, index) => timelineItemHtml(item, index)).join('');
  return `<div class="timeline-view" style="--timeline-count:${items.length}">
    <div class="timeline-track">${pointHtml}</div>
  </div>`;
}

// 假设视图（hypothesis 推理卡 body）

const HYP_STAGE_LABELS = {
  fact_recognition: '事实认定',
  legal_reasoning: '法律推理',
  legal_application: '法律适用',
  final_claim: '最终主张',
};

const HYP_CONFIDENCE_LABELS = { high: '高', medium: '中', low: '低' };

const hypRelationLabel = (relation) => (relation === 'support' ? '支持' : relation === 'attack' ? '反对' : '待定');

// 判断推理卡是否带完整的 hypothesis view_payload（含推论节点/链接），用于决定 wide 布局
export function hasHypothesisBoard(card) {
  const payload = card?.detail?.view_payload;
  if (!payload || payload.view_type !== 'hypothesis') return false;
  const hypotheses = Array.isArray(payload.hypotheses) ? payload.hypotheses : [];
  const links = Array.isArray(payload.links) ? payload.links : [];
  return hypotheses.length > 0 || links.length > 0;
}

// 完整 hypothesis 三列布局（支持证据 / 推论节点 / 反对证据），翻译自 TS 版 HypothesisBoard
export function renderHypothesisBoard(card) {
  if (!hasHypothesisBoard(card)) return '';
  const payload = card.detail?.view_payload;
  const metaNodes = Array.isArray(payload.meta_nodes) ? payload.meta_nodes : [];
  const hypotheses = Array.isArray(payload.hypotheses) ? payload.hypotheses : [];
  const links = Array.isArray(payload.links) ? payload.links : [];
  const groups = Array.isArray(payload.groups) ? payload.groups : [];

  const stageOrder = { fact_recognition: 0, legal_reasoning: 1, legal_application: 2, final_claim: 3 };
  const sortedHypotheses = [...hypotheses].sort(
    (a, b) => (stageOrder[a.stage] ?? 99) - (stageOrder[b.stage] ?? 99)
  );
  const supportLinks = links.filter((l) => l.relation === 'support');
  const attackLinks = links.filter((l) => l.relation === 'attack');
  const pendingLinks = links.filter((l) => l.relation === 'pending');

  const linkItem = (link) => `<li class="hb-link-item hb-link-${esc(link.relation)}">
    <p class="hb-link-explanation">${esc(link.explanation || '')}</p>
    <p class="hb-link-certainty">确定性：${esc(HYP_CONFIDENCE_LABELS[link.certainty] ?? link.certainty)}</p>
  </li>`;

  const nodeCard = (node) => {
    const nodeLinks = links.filter((l) => l.source_id === node.id || l.target_id === node.id);
    const nodeLinksHtml = nodeLinks.length
      ? `<ul class="hb-node-links">${nodeLinks
          .map((l) => `<li class="hb-node-link-item"><span class="hb-link-rel">${esc(hypRelationLabel(l.relation))}</span><span class="hb-link-explanation">${esc(l.explanation || '')}</span></li>`)
          .join('')}</ul>`
      : '';
    return `<div class="hb-node">
      <div class="hb-node-header">
        <span class="hb-stage-badge">${esc(HYP_STAGE_LABELS[node.stage] ?? node.stage)}</span>
        <span class="hb-confidence-badge hb-confidence-${esc(node.confidence)}">${esc(HYP_CONFIDENCE_LABELS[node.confidence] ?? node.confidence)}</span>
      </div>
      <p class="hb-node-title">${esc(node.title || '')}</p>
      ${node.description ? `<p class="hb-node-desc">${esc(node.description)}</p>` : ''}
      ${nodeLinksHtml}
    </div>`;
  };

  const metaRow = metaNodes.length
    ? `<div class="hb-meta-row">${metaNodes
        .map((mn) => `<div class="hb-meta-node" title="${esc(mn.detail || '')}"><p class="hb-meta-title">${esc(mn.title || '')}</p><p class="hb-meta-detail">${esc(mn.detail || '')}</p></div>`)
        .join('')}</div>`
    : '';

  const supportCol = `<div class="hb-col hb-col-support">
    <div class="hb-col-header hb-col-header-support">支持证据</div>
    ${supportLinks.length ? `<ul class="hb-link-list">${supportLinks.map(linkItem).join('')}</ul>` : '<p class="hb-empty">无</p>'}
  </div>`;

  const hypothesesCol = `<div class="hb-col hb-col-hypotheses">
    <div class="hb-col-header">推论节点</div>
    ${sortedHypotheses.length ? `<div class="hb-node-list">${sortedHypotheses.map(nodeCard).join('')}</div>` : '<p class="hb-empty">无推论</p>'}
  </div>`;

  const attackCol = `<div class="hb-col hb-col-attack">
    <div class="hb-col-header hb-col-header-attack">反对证据</div>
    ${attackLinks.length ? `<ul class="hb-link-list">${attackLinks.map(linkItem).join('')}</ul>` : '<p class="hb-empty">无</p>'}
    ${pendingLinks.length ? `<div class="hb-col-header hb-col-header-pending">待定</div><ul class="hb-link-list">${pendingLinks.map(linkItem).join('')}</ul>` : ''}
  </div>`;

  const groupsRow = groups.length
    ? `<div class="hb-groups"><p class="hb-groups-label">证据组合</p>${groups
        .map((g) => `<div class="hb-group hb-group-${esc((g.type || '').toLowerCase())}"><span class="hb-group-type">${esc(g.type || '')}</span><span class="hb-group-status">${g.status === 'complete' ? '完整' : '部分'}</span><span class="hb-group-target">→ ${esc(g.target_hypothesis_id || '')}</span></div>`)
        .join('')}</div>`
    : '';

  return `<div class="hb-root">
    ${metaRow}
    <div class="hb-board">${supportCol}${hypothesesCol}${attackCol}</div>
    ${groupsRow}
  </div>`;
}

export function hypothesisBody(card, map, level) {
  const support = titles(card.detail?.supporting_card_ids, map);
  const oppose = titles(card.detail?.opposing_card_ids, map);
  const missing = titles(card.detail?.missing_card_ids, map);
  const steps = card.detail?.reasoning_steps || [];
  if (level === 1) return `<div class="summary">${esc(card.summary || card.detail?.claim || '暂无摘要')}</div>`;
  if (level === 2) {
    const overview = [
      `支持 ${support.length}`,
      `反对 ${oppose.length}`,
      `待补 ${missing.length}`,
    ];
    return `<div class="summary compact-overview">${overview.map((item) => `<span>${esc(item)}</span>`).join('')}</div>`;
  }
  const board = renderHypothesisBoard(card);
  if (board) return board;
  return `${listBlock('支持证据', support)}
    ${listBlock('反对证据', oppose)}
    ${listBlock('待补证据', missing)}
    ${listBlock('推理步骤', steps)}`;
}

// 通用 body

export function normalBody(card, level) {
  if (level === 1) return '';
  const summary = card.summary || card.detail?.claim || card.detail?.text || card.detail?.description || '暂无摘要';
  if (card.card_type === 'inference' && card.inference_type !== 'conflict' && card.inference_type !== 'missing_evidence' && level >= 3) {
    const steps = Array.isArray(card.detail?.reasoning_steps) ? card.detail.reasoning_steps.filter(Boolean).slice(0, 3) : [];
    const stepHtml = steps.length ? `<div class="inference-steps">${steps.map((step) => `<span>${esc(step)}</span>`).join('')}</div>` : '';
    return `<div class="inference-body"><div class="inference-detail">${esc(summary)}</div>${stepHtml}</div>`;
  }
  return `<div class="summary">${esc(summary)}</div>`;
}

export function metaBody(card, level) {
  if (level === 1) return '';
  if (level === 2) {
    const metaSummary = card.summary || card.detail?.claim || card.detail?.text || '暂无摘要';
    return `<div class="meta-body"><div class="meta-summary">${esc(metaSummary)}</div></div>`;
  }
  if (level === 3) {
    const metaText = card.detail?.description || '暂无详情';
    const metaReason = card.detail?.attributes?.extraction_reason || '暂无理由';
    return `<div class="meta-body"><div class="meta-text">详情：${esc(metaText)}</div><div class="meta-text">理由：${esc(metaReason)}</div></div>`;
  }
  return '';
}

export function missingBody(card, level) {
  if (level === 1) return '';
  const missingText = card.summary || card.detail?.claim || card.title || '';
  return `<div class="missing-body"><div class="missing-text">${esc(missingText)}</div></div>`;
}

// 推理类型 badge

export const inferenceBadgeLabel = (inferenceType) => ({
  hypothesis: '假设',
  conclusion: '结论',
  timeline: '时序',
  risk: '风险',
  other: '推论',
})[inferenceType] || '推论';
