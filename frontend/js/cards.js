// frontend/js/cards.js
// 拆分自原 index.js 58-66, 141-160, 162-173, 175-255 行
// card 查询、视图类型判断、position 计算、footprint 尺寸

import { state } from './state.js';
import { MIN_LEVEL, MAX_LEVEL } from './config.js';
import { decisionLabel } from './labels.js';

// 基础查询

export const allCards = () => state.caseData
  ? [...state.caseData.meta_cards, ...state.caseData.inference_cards]
  : [];

export const cardMap = () => new Map(allCards().map((card) => [card.id, card]));

export const generationScope = () => state.selected.length
  ? state.selected.slice()
  : (state.caseData?.meta_cards || []).slice(0, 4).map((card) => card.id);

// Timeline：runtime projection，不绑定 inference card。
// TimelineItem = { item_type: 'event' | 'claim', item_ref, time: { label, sort_value, precision } }
// 前端通过 item_ref 回查 Core Entity，不复制 Core Entity 字段。

export function timelineItems(timeline) {
  return Array.isArray(timeline?.items) ? timeline.items.filter((item) => item && typeof item === 'object') : [];
}

// 回查 Core Entity（Event / Claim）以获取展示内容。
export function coreEntityByRef(ref) {
  if (!state.caseData || !ref) return null;
  const groups = [state.caseData.events, state.caseData.claims];
  for (const group of groups) {
    const found = (group || []).find((entity) => entity.id === ref);
    if (found) return found;
  }
  return null;
}

// 回查 Person（用于 Claim claimant 展示）。
export function personByRef(ref) {
  if (!state.caseData || !ref) return null;
  return (state.caseData.persons || []).find((person) => person.id === ref) || null;
}

// kind 分类

export const kindOf = (card) => {
  if (card.card_type === 'meta') return card.entity_kind || 'meta';
  if (card.inference_type === 'conflict') return 'conflict';
  if (card.inference_type === 'missing_evidence') return 'missing';
  return 'hypothesis';
};

// level 处理

export const clampLevel = (value) => Math.max(MIN_LEVEL, Math.min(MAX_LEVEL, value));

export function semanticBaseLevel(card) {
  return clampLevel(card.display_level || 2);
}

// 卡片尺寸（用于画布布局）

export function cardFootprint(card, index = 0) {
  const level = semanticBaseLevel(card);
  const viewType = card.detail?.view_payload?.view_type;
  if (card.card_type === 'inference' && card.inference_type === 'conflict' && level >= 3) {
    const hasPlan = Array.isArray(card.detail?.view_payload?.layout_plan?.sections) && card.detail.view_payload.layout_plan.sections.length > 0;
    return { width: hasPlan ? 860 : 420, height: hasPlan ? 560 : 320 };
  }
  if (card.card_type === 'meta' && level >= 2) return { width: 432, height: 220 };
  if (card.card_type === 'inference' && card.inference_type === 'missing_evidence' && level >= 2) return { width: 432, height: 220 };
  if (card.card_type === 'inference' && viewType === 'hypothesis' && level >= 3) {
    const hypotheses = Array.isArray(card.detail?.view_payload?.hypotheses) ? card.detail.view_payload.hypotheses : [];
    const links = Array.isArray(card.detail?.view_payload?.links) ? card.detail.view_payload.links : [];
    if (hypotheses.length || links.length) return { width: 860, height: 560 };
  }
  if (card.card_type === 'inference' && level >= 3) return { width: 360, height: 260 };
  if (level === 2) return { width: 320, height: 180 };
  return { width: 220, height: 96 };
}

// 寻找空位

export function findOpenCanvasPosition(card) {
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

// 选中辅助

export function hoveredSelectedCardFromEvent(event) {
  const node = event.target instanceof Element ? event.target.closest('.node') : null;
  if (!node) return null;
  if (!state.selected.includes(node.id)) return null;
  return cardMap().get(node.id) || null;
}

export function selectedCards() {
  const map = cardMap();
  return state.selected.map((id) => map.get(id)).filter(Boolean);
}

// position 计算

export function fallbackPosition(card, index) {
  const metaIndex = state.caseData.meta_cards.findIndex((item) => item.id === card.id);
  const inferenceIndex = state.caseData.inference_cards.findIndex((item) => item.id === card.id);
  if (card.card_type === 'meta') return { x: 80 + (metaIndex % 2) * 360, y: 80 + Math.floor(metaIndex / 2) * 240 };
  return { x: 900 + (inferenceIndex % 2) * 420, y: 240 + Math.floor(inferenceIndex / 2) * 280 + index * 4 };
}

export function positionOf(card, index) {
  const position = card.position || { x: 0, y: 0 };
  if (typeof position.x === 'number' && typeof position.y === 'number' && (position.x !== 0 || position.y !== 0)) {
    return position;
  }
  return fallbackPosition(card, index);
}
