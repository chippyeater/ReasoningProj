// frontend/js/config.js
// 拆分自原 index.js 第 1-4, 33-37 行
// 常量与资源路径

export const API = 'http://localhost:8000';
export const OFF = 5000;
export const MIN_LEVEL = 1;
export const MAX_LEVEL = 3;

export const EDGE_ICON_SRC = {
  danger: new URL('../assets/Familiar.png', import.meta.url).href,
  warn: new URL('../assets/Warn.png', import.meta.url).href,
  success: new URL('../assets/Success.png', import.meta.url).href,
};
