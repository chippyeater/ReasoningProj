// frontend/js/layout.js
// 拆分自原 index.js 975-1006 行
// 画布 transform / fitView / focusNode

import { state, dom } from './state.js';
import { allCards, positionOf } from './cards.js';
import { updateLines } from './edges.js';

export function transform() {
  dom.content.style.transform = `translate(${state.transform.x}px, ${state.transform.y}px) scale(${state.transform.scale})`;
  updateLines();
}

export function fitView() {
  const cards = allCards().filter((card) => card.status !== 'archived' && card.status !== 'hidden');
  if (!cards.length) return;
  const positions = cards.map((card, index) => positionOf(card, index));
  const minX = Math.min(...positions.map((point) => point.x));
  const minY = Math.min(...positions.map((point) => point.y));
  const maxX = Math.max(...positions.map((point) => point.x + 360));
  const maxY = Math.max(...positions.map((point) => point.y + 260));
  const boxWidth = maxX - minX;
  const boxHeight = maxY - minY;
  const sx = (dom.wrap.clientWidth - 120) / Math.max(boxWidth, 1);
  const sy = (dom.wrap.clientHeight - 120) / Math.max(boxHeight, 1);
  state.transform.scale = Math.max(0.45, Math.min(1, Math.min(sx, sy)));
  state.transform.x = 60 - minX * state.transform.scale;
  state.transform.y = 60 - minY * state.transform.scale;
  transform();
}

export function focusNode(id) {
  const node = document.getElementById(id);
  if (!node) return;
  const x = parseFloat(node.style.left || '0');
  const y = parseFloat(node.style.top || '0');
  state.transform.x = dom.wrap.clientWidth * 0.5 - (x + node.offsetWidth / 2) * state.transform.scale;
  state.transform.y = dom.wrap.clientHeight * 0.38 - (y + node.offsetHeight / 2) * state.transform.scale;
  transform();
}
