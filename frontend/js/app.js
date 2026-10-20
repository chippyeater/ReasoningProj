// frontend/js/app.js
// 入口 bootstrap：import 所有模块，挂到 window 解决循环依赖，attachGlobalEvents，初始化
//
// 使用方式：index.html 引用 ./index.js；index.js 顶部 import './js/app.js' 即可。
//
// 循环依赖解法：
// - render.js 调 window.__bindNode (events.js 注入)
// - render.js 调 window.__updateLines (edges.js 注入)
// - api.js 调 window.__render / window.__focusNode (render.js + layout.js 注入)
// - render.js 调 queueSave (本文件里再 export) — 这是 render.js 内部依赖
//
// 时序：app.js import render.js 时 render.js 立即执行（注册 window.__render），
//      然后 app.js 调 loadCase() 时 api.js 调 window.__render 即可工作。

import { render } from './render.js';
import { focusNode, fitView, transform } from './layout.js';
import { updateLines } from './edges.js';
import { bindNode, attachGlobalEvents } from './events.js';
import { loadCase } from './api.js';
import { setStatus, fail } from './http.js';
import { state } from './state.js';

// 挂到 window：让其他模块（render.js / api.js）通过 window.__xxx 查找
window.__render = render;
window.__focusNode = focusNode;
window.__updateLines = updateLines;
window.__bindNode = bindNode;

// 初始化
export async function init() {
  attachGlobalEvents();
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
    fail('初始化', error);
  }
}

// DOM ready 后启动
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
