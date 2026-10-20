// frontend/js/labels.js
// 拆分自原 index.js 第 68-160 行
// 纯 label/icon/edge visual 函数，无副作用，无 state 依赖（除 EDGE_ICON_SRC 常量）

import { EDGE_ICON_SRC } from './config.js';

export const iconOf = (kind) => ({
  person: '人',
  location: '地',
  object: '物',
  event: '事',
  claim: '说',
  evidence: '证',
  conflict: '冲',
  missing: '缺',
  timeline: '时',
})[kind] || '推';

export const decisionLabel = (decision) => {
  if (decision === 'accepted') return '已确认';
  if (decision === 'rejected') return '已驳回';
  if (decision === 'pending') return '暂存';
  if (decision === 'undecided') return '待确认';
  return '未处理';
};

export const levelLabel = (level) => {
  if (level === 3) return '详情';
  if (level === 2) return '概要';
  return '胶囊';
};

export const edgeLabel = (type) => ({
  claimed_by: '声称者',
  involved_in: '参与',
  about: '关于',
  occurs_in: '发生地',
  involves_object: '涉及物',
  supports: '支持',
  contradicts: '矛盾',
})[type] || type;

export const edgeVisual = (edge) => {
  if (edge.edge_type === 'supports') {
    return { color: '#36FF36', icon: EDGE_ICON_SRC.success, iconOnly: true, tone: 'success' };
  }
  if (edge.edge_type === 'contradicts') {
    return { color: '#FF0000', icon: EDGE_ICON_SRC.danger, iconOnly: true, tone: 'danger' };
  }
  // Phase 1c structural edges（claimed_by/involved_in/about/occurs_in/involves_object）：中性
  return { color: '#FFFFFF', icon: null, iconOnly: false, tone: 'neutral' };
};

export const lineColor = (edge) => edgeVisual(edge).color;

// Phase 1b.1：删除 preferredSideLabel；RecommendedResolution.preferred_side 已从
// 后端 schema 移除，立场归类推迟到 Phase 1c Relation 阶段。
// 不再接受 / 输出 side/position 字段。
const recommendationDecisionLabel = (decision) => {
  if (decision === 'accepted') return '建议确认';
  if (decision === 'rejected') return '建议驳回';
  return '建议暂存';
};

// typeMeta 也在 labels.js 里因为它本质是 label；放在 cards.js 会形成 cards → labels 反向依赖
export const typeMeta = (card) => {
  if (card.card_type === 'meta') return String(card.entity_kind || 'meta');
  if (card.inference_type === 'conflict') return `冲突 / ${decisionLabel(card.decision)}`;
  if (card.inference_type === 'missing_evidence') return '待验证点';
  return '推理卡';
};

export const recommendationText = (card) => {
  const resolution = card.detail?.recommended_resolution;
  if (!resolution || typeof resolution !== 'object') return '';
  const reason = String(resolution.reason || '').trim();
  if (!reason) return '';
  const confidence = typeof resolution.confidence === 'number' ? ` ? ${Math.round(resolution.confidence * 100)}%` : '';
  return `系统建议：${recommendationDecisionLabel(resolution.recommended_decision)}${confidence}。${reason}`;
};
