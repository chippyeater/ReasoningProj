// frontend/js/http.js
// HTML 转义、状态条、fetch 包装、统一错误转译

import { dom } from './state.js';

export const esc = (value) => String(value ?? '')
  .replace(/&/g, '&amp;')
  .replace(/</g, '&lt;')
  .replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;')
  .replace(/'/g, '&#39;');

export const setStatus = (message) => {
  dom.statusEl.textContent = message;
};

// 把底层报错转译成用户能看懂的简短提示。
// 完整错误通过 console.error 输出，状态条只显示这一句。
export function friendlyError(error) {
  const raw = String(error?.message || error || '');
  if (error?.status === 404 || /HTTP 404|\b404\b/.test(raw)) {
    return '接口不存在，请确认后端已启动且路由正确';
  }
  if (error?.status === 500 || /HTTP 500|\b500\b/.test(raw)) {
    return '服务内部错误，请查看后端日志';
  }
  if (/Unexpected token|<!DOC|<html/i.test(raw)) {
    return '后端未启动或未配置代理，请确认后端已在 8000 端口运行';
  }
  if (/failed to fetch|networkerror|load failed|ECONNREFUSED/i.test(raw)) {
    return '无法连接后端服务，请确认后端已启动';
  }
  return '操作失败，请重试';
}

// 统一错误出口：console 输出完整堆栈（方便调试），状态条只显示简短转译。
export function fail(context, error) {
  console.error(`[${context}]`, error);
  setStatus(`${context}：${friendlyError(error)}`);
}

// fetch 包装：非 2xx 抛错（携带 status），200 但 body 不是 JSON 也转成友好错误。
export const json = async (response) => {
  if (!response.ok) {
    const text = await response.text();
    const err = new Error(text || `HTTP ${response.status}`);
    err.status = response.status;
    throw err;
  }
  try {
    return await response.json();
  } catch (e) {
    const err = new Error('响应不是有效的 JSON（后端可能未启动）');
    err.status = response.status;
    err.cause = e;
    throw err;
  }
};
