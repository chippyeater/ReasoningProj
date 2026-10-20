import { defineConfig } from "vite";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  server: {
    port: 5173,
    // 前端 API 全部走相对路径 /api/*，开发时由 Vite 代理到后端。
    // 否则请求会打到 Vite dev server 本身，返回 index.html（HTML）或 404。
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/health": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  build: {
    rollupOptions: {
      // 明确告诉 Vite 入口是 frontend/index.html（纯 JS，无 React/TS）。
      input: resolve(__dirname, "index.html"),
    },
  },
});
