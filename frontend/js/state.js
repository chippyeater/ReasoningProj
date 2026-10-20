// frontend/js/state.js
// 拆分自原 index.js 第 6-31 行
// 全局 state 与 14 个 DOM 引用

export const state = {
  caseData: null,
  selected: [],
  focused: null,
  transform: { x: 360, y: 80, scale: 1 },
  draggingCanvas: false,
  panX: 0,
  panY: 0,
  pending: new Map(),
  pendingLevels: new Map(),
  // Timeline runtime projection（GET /api/cases/{id}/timeline，不持久化）
  timeline: null,
};

export const dom = {
  wrap: document.getElementById('wrap'),
  content: document.getElementById('content'),
  links: document.getElementById('links'),
  nodes: document.getElementById('nodes'),
  fileList: document.getElementById('fileList'),
  selectionList: document.getElementById('selectionList'),
  empty: document.getElementById('empty'),
  caseTitle: document.getElementById('caseTitle'),
  statusEl: document.getElementById('status'),
  promptEl: document.getElementById('prompt'),
  uploadZone: document.getElementById('uploadZone'),
  fileInput: document.getElementById('fileInput'),
  reloadBtn: document.getElementById('reloadBtn'),
  relationBtn: document.getElementById('relationBtn'),
  timelineBtn: document.getElementById('timelineBtn'),
  fitBtn: document.getElementById('fitBtn'),
  sendBtn: document.getElementById('sendBtn'),
  timelinePanel: document.getElementById('timelinePanel'),
  timelineBody: document.getElementById('timelineBody'),
  timelineCloseBtn: document.getElementById('timelineCloseBtn'),
};
