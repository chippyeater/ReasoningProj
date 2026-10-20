// frontend/js/edges.js
// 拆分自原 index.js 812-931, 933-973 行
// SVG 边绘制：箭头、pill 标签、详情弹窗、updateLines

import { state, dom } from './state.js';
import { OFF } from './config.js';
import { edgeLabel, edgeVisual } from './labels.js';
import { cardMap } from './cards.js';

// 箭头

function buildEdgeArrow(endPoint, previousPoint, color) {
  const angle = Math.atan2(endPoint.y - previousPoint.y, endPoint.x - previousPoint.x) * 180 / Math.PI;
  const arrow = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
  arrow.setAttribute('points', '-10,-5 0,0 -10,5');
  arrow.setAttribute('fill', color);
  arrow.setAttribute('transform', `translate(${endPoint.x},${endPoint.y}) rotate(${angle})`);
  return arrow;
}

// pill 标签

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
    group.style.cursor = 'pointer';
    group.addEventListener('dblclick', (e) => { e.stopPropagation(); showEdgeDetails(edge); });
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
  dom.links.appendChild(group);

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
  group.style.cursor = 'pointer';
  group.addEventListener('dblclick', (e) => { e.stopPropagation(); showEdgeDetails(edge); });
  return group;
}

// 边详情弹窗

function showEdgeDetails(edge) {
  const prev = document.getElementById('edge-detail-overlay');
  if (prev) prev.remove();

  const overlay = document.createElement('div');
  overlay.id = 'edge-detail-overlay';
  Object.assign(overlay.style, {
    position: 'fixed', left: '0', top: '0', right: '0', bottom: '0',
    background: 'rgba(0,0,0,0.45)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 9999
  });

  const card = document.createElement('div');
  Object.assign(card.style, {
    width: 'min(720px, 90%)', maxHeight: '80%', overflow: 'auto', background: 'linear-gradient(180deg, rgba(34,45,40,.98), rgba(19,26,23,.99))',
    border: '1px solid rgba(255,255,255,.08)', borderRadius: '12px', padding: '18px', color: '#fff', boxShadow: '0 24px 80px rgba(0,0,0,.5)'
  });

  const title = document.createElement('div');
  title.textContent = `边: ${edge.id || ''}`;
  Object.assign(title.style, { fontSize: '16px', fontWeight: 700, marginBottom: '8px' });

  const table = document.createElement('div');
  const addRow = (k, v) => {
    const row = document.createElement('div');
    Object.assign(row.style, { display: 'flex', gap: '12px', marginBottom: '6px', alignItems: 'flex-start' });
    const kEl = document.createElement('div'); kEl.textContent = `${k}:`; Object.assign(kEl.style, { color: 'var(--sub)', minWidth: '110px' });
    const vEl = document.createElement('div'); vEl.textContent = v == null ? '' : String(v);
    row.appendChild(kEl); row.appendChild(vEl); table.appendChild(row);
  };

  const map = cardMap();
  const srcTitle = map.get(edge.source)?.title || edge.source;
  const tgtTitle = map.get(edge.target)?.title || edge.target;

  addRow('类型', edge.edge_type || '');
  addRow('标签', edge.label || '');
  addRow('来源', `${edge.source} ${srcTitle ? ` — ${srcTitle}` : ''}`);
  addRow('目标', `${edge.target} ${tgtTitle ? ` — ${tgtTitle}` : ''}`);
  addRow('关系类型', edge.relation_type || '');
  addRow('权重', edge.weight ?? '');
  addRow('创建时间', edge.created_at || '');

  const closeBtn = document.createElement('button');
  closeBtn.textContent = '关闭';
  Object.assign(closeBtn.style, { marginTop: '12px', padding: '8px 12px', borderRadius: '8px', background: 'rgba(255,255,255,.06)', color: '#fff', border: '1px solid rgba(255,255,255,.08)', cursor: 'pointer' });
  closeBtn.addEventListener('click', () => overlay.remove());

  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });

  card.appendChild(title);
  card.appendChild(table);
  card.appendChild(closeBtn);
  overlay.appendChild(card);
  document.body.appendChild(overlay);
}

// Phase 1c：semantic relations → edge 投影（前端 runtime projection）。
// CaseData 不再持久化 edges；structural/semantic GraphEdge 都由运行时投影产生。
// 前端最小 adapter：仅投影 semantic relations（supports/contradicts）用于画布展示。
function projectEdges() {
  const caseData = state.caseData;
  if (!caseData) return [];
  const edges = [];
  for (const rel of (caseData.semantic_relations || [])) {
    edges.push({
      id: rel.id,
      source: rel.source_id,
      target: rel.target_id,
      edge_type: rel.relation_type,
      label: rel.relation_type,
      relation_type: rel.relation_type,
      weight: 2.2,
      created_at: rel.created_at,
    });
  }
  return edges;
}

// 重画所有边

export function updateLines() {
  dom.links.innerHTML = '';
  if (!state.caseData) return;
  for (const edge of projectEdges()) {
    const source = document.getElementById(edge.source);
    const target = document.getElementById(edge.target);
    if (!source || !target) continue;
    const sourceRect = source.getBoundingClientRect();
    const targetRect = target.getBoundingClientRect();
    const contentRect = dom.content.getBoundingClientRect();
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
    group.style.cursor = 'pointer';
    group.addEventListener('dblclick', (e) => { e.stopPropagation(); showEdgeDetails(edge); });
    dom.links.appendChild(group);

    const totalLength = path.getTotalLength();
    const endPoint = path.getPointAtLength(totalLength);
    const previousPoint = path.getPointAtLength(Math.max(0, totalLength - 12));
    group.appendChild(buildEdgeArrow(endPoint, previousPoint, visual.color));

    const pill = buildEdgePill(edge, path);
    if (pill) group.appendChild(pill);
  }
}
